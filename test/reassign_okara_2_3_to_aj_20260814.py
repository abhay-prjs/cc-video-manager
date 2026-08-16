"""One-off: reassign Ivan's Okara 2 and Okara 3 (currently Jewel, part of the
2026-08-14 bulk-assign) to AJ, replicating the exact real /reassign path
(ReassignEditorSelect._on_select's Notion branch in discord_bot.py) rather
than writing straight to discord_queue.json — see CLAUDE.md's bulk-assign
gotcha about that shortcut leaving Editor unset / stats out of sync.

Steps per folder, matching ReassignEditorSelect exactly:
  1. Notion PATCH: Editor -> AJ, Status -> In Progress.
  2. update_deadline_editor(folder_id, notion_page_id, 'AJ') — repoints the
     deadlines.json entry so the deadline checker/pickup ladder follows AJ.
  3. recalculate_active_videos for AJ and for Jewel (both change).
  4. Enqueue a type-less discord_queue.json item with is_reassign=True — the
     running discord-bot picks it up (~3s) and runs assign_folder(), sending
     AJ the '🔁 Reassigned to You' embed.
  5. Enqueue a reassign_notify item — discord-bot's handle_reassign_notify
     pings the creator (Ivan's channel) and the outgoing editor (Jewel).

Dry-run by default; --live to execute.
"""
import argparse
import json
import os
import time

import requests
from filelock import FileLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
DEADLINES_FILE = os.path.join(BASE_DIR, 'deadlines.json')
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
QUEUE_LOCK = FileLock(QUEUE_FILE + '.lock')
DEADLINES_LOCK = FileLock(DEADLINES_FILE + '.lock')

NEW_EDITOR = 'AJ'

FOLDERS = [
    {
        'client_name': 'Ivan', 'folder_name': 'Okara 2', 'video_count': 10,
        'folder_id': '1oDFrz_WuTS2lbX1S6_n3rwwPFQlbO5fN',
        'notion_page_id': '3bbe637c-317d-81d6-9dad-cb55fc534e79',
        'old_editor': 'Jewel',
    },
    {
        'client_name': 'Ivan', 'folder_name': 'Okara 3', 'video_count': 10,
        'folder_id': '1oBeqk94yZw690i9GFX436XaqUm_wA6KH',
        'notion_page_id': '3bbe637c-317d-8171-8585-d78a9128f9b8',
        'old_editor': 'Jewel',
    },
]


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def notion_patch(token, page_id, props):
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(token), json={'properties': props}, timeout=15,
    )
    if not resp.ok:
        print(f'  ! Notion PATCH failed for {page_id}: {resp.status_code} {resp.text[:200]}')
    return resp.ok


def verify_current_state(token, page_id, expected_editor, expected_status='In Progress'):
    resp = requests.get(f'https://api.notion.com/v1/pages/{page_id}', headers=notion_headers(token), timeout=15)
    if not resp.ok:
        return False, f'fetch failed {resp.status_code}'
    props = resp.json().get('properties', {})
    editor = (props.get('Editor', {}).get('select') or {}).get('name', '')
    status = (props.get('Status', {}).get('select') or {}).get('name', '')
    if editor != expected_editor or status != expected_status:
        return False, f'expected editor={expected_editor!r} status={expected_status!r}, found editor={editor!r} status={status!r}'
    return True, ''


def update_deadline_editor(folder_id, notion_page_id, new_editor):
    with DEADLINES_LOCK:
        deadlines = {}
        if os.path.exists(DEADLINES_FILE):
            with open(DEADLINES_FILE) as f:
                deadlines = json.load(f)
        key = folder_id if (folder_id and folder_id in deadlines) else None
        if key is None and notion_page_id:
            for fid, d in deadlines.items():
                if d.get('notion_page_id') == notion_page_id:
                    key = fid
                    break
        if key is None:
            key = folder_id or notion_page_id
            if not key:
                return
        entry = deadlines.get(key, {})
        entry['editor_name'] = new_editor
        due_ts = entry.get('due_ts')
        if not entry.get('indefinite') and due_ts and (due_ts - time.time()) > 6 * 3600:
            entry['warned_6h'] = False
        deadlines[key] = entry
        with open(DEADLINES_FILE, 'w') as f:
            json.dump(deadlines, f, indent=2)


def recalculate_active_videos(token, editor_name):
    import re
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {'and': [
            {'property': 'Editor', 'select': {'equals': editor_name}},
            {'or': [
                {'property': 'Status', 'select': {'equals': 'In Progress'}},
                {'property': 'Status', 'select': {'equals': 'Raw'}},
            ]},
        ]}
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    total = 0
    if resp.ok:
        for page in resp.json().get('results', []):
            notes_rt = page['properties'].get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            total += int(m.group(1)) if m else 0

    prof_url = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    prof_resp = requests.post(prof_url, headers=notion_headers(token), json={}, timeout=15)
    page_id, capacity = None, 0
    for page in prof_resp.json().get('results', []):
        name_rt = page['properties'].get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else ''
        if name == editor_name:
            page_id = page['id']
            capacity = page['properties'].get('Capacity', {}).get('number') or 0
            break
    if not page_id:
        print(f'  ! recalculate_active_videos: {editor_name} not found in Editor Profiles')
        return total

    ratio = total / capacity if capacity else 0
    status = 'Overloaded' if ratio >= 0.85 else 'Busy' if ratio >= 0.6 else 'Available'
    requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(token),
        json={'properties': {'Active Videos': {'number': total}, 'Status': {'select': {'name': status}}}},
        timeout=15,
    )
    print(f'  recalculate_active_videos: {editor_name} -> {total} ({status})')
    return total


def append_to_discord_queue(item):
    with QUEUE_LOCK:
        queue = []
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f:
                queue = json.load(f)
        queue.append(item)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(queue, f, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true')
    args = ap.parse_args()

    config = load_config()
    token = config['notion_token']

    print(f'Plan: reassign {len(FOLDERS)} folder(s) to {NEW_EDITOR}.')
    for f in FOLDERS:
        ok, msg = verify_current_state(token, f['notion_page_id'], f['old_editor'])
        status = 'OK' if ok else f'MISMATCH ({msg})'
        print(f"  {f['client_name']} / {f['folder_name']}: currently {f['old_editor']} -> {status}")
        if not ok and args.live:
            print('  Aborting live run — live Notion state does not match what this script expects.')
            return

    if not args.live:
        print('\nDry run — no writes. Re-run with --live to execute.')
        return

    print()
    for f in FOLDERS:
        label = f"{f['client_name']} / {f['folder_name']}"
        patched = notion_patch(token, f['notion_page_id'], {
            'Editor': {'select': {'name': NEW_EDITOR}},
            'Status': {'select': {'name': 'In Progress'}},
        })
        if not patched:
            print(f'  SKIP (Notion patch failed): {label}')
            continue

        update_deadline_editor(f['folder_id'], f['notion_page_id'], NEW_EDITOR)
        recalculate_active_videos(token, NEW_EDITOR)
        if f['old_editor'] != NEW_EDITOR:
            recalculate_active_videos(token, f['old_editor'])

        append_to_discord_queue({
            'client_name':          f['client_name'],
            'folder_name':          f['folder_name'],
            'video_count':          f['video_count'],
            'folder_id':            f['folder_id'],
            'editor_name':          NEW_EDITOR,
            'notion_queue_page_id': f['notion_page_id'],
            'is_reassign':          True,
        })
        append_to_discord_queue({
            'type':        'reassign_notify',
            'client_name': f['client_name'],
            'folder_name': f['folder_name'],
            'old_editor':  f['old_editor'],
            'new_editor':  NEW_EDITOR,
        })
        print(f'  OK: {label} -> {NEW_EDITOR}')
        time.sleep(0.3)

    print(f'\nQueued reassign notifications for discord-bot to pick up (polls every 3s).')


if __name__ == '__main__':
    main()

"""One-off: bulk-assign the 2026-08-14 pending-folder backlog (42 Raw Active
Queue folders) per the plan Vex approved in chat. Steven is off today; Gabo
(132/70 capacity) and AJ (69/70) are excluded as over/near capacity. 6 Ivan
folders (Ape 4/Ape 3/Okara 4-7, 69 videos) are left unassigned — no headroom
remains anywhere once the rest of the plan is placed.

Follows the exact real /assign path (see CLAUDE.md "Known Gotchas" — bulk
writes must not bypass this):
  1. _assign_raw_to_editor equivalent: PATCH Notion Status -> In Progress and
     Editor -> assigned editor, for every Active Queue row matching folder_id.
  2. Enqueue a plain (type-less) discord_queue.json item so the *running*
     discord-bot service picks it up within 3s and runs the real
     assign_folder() (Discord embed, deadlines entry, recalculate_active_videos)
     + handle_creator_notify() — identical code path the /assign slash command
     itself calls, just via the same queue IPC notion_bridge.py already uses.

Dry-run by default; pass --live to actually write.
"""
import argparse
import json
import os
import sys
import time

import requests
from filelock import FileLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
QUEUE_LOCK = FileLock(QUEUE_FILE + '.lock')

# page_id -> editor, built from the live Raw-folder pull (see chat draft plan)
PLAN = {
    # Jill <- Krish
    '3bbe637c-317d-81f4-95b2-f138c4bef161': 'Jill',   # Krish / Invo batch 2 (48)
    '3bbe637c-317d-813f-ab48-f7874f913cb5': 'Jill',   # Krish / Composio 1 (4)

    # Danie <- Henry (all 7)
    '3bbe637c-317d-8171-b27f-cb216970ec06': 'Danie',  # Henry / higgsfield 13 (7)
    '3bbe637c-317d-81d7-86ae-eb50b60e934c': 'Danie',  # Henry / manus 2 (4)
    '3bbe637c-317d-815b-bd74-cbe53aaf072e': 'Danie',  # Henry / phrasly 48 (12)
    '3bbe637c-317d-8188-bf6f-c21417a8007f': 'Danie',  # Henry / monid 3 (3)
    '3bbe637c-317d-8160-9146-cbfdc560fc69': 'Danie',  # Henry / higgsfield 12 (8)
    '3bbe637c-317d-81b7-9470-e4242d98fa66': 'Danie',  # Henry / mathgpt 5 (20)
    '3bbe637c-317d-810d-9bbd-e29663cd3503': 'Danie',  # Henry / mathgpt 4 (1)

    # Storm <- Chris (all 4 Aug 13th)
    '3bce637c-317d-8165-937d-f5956e67153e': 'Storm',  # Chris / August 13th composio (14)
    '3bbe637c-317d-8125-9357-e390d70ec6aa': 'Storm',  # Chris / August 13th openart youtube (11)
    '3bbe637c-317d-81e9-8cd5-f978732abe63': 'Storm',  # Chris / August 13th openart women (11)
    '3bbe637c-317d-8161-abde-dee9505a30d8': 'Storm',  # Chris / August 13th lovable format 2 (11)

    # Naomi <- Mina (5) + Natashka (2)
    '3bae637c-317d-81a2-bcde-e487b64f5d80': 'Naomi',  # Mina / airlearn 6 (6)
    '3bae637c-317d-81b2-881a-ff0e1e64cc36': 'Naomi',  # Mina / airlearn 3 (7)
    '3bae637c-317d-81f7-860b-e3b7fc18ed36': 'Naomi',  # Mina / airlearn 4 (6)
    '3bae637c-317d-8157-9995-e690dd025e56': 'Naomi',  # Mina / airlearn 5 (6)
    '3bae637c-317d-8160-b6a4-e378f8453c76': 'Naomi',  # Mina / airlearn 2 (7)
    '3bbe637c-317d-8162-a1ba-c932437716e1': 'Naomi',  # Natashka / Invo 2 (9)
    '3bbe637c-317d-819f-97fc-ce9943a103f8': 'Naomi',  # Natashka / Phrasly 2 (1)

    # Aki <- Karol + Jauseff + Jonny
    '3bbe637c-317d-8172-96e0-da213beae7f7': 'Aki',    # Karol / 13.08 invo 4 (16)
    '3bbe637c-317d-8139-991e-f878f515f4cb': 'Aki',    # Jauseff / phrasly 16 (14)
    '3bbe637c-317d-8170-8812-dd252ff64331': 'Aki',    # Jonny / Composio 10 (2)

    # Josh <- Cat + Alejandro + Joshua + Ivan/Ape 4 (4-vid one)
    '3bbe637c-317d-81cc-bd27-d0e63dcf8c92': 'Josh',   # Cat / Invo Crypto rocket (2)
    '3bbe637c-317d-812b-9250-eec30324efd9': 'Josh',   # Cat / AnimateWorld 2 (11)
    '3bbe637c-317d-814a-b5fe-cb6216b53307': 'Josh',   # Cat / Invo 2 (1)
    '3bce637c-317d-814b-9b17-dca2295a6eef': 'Josh',   # Alejandro / Phrasly 12 (10)
    '3bce637c-317d-81aa-9d42-cdb71c64eed7': 'Josh',   # Joshua / invo 32 (4)
    '3bbe637c-317d-8139-ab0a-e32ca082a8a4': 'Josh',   # Joshua / invo 30 (3)
    '3bbe637c-317d-8150-b3dd-ccc9d0f5246f': 'Josh',   # Ivan / Ape 4, 4 videos

    # Jewel <- Ivan Okara 1-3
    '3bbe637c-317d-8177-b19c-fc643ce0bff5': 'Jewel',  # Ivan / Okara 1 (10)
    '3bbe637c-317d-81d6-9dad-cb55fc534e79': 'Jewel',  # Ivan / Okara 2 (10)
    '3bbe637c-317d-8171-8585-d78a9128f9b8': 'Jewel',  # Ivan / Okara 3 (10)

    # Zyon <- Ivan Nook 4 + Launchpoint 11 + Ape 2
    '3bbe637c-317d-811b-881f-e5361b1a811d': 'Zyon',   # Ivan / Nook 4 (1)
    '3bbe637c-317d-8186-a996-e5d8cfc77434': 'Zyon',   # Ivan / Launchpoint 11 (3)
    '3bbe637c-317d-810e-b288-dc73c21acd23': 'Zyon',   # Ivan / Ape 2 (8)
}
# Left unassigned (overflow, no headroom): Ivan / Ape 4 (15), Ape 3 (14),
# Okara 4/5/6/7 (10 each) = 6 folders, 69 videos.


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
    url = f'https://api.notion.com/v1/pages/{page_id}'
    resp = requests.patch(url, headers=notion_headers(token), json={'properties': props}, timeout=15)
    if not resp.ok:
        print(f'  ! Notion PATCH failed for {page_id}: {resp.status_code} {resp.text[:200]}')
    return resp.ok


def fetch_raw_rows(token):
    import re
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
    results, cursor = [], None
    while True:
        b = dict(body, page_size=100)
        if cursor:
            b['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=b, timeout=15)
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')

    rows = {}
    for page in results:
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        fname = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        cname = creator_rt[0].get('plain_text', '') if creator_rt else ''
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m = re.search(r'Videos:\s*(\d+)', notes)
        vc = int(m.group(1)) if m else 0
        link = props.get('Drive Link', {}).get('url') or ''
        m2 = re.search(r'/folders/([a-zA-Z0-9_-]+)', link)
        fid = m2.group(1) if m2 else ''
        rows[page['id']] = {
            'page_id': page['id'], 'client_name': cname, 'folder_name': fname,
            'video_count': vc, 'folder_id': fid,
        }
    return rows


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
    ap.add_argument('--live', action='store_true', help='Actually write. Default is dry-run.')
    args = ap.parse_args()

    config = load_config()
    token = config['notion_token']

    raw_rows = fetch_raw_rows(token)

    ok, missing = [], []
    for page_id, editor in PLAN.items():
        row = raw_rows.get(page_id)
        if not row or row['folder_name'] == '' or row['folder_id'] == '':
            missing.append((page_id, editor))
            continue
        ok.append((row, editor))

    print(f'Plan: {len(PLAN)} folders. Matched live Raw rows: {len(ok)}. Missing/changed: {len(missing)}.')
    if missing:
        for pid, ed in missing:
            print(f'  ! {pid} -> {ed}: no longer a matching Raw row (already assigned/removed?)')

    if not args.live:
        print('\nDry run — no writes. Re-run with --live to execute.')
        for row, editor in ok:
            print(f"  {row['client_name']:12} / {row['folder_name']:35} ({row['video_count']:>3}v) -> {editor}")
        return

    print()
    for row, editor in ok:
        label = f"{row['client_name']} / {row['folder_name']} ({row['video_count']}v) -> {editor}"
        patched = notion_patch(token, row['page_id'], {
            'Status': {'select': {'name': 'In Progress'}},
            'Editor': {'select': {'name': editor}},
        })
        if not patched:
            print(f'  SKIP (Notion patch failed): {label}')
            continue
        append_to_discord_queue({
            'client_name': row['client_name'],
            'folder_name': row['folder_name'],
            'video_count': row['video_count'],
            'folder_id': row['folder_id'],
            'editor_name': editor,
            'notion_queue_page_id': row['page_id'],
        })
        print(f'  OK: {label}')
        time.sleep(0.3)

    print(f'\nQueued {len(ok)} assignments for discord-bot to pick up (polls every 3s).')


if __name__ == '__main__':
    main()

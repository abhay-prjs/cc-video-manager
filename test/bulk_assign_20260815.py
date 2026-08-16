"""One-off: bulk-assign the 2026-08-15 pending-folder backlog (22 Raw Active
Queue folders) per the plan Vex approved in chat. Zyon is off today, excluded.
Gabo/Jewel excluded (already heavy at 128/187 videos). Load-balanced greedy
by video count against current In Progress totals (see chat).

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
import re
import time

import requests
from filelock import FileLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
QUEUE_LOCK = FileLock(QUEUE_FILE + '.lock')

# (client_name, folder_name) -> editor, matched against the live Raw pull
PLAN = {
    ('Jason', 'invo 5'): 'Steven',

    ('Aiden', 'phrasly 10'): 'Danie',
    ('Cat', 'Noiz 3'): 'Danie',
    ('Savera', 'dripwriter 02'): 'Danie',
    ('Henry', 'invo 9 8/14/26 (1vid)'): 'Danie',

    ('Ivan', 'Ape 4'): 'Naomi',
    ('Whitney', 'phrasly 34'): 'Naomi',
    ('Savera', 'phrasly 28'): 'Naomi',
    ('Joshua', 'invo 33'): 'Naomi',
    ('Savera', 'invo 05'): 'Naomi',

    ('Ivan', 'Ape 3'): 'Jill',
    ('Joshua', 'invo 35'): 'Jill',
    ('Aiden', 'phrasly 9'): 'Jill',
    ('Krish', 'Freebuff 1'): 'Jill',

    ('Ivan', 'Okara 4'): 'Storm',
    ('Savera', 'viewmax 13'): 'Storm',
    ('Savera', 'phrasly 27'): 'Storm',

    ('Joshua', 'invo 34'): 'Josh',

    ('Henry', 'invo 8 8/13/26 (2 vids)'): 'AJ',
    ('Henry', 'higgsfield 14 8/14/26 (1 vid)'): 'AJ',

    ('Konstantin', 'Invo 4'): 'Aki',
    ('Sam', 'invo 1'): 'Aki',
}


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
        rows[(cname, fname)] = {
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
    for key, editor in PLAN.items():
        row = raw_rows.get(key)
        if not row or row['folder_id'] == '':
            missing.append((key, editor))
            continue
        ok.append((row, editor))

    print(f'Plan: {len(PLAN)} folders. Matched live Raw rows: {len(ok)}. Missing/changed: {len(missing)}.')
    if missing:
        for key, ed in missing:
            print(f'  ! {key} -> {ed}: no longer a matching Raw row (already assigned/removed?)')

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

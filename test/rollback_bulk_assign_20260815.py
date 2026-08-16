"""One-off: fully undo test/bulk_assign_20260815.py (the 20-folder batch
assigned this session, matched to the correct page_ids via editor+video_count
to avoid a duplicate-title collision on "Ivan / Ape 4"). Vex flagged a mistake
and asked to retract quietly — no new notifications to editors or creators.

Steps per folder:
  1. Notion PATCH: Status -> Raw, Editor cleared.
  2. Delete the assignment Discord message sent to the editor's channel
     (assignment_messages.json has message_id/channel_id), then drop that
     entry from assignment_messages.json.
  3. Drop the deadlines.json entry (pending_start state assign_folder created).
  4. Recalculate each affected editor's Active Videos in Editor Profiles.

Does NOT touch pending_ops_assigns.json / the #assignments ops-assign
messages — those still show the original "New Folder — Assign Editor"
dropdown (11 were mid-edit to "Assigned" before being stopped; a separate
step restores those).
"""
import json
import os
import time

import requests
from filelock import FileLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
ASSIGNMENT_MESSAGES_FILE = os.path.join(BASE_DIR, 'assignment_messages.json')
DEADLINES_FILE = os.path.join(BASE_DIR, 'deadlines.json')
ASSIGNMENT_MESSAGES_LOCK = FileLock(ASSIGNMENT_MESSAGES_FILE + '.lock')
DEADLINES_LOCK = FileLock(DEADLINES_FILE + '.lock')

with open('/tmp/rollback_20260815.json') as f:
    ROWS = json.load(f)


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

    profiles_url = 'https://api.notion.com/v1/databases/a18d5c16-f359-4a2b-a620-6c837aa04232/query'
    presp = requests.post(profiles_url, headers=notion_headers(token),
                           json={'filter': {'property': 'Editor', 'title': {'equals': editor_name}}}, timeout=15)
    if not presp.ok or not presp.json().get('results'):
        print(f'  ! recalc: could not find Editor Profiles row for {editor_name}')
        return total
    page = presp.json()['results'][0]
    page_id = page['id']
    capacity = page['properties'].get('Capacity', {}).get('number') or 0
    ratio = total / capacity if capacity else 0
    status = 'Overloaded' if ratio >= 0.85 else 'Busy' if ratio >= 0.6 else 'Available'
    notion_patch(token, page_id, {
        'Active Videos': {'number': total},
        'Status': {'select': {'name': status}},
    })
    return total


def main():
    config = load_config()
    token = config['notion_token']

    with ASSIGNMENT_MESSAGES_LOCK:
        with open(ASSIGNMENT_MESSAGES_FILE) as f:
            assignment_messages = json.load(f)

    with DEADLINES_LOCK:
        with open(DEADLINES_FILE) as f:
            deadlines = json.load(f)

    bot_token = config['discord_bot_token']
    dheaders = {
        'Authorization': f'Bot {bot_token}',
        'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
    }

    editors_touched = set()

    for row in ROWS:
        label = f"{row['client']} / {row['folder']} ({row['video_count']}v) [{row['editor']}]"
        page_id = row['page_id']
        folder_id = row['folder_id']
        editors_touched.add(row['editor'])

        ok = notion_patch(token, page_id, {
            'Status': {'select': {'name': 'Raw'}},
            'Editor': {'select': None},
        })
        print(f'{"OK " if ok else "FAIL"} Notion revert: {label}')

        msg_rec = assignment_messages.get(folder_id)
        if msg_rec:
            msg_id = msg_rec.get('message_id')
            ch_id = msg_rec.get('channel_id')
            if msg_id and ch_id:
                url = f'https://discord.com/api/v10/channels/{ch_id}/messages/{msg_id}'
                r = requests.delete(url, headers=dheaders, timeout=15)
                if r.ok or r.status_code == 404:
                    print(f'  OK deleted assignment message for {label}')
                else:
                    print(f'  ! delete assignment message failed for {label}: {r.status_code} {r.text[:150]}')
            del assignment_messages[folder_id]
        else:
            print(f'  ! no assignment_messages.json entry for {label}')

        if folder_id in deadlines:
            del deadlines[folder_id]

        time.sleep(1.5)

    with ASSIGNMENT_MESSAGES_LOCK:
        with open(ASSIGNMENT_MESSAGES_FILE, 'w') as f:
            json.dump(assignment_messages, f, indent=2)

    with DEADLINES_LOCK:
        with open(DEADLINES_FILE, 'w') as f:
            json.dump(deadlines, f, indent=2)

    print('\nRecalculating active videos for affected editors...')
    for ed in sorted(editors_touched):
        total = recalculate_active_videos(token, ed)
        print(f'  {ed}: active videos now {total}')


if __name__ == '__main__':
    main()

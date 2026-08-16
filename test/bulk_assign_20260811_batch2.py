"""One-off: bulk-assign the 10 Raw folders pending as of 2026-08-11 16:55 UTC
(the "batch2" set — Danie/Jewel/Naomi/AJ load-balance plan agreed with Vex).

Replicates AssignEditorSelect.callback() exactly, same pattern as bulk_assign_20260811.py:
  1. PATCH Notion (Status=In Progress, Editor=<editor>) — same as _assign_raw_to_editor()
  2. Enqueue a plain assign item to discord_queue.json — picked up by the live discord-bot
     process within 3s, which runs the real assign_folder() + creator_notify (deadline entry,
     Discord embed, dashboard bridge push).

video_count values below come from a fresh Notion Notes read (2026-08-11 16:55 UTC), NOT from
pending_ops_assigns.json's cached video_count — that field drifted stale for several of these
(e.g. Animate World cached as 1, actually 35; Sprout 10 cached as 8, actually 16) since the
initial detection push fired before the Drive scan finished counting.

Cleanup of the ops-assign Discord messages (pending_ops_assigns.json) is a separate pass run
after confirming the queue drained.
"""
import json
import os
import time
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')

config = json.load(open(CONFIG_FILE))
NOTION_TOKEN = config['notion_token']

ASSIGNMENTS = [
    # (message_id, editor, correct_video_count)
    ("1536530236789891184", "Danie", 1),   # Invo1 / Natashka
    ("1536582441370779690", "Danie", 35),  # Animate World / Cat
    ("1536546784439173271", "Jewel", 9),   # INVO / Cat
    ("1536712477298130984", "Jewel", 16),  # Sprout 10 / Si
    ("1536710497120817193", "Jewel", 10),  # phrasly 14 / Claudia
    ("1536627910138531973", "Naomi", 21),  # Krea 2 / Chris Lam
    ("1536629906060877987", "AJ", 5),      # viewmax 2 / Sam
    ("1536673777038008432", "AJ", 5),      # Phrasly / Chiara
    ("1536574922758029364", "AJ", 1),      # higgsfield 10 8/10/26 (1 vid) / Henry
    ("1536606759056183347", "AJ", 5),      # INVO 1 / Sam Michigan
]

pending = json.load(open(os.path.join(BASE_DIR, 'pending_ops_assigns.json')))


def notion_patch(page_id, properties):
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers={
            'Authorization': f'Bearer {NOTION_TOKEN}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json',
        },
        json={'properties': properties},
        timeout=15,
    )
    if not resp.ok:
        print(f'  NOTION PATCH FAILED {page_id}: {resp.status_code} {resp.text}')
    return resp.ok


def enqueue(item):
    with open(QUEUE_FILE) as f:
        queue = json.load(f)
    queue.append(item)
    with open(QUEUE_FILE, 'w') as f:
        json.dump(queue, f, indent=2)


for msg_id, editor, video_count in ASSIGNMENTS:
    item = pending[msg_id]
    folder_id = item['folder_id']
    notion_page_id = item['notion_page_id']
    client_name = item['client_name']
    folder_name = item['folder_name']
    project_number = item.get('project_number', '')

    print(f'{client_name} / {folder_name} ({video_count} vids) -> {editor}')

    ok = notion_patch(notion_page_id, {
        'Status': {'select': {'name': 'In Progress'}},
        'Editor': {'select': {'name': editor}},
    })
    if not ok:
        print('  skipping enqueue due to Notion PATCH failure')
        continue

    enqueue({
        'client_name': client_name,
        'folder_name': folder_name,
        'video_count': video_count,
        'folder_id': folder_id,
        'editor_name': editor,
        'notion_queue_page_id': notion_page_id,
        'project_number': project_number,
    })
    time.sleep(0.5)

print('Done. discord-bot will drain the queue within ~3s.')

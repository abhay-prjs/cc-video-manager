"""One-off: assign the 3 oldest Raw folders as of 2026-08-14 to Jill per Vex's request.
Same flow as assign_jill_20260811.py:
  1. PATCH Notion (Status=In Progress, Editor=Jill) — same as _assign_raw_to_editor()
  2. Enqueue a plain assign item to discord_queue.json — picked up by the live discord-bot
     process within 3s, which runs the real assign_folder() + creator_notify (deadline entry,
     Discord embed, dashboard bridge push — all the state a dropdown click would produce).
Does NOT touch pending_ops_assigns.json / edit the ops-assign messages — that's done in a
separate cleanup pass after confirming the queue drained.

Note: Jill was at 67/70 capacity (Active Videos) when this ran. These 3 folders are 30
videos total (10 each), so this intentionally pushes her over capacity — confirmed with
Vex before running (she asked for the oldest 3 Raw folders specifically, capacity warning
acknowledged).
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

EDITOR = 'Jill'
MSG_IDS = [
    "1537341620280758333",  # Okara 7 / Ivan
    "1537341651364610110",  # Okara 6 / Ivan
    "1537341672994775101",  # Okara 5 / Ivan
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


for msg_id in MSG_IDS:
    item = pending[msg_id]
    folder_id = item['folder_id']
    notion_page_id = item['notion_page_id']
    client_name = item['client_name']
    folder_name = item['folder_name']
    video_count = item['video_count']
    project_number = item.get('project_number', '')

    print(f'{client_name} / {folder_name} -> {EDITOR}')

    ok = notion_patch(notion_page_id, {
        'Status': {'select': {'name': 'In Progress'}},
        'Editor': {'select': {'name': EDITOR}},
    })
    if not ok:
        print('  skipping enqueue due to Notion PATCH failure')
        continue

    enqueue({
        'client_name': client_name,
        'folder_name': folder_name,
        'video_count': video_count,
        'folder_id': folder_id,
        'editor_name': EDITOR,
        'notion_queue_page_id': notion_page_id,
        'project_number': project_number,
    })
    time.sleep(0.5)

print('Done. discord-bot will drain the queue within ~3s.')

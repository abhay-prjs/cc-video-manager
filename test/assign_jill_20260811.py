"""One-off: assign the 5 oldest Raw folders as of 2026-08-11 to Jill per Vex's request.
Same flow as bulk_assign_20260811.py:
  1. PATCH Notion (Status=In Progress, Editor=Jill) — same as _assign_raw_to_editor()
  2. Enqueue a plain assign item to discord_queue.json — picked up by the live discord-bot
     process within 3s, which runs the real assign_folder() + creator_notify (deadline entry,
     Discord embed, dashboard bridge push — all the state a dropdown click would produce).
Does NOT touch pending_ops_assigns.json / edit the ops-assign messages — that's done in a
separate cleanup pass (assign_jill_cleanup_20260811.py) after confirming the queue drained.
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
    "1536484421895462975",  # Phrasly 1 / Natashka
    "1536484381701447803",  # manus 1 8/10/26 (3vids) / Henry
    "1536487425147277333",  # Higgsfield 1 / Natashka
    "1536490869803319390",  # phrasly batch 21 / Joshua
    "1536520631536062549",  # Composio raw / Reuben
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

"""One-off: assign the oldest remaining Raw folder (Invo / Shindy) to Gabo per Vex's request,
despite Gabo showing Overloaded status. Same flow as bulk_assign_20260811.py:
  1. PATCH Notion (Status=In Progress, Editor=Gabo)
  2. Enqueue plain item to discord_queue.json for the live discord-bot to process
     (real assign_folder() + creator_notify).
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

MSG_ID = "1536483405926240357"  # Invo / Shindy
EDITOR = "Gabo"

pending = json.load(open(os.path.join(BASE_DIR, 'pending_ops_assigns.json')))
item = pending[MSG_ID]

folder_id = item['folder_id']
notion_page_id = item['notion_page_id']
client_name = item['client_name']
folder_name = item['folder_name']
video_count = item['video_count']
project_number = item.get('project_number', '')

print(f'{client_name} / {folder_name} -> {EDITOR}')

resp = requests.patch(
    f'https://api.notion.com/v1/pages/{notion_page_id}',
    headers={
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    },
    json={'properties': {
        'Status': {'select': {'name': 'In Progress'}},
        'Editor': {'select': {'name': EDITOR}},
    }},
    timeout=15,
)
if not resp.ok:
    print(f'  NOTION PATCH FAILED: {resp.status_code} {resp.text}')
    raise SystemExit(1)

with open(QUEUE_FILE) as f:
    queue = json.load(f)
queue.append({
    'client_name': client_name,
    'folder_name': folder_name,
    'video_count': video_count,
    'folder_id': folder_id,
    'editor_name': EDITOR,
    'notion_queue_page_id': notion_page_id,
    'project_number': project_number,
})
with open(QUEUE_FILE, 'w') as f:
    json.dump(queue, f, indent=2)

print('Done. discord-bot will drain the queue within ~3s.')

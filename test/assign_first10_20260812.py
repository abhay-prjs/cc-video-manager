"""One-off: assign the first 10 (oldest) Raw folders as of 2026-08-12.
Josh excluded (off today). Distributed across lightest-loaded non-overloaded
editors (Steven, Storm, Jill, Jewel, Aki, Naomi, Danie); AJ/Zyon/Gabo skipped
(already Overloaded status in Editor Profiles).

Replicates AssignEditorSelect.callback():
  1. PATCH Notion (Status=In Progress, Editor=<editor>) — same as _assign_raw_to_editor()
  2. Enqueue a plain assign item to discord_queue.json — picked up by the live discord-bot
     process within 3s, which runs the real assign_folder() + creator_notify (deadline entry,
     Discord embed, dashboard bridge push — all the state a dropdown click would produce).
Does NOT touch pending_ops_assigns.json / edit the ops-assign messages — that's a separate
cleanup pass, run after confirming the queue drained.
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
    # (message_id, editor)
    ("1536795500957933629", "Steven"),  # composio 8 / Joshua
    ("1536810672539828365", "Storm"),   # Assets / Raden
    ("1536821175240626216", "Jill"),    # Launchpoint 10 / Ivan
    ("1536821192680411239", "Jewel"),   # Invo 1 / Ivan
    ("1536821223114285107", "Aki"),     # Ape 1 / Ivan
    ("1536835972220850217", "Naomi"),   # Lovable 2 / Konstantin
    ("1536843788553687161", "Danie"),   # viewmax 3 / Sam
    ("1536852836061814915", "Steven"),  # Coderabbit 5 / Raden
    ("1536862919676141731", "Storm"),   # INVO 2 / Cat
    ("1536864465503653929", "Jill"),    # phrasly 15 / Jauseff
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


for msg_id, editor in ASSIGNMENTS:
    item = pending[msg_id]
    folder_id = item['folder_id']
    notion_page_id = item['notion_page_id']
    client_name = item['client_name']
    folder_name = item['folder_name']
    video_count = item['video_count']
    project_number = item.get('project_number', '')

    print(f'{client_name} / {folder_name} -> {editor}')

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

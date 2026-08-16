"""One-off: assign Krish / Phrasly 1 (Drive/Notion Raw, Notion page
3bce637c-317d-81ac-a482-f66cfcde546e, folder_id 1t1U9t-Ob_H-_-07WqSAauxDO4vxNoe0f,
4 videos, Project #1230) to Jill. First of a small batch on 2026-08-14; Steven
excluded from the wider assignment round per Vex's instruction.
"""
import json
import os
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')

config = json.load(open(CONFIG_FILE))
NOTION_TOKEN = config['notion_token']

PAGE_ID = '3bce637c-317d-81ac-a482-f66cfcde546e'
FOLDER_ID = '1t1U9t-Ob_H-_-07WqSAauxDO4vxNoe0f'
CLIENT = 'Krish'
FOLDER = 'Phrasly 1'
VIDEOS = 4
PROJECT = 1230
EDITOR = 'Jill'


def notion_patch(page_id, properties):
    req = urllib.request.Request(
        f'https://api.notion.com/v1/pages/{page_id}',
        data=json.dumps({'properties': properties}).encode(),
        headers={
            'Authorization': f'Bearer {NOTION_TOKEN}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json',
        },
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        print(f'  NOTION PATCH FAILED {page_id}: {e.code} {e.read()}')
        return False


def enqueue(item):
    with open(QUEUE_FILE) as f:
        queue = json.load(f)
    queue.append(item)
    with open(QUEUE_FILE, 'w') as f:
        json.dump(queue, f, indent=2)


print(f'{CLIENT} / {FOLDER} -> {EDITOR}')
ok = notion_patch(PAGE_ID, {
    'Status': {'select': {'name': 'In Progress'}},
    'Editor': {'select': {'name': EDITOR}},
})
if not ok:
    print('  Notion PATCH failed, not enqueueing')
else:
    enqueue({
        'client_name': CLIENT,
        'folder_name': FOLDER,
        'video_count': VIDEOS,
        'folder_id': FOLDER_ID,
        'editor_name': EDITOR,
        'notion_queue_page_id': PAGE_ID,
        'project_number': PROJECT,
    })
    time.sleep(0.5)
    print('Done — queued for discord-bot to pick up (~3s).')

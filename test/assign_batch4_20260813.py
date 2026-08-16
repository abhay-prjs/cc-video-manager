"""One-off: assign the next 5 oldest unassigned items (Drive + website-native)
as of 2026-08-13 evening, load-balanced by current folder count across the
full active roster (no exclusions this round). Follows the same pattern as
assign_batch3_20260813.py.
"""
import json
import os
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')

config = json.load(open(CONFIG_FILE))
NOTION_TOKEN = config['notion_token']
DASHBOARD_URL = config.get('dashboard_url')
DASHBOARD_SECRET = config.get('dashboard_secret')
BOT_TOKEN = config['discord_bot_token']

UA = {'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)'}


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


def fetch_editor_discord_id(editor_name):
    req = urllib.request.Request(
        'https://api.notion.com/v1/databases/a18d5c16-f359-4a2b-a620-6c837aa04232/query',
        data=json.dumps({'page_size': 100}).encode(),
        headers={
            'Authorization': f'Bearer {NOTION_TOKEN}',
            'Notion-Version': '2022-06-28',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    for page in data['results']:
        p = page['properties']
        name_rt = p.get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else ''
        if name == editor_name:
            uid_rt = p.get('Discord User ID', {}).get('rich_text', [])
            return uid_rt[0].get('plain_text', '') if uid_rt else ''
    return ''


def post_dashboard_assignment(payload):
    if not DASHBOARD_URL or not DASHBOARD_SECRET:
        print('  dashboard_url/secret not configured — skipping dashboard POST')
        return
    req = urllib.request.Request(
        DASHBOARD_URL,
        data=json.dumps(payload).encode(),
        headers={**UA, 'Authorization': f'Bearer {DASHBOARD_SECRET}', 'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f'  dashboard assign POST -> {resp.status}')
    except urllib.error.HTTPError as e:
        print(f'  dashboard assign POST FAILED: {e.code} {e.read()}')


def patch_discord_message(channel_id, msg_id, editor, client, folder):
    payload = {
        'embeds': [{
            'title': f'✅ Assigned to {editor}',
            'color': 0x2ecc71,
            'fields': [
                {'name': 'Brand', 'value': client, 'inline': True},
                {'name': 'Batch', 'value': folder, 'inline': True},
            ],
        }],
        'components': [],
    }
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}'
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={**UA, 'Authorization': f'Bot {BOT_TOKEN}', 'Content-Type': 'application/json'},
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f'  discord message patch -> {resp.status}')
    except urllib.error.HTTPError as e:
        print(f'  discord message patch FAILED: {e.code} {e.read()}')


DRIVE_ASSIGNMENTS = [
    # (notion_page_id, folder_id, client_name, folder_name, video_count, project_number, editor)
    ('3bae637c-317d-8177-a3a1-fa765e37387b', '1m8_1OnkCz_jHgP2auwV9LshmVE6BgTtF',
     'Zay', 'Asmi 1', 4, 1184, 'AJ'),
    ('3bae637c-317d-8106-8e83-e14fa45d7177', '1_8t7xtHXGLwbQsb4ldYux0htRtXw_6lq',
     'Michael', 'Composio 07', 18, 1185, 'Gabo'),
    ('3bae637c-317d-813f-9f32-d88eaab19269', '1fOF6O4pu7_kI8K1duJ0-lCLHs4ppMhkP',
     'Mina', 'airlearn 1 aug 11 script', 7, 1186, 'AJ'),
]

WEB_ASSIGNMENTS = [
    # (msg_id, ticket_id, student_name, folder_name, video_count, editor)
    ('1537197208078651403', '8966f1c4-2c8d-4d7b-8574-3722eeda553f', 'Kai Gangi',
     'Mosaic — hook + demo ×20', 74, 'Jewel'),
    ('1537222194164273208', 'a534b7b2-cd33-4620-af28-b1ca9f62bbe3', 'Chris Harms-Haltiner',
     'Higgsfield — talking head ×1', 1, 'Danie'),
]

CHANNEL_ID = '1516469364260339842'

pending = json.load(open(PENDING_FILE))
editor_uid_cache = {}

print('=== Drive folders ===')
for page_id, folder_id, client, folder, vids, proj, editor in DRIVE_ASSIGNMENTS:
    print(f'{client} / {folder} -> {editor}')
    ok = notion_patch(page_id, {
        'Status': {'select': {'name': 'In Progress'}},
        'Editor': {'select': {'name': editor}},
    })
    if not ok:
        print('  skipping enqueue due to Notion PATCH failure')
        continue
    enqueue({
        'client_name': client,
        'folder_name': folder,
        'video_count': vids,
        'folder_id': folder_id,
        'editor_name': editor,
        'notion_queue_page_id': page_id,
        'project_number': proj,
    })
    time.sleep(0.5)

print()
print('=== Website batches ===')
for msg_id, ticket_id, student, folder, vids, editor in WEB_ASSIGNMENTS:
    print(f'{student} / {folder} -> {editor}')
    if editor not in editor_uid_cache:
        editor_uid_cache[editor] = fetch_editor_discord_id(editor)
    uid = editor_uid_cache[editor]

    post_dashboard_assignment({
        'ticket_id': ticket_id,
        'creator_name': student,
        'folder_name': folder,
        'editor_name': editor,
        'editor_discord_id': uid,
        'video_count': vids,
    })
    patch_discord_message(CHANNEL_ID, msg_id, editor, student, folder)
    pending.pop(msg_id, None)
    time.sleep(12)

with open(PENDING_FILE, 'w') as f:
    json.dump(pending, f, indent=2)

print()
print('Done. Drive assignments queued for discord-bot (~3s); website assignments posted directly.')

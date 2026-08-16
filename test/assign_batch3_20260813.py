"""One-off: assign the first 10 oldest unassigned items (Drive + website-native
batches combined) as of 2026-08-13, per Vex's load-balance-by-folder-count call
(Aki/Naomi/Storm excluded from the pool for this batch). Airlearn duplicates
intentionally left untouched.

Drive folders: PATCH Notion (Status=In Progress, Editor=<editor>) then enqueue
a plain assign item to discord_queue.json, same as bulk_assign_20260811.py —
picked up by the live discord-bot within ~3s, which runs assign_folder() +
creator_notify.

Website batches: POST straight to the dashboard assign endpoint (same shape
DashboardAssignSelect.callback uses), then patch the #assignments dropdown
message to a resolved state and drop it from pending_ops_assigns.json —
mirrors what discord_bot.py's own website-assign path does, reimplemented
here per the test/ convention (no importing the ~7000-line live-bot module).
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
    ('3bae637c-317d-8165-9d22-df54dc381f05', '1VPv6QerW2XE4NpIadh3tG4n2uFFbCsNS',
     'Henry', 'higgsfield 11 8/12/26 (1vid)', 6, 1182, 'Jill'),
    ('3bae637c-317d-810a-8765-e9203158e821', '1ip-PW2neLXcgO7sdakCHuRTctn3KLnso',
     'Henry', 'phrasly 47 8/12/26 (4 vids)', 4, 1183, 'AJ'),
]

WEB_ASSIGNMENTS = [
    # (msg_id, ticket_id, student_name, folder_name, video_count, editor)
    ('1536744241487814657', '356c2af0-0882-48fc-8a8c-751cd2fa6e72', 'Patrick Poon',
     'Asmi — green screen ×6', 24, 'AJ'),
    ('1536763001468751954', 'af436456-c822-4d45-9c53-9a04201ab366', 'Patrick Poon',
     'Modo Casino — hook + demo ×21', 84, 'Jewel'),
    ('1536948951180382220', 'afa98788-76be-4d54-a39a-9c61b033b02d', 'Oliver',
     'Composio — hook + demo ×9, talking head ×2', 11, 'Jill'),
    ('1536957714352312371', '603acc3e-38e2-49c8-9830-cc9be9162572', 'Oliver',
     'Composio — talking head ×3, hook + demo ×1', 4, 'AJ'),
    ('1536978633753763841', 'f4f204db-8953-4444-911c-d365ce452971', 'Jackie Zhang',
     'Phrasly — hook + demo ×1', 1, 'Steven'),
    ('1537000891012354129', 'a9ed3e17-0d28-4a14-8674-fea24fdc31d9', 'Jackie Zhang',
     'Phrasly — hook + demo ×4', 4, 'Danie'),
    ('1537020255224729623', '212de800-c1c9-4b51-8ec6-97aa05733705', 'Jackie Zhang',
     'Phrasly — hook + demo ×4', 4, 'Josh'),
    ('1537102983286759475', '29a9b794-f83b-48c9-8d68-e4dc70597b12', 'Oliver',
     'Composio — talking head ×6', 24, 'Jewel'),
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
    time.sleep(12)  # Discord edit rate-limit gap, same as bulk cleanup convention

with open(PENDING_FILE, 'w') as f:
    json.dump(pending, f, indent=2)

print()
print('Done. Drive assignments queued for discord-bot (~3s); website assignments posted directly.')

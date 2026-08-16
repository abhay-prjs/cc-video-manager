"""Cleanup pass for assign_first10_20260812.py: mark the 10 ops-assign Discord messages
as assigned (green embed, dropdown removed) and drop them from pending_ops_assigns.json.
Follows the documented bulk-assign cleanup convention (User-Agent required, 12s gaps
between edits to avoid Discord 429s).
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')

config = json.load(open(CONFIG_FILE))
BOT_TOKEN = config['discord_bot_token']
UA = 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)'

ASSIGNMENTS = [
    ("1536795500957933629", "composio 8", "Joshua", "Steven"),
    ("1536810672539828365", "Assets", "Raden", "Storm"),
    ("1536821175240626216", "Launchpoint 10", "Ivan", "Jill"),
    ("1536821192680411239", "Invo 1", "Ivan", "Jewel"),
    ("1536821223114285107", "Ape 1", "Ivan", "Aki"),
    ("1536835972220850217", "Lovable 2", "Konstantin", "Naomi"),
    ("1536843788553687161", "viewmax 3", "Sam", "Danie"),
    ("1536852836061814915", "Coderabbit 5", "Raden", "Steven"),
    ("1536862919676141731", "INVO 2", "Cat", "Storm"),
    ("1536864465503653929", "phrasly 15", "Jauseff", "Jill"),
]

pending = json.load(open(PENDING_FILE))


def edit_message(channel_id, msg_id, folder_name, client_name, editor):
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}'
    payload = {
        "embeds": [{
            "title": "✅ Assigned",
            "description": f"**{folder_name}** ({client_name}) → **{editor}**",
            "color": 0x2ecc71,
        }],
        "components": [],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f'Bot {BOT_TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': UA,
        },
        method='PATCH',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        print(f'  DISCORD PATCH FAILED {msg_id}: {e.code} {e.read()}')
        return None


for i, (msg_id, folder_name, client_name, editor) in enumerate(ASSIGNMENTS):
    item = pending.get(msg_id)
    if not item:
        print(f'{msg_id} ({folder_name}) not in pending_ops_assigns.json, skipping')
        continue
    channel_id = item['channel_id']
    status = edit_message(channel_id, msg_id, folder_name, client_name, editor)
    print(f'{folder_name} -> edit status {status}')
    if status in (200, 204):
        del pending[msg_id]
        with open(PENDING_FILE, 'w') as f:
            json.dump(pending, f, indent=2)
    if i < len(ASSIGNMENTS) - 1:
        time.sleep(12)

print('Cleanup done.')

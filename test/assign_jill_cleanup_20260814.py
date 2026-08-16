"""Cleanup pass for assign_jill_oldest3_20260814.py: mark the 3 ops-assign Discord messages
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
    ("1537341620280758333", "Okara 7", "Ivan", "Jill"),
    ("1537341651364610110", "Okara 6", "Ivan", "Jill"),
    ("1537341672994775101", "Okara 5", "Ivan", "Jill"),
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

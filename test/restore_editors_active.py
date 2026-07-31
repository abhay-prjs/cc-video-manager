#!/usr/bin/env python3
"""One-shot: re-check Active for editors taken off the roster for the day.

Scheduled via `systemd-run --user --on-calendar` for midnight IST 2026-07-05→06
(Jake + Anne off for the day, per Vex). Safe to re-run: PATCHing an already-true
checkbox is a no-op.
"""
import json
import requests

BASE = '/home/ubuntu/gdrive_watcher'
EDITORS = {
    'Jake': '390e637c-317d-803e-a0b7-dc4dcdfa1ccb',
    'Anne': '356e637c-317d-80d2-8d37-ef176e720f54',
}
OPS_CHANNEL = '1516469364260339842'

cfg = json.load(open(f'{BASE}/config.json'))
nh = {'Authorization': f"Bearer {cfg['notion_token']}",
      'Notion-Version': '2022-06-28', 'Content-Type': 'application/json'}

restored = []
for name, pid in EDITORS.items():
    r = requests.patch(f'https://api.notion.com/v1/pages/{pid}', headers=nh,
                       json={'properties': {'Active': {'checkbox': True}}}, timeout=15)
    if r.ok:
        restored.append(name)
    else:
        print(f'FAILED {name}: {r.status_code} {r.text[:200]}')

if restored:
    dh = {'Authorization': f"Bot {cfg['discord_bot_token']}", 'Content-Type': 'application/json'}
    msg = ('🌅 ' + ' and '.join(restored) +
           ' are back on the assignable roster (day-off flag from 2026-07-05 cleared automatically).')
    requests.post(f'https://discord.com/api/v10/channels/{OPS_CHANNEL}/messages',
                  headers=dh, json={'content': msg}, timeout=15)
    print('Restored:', ', '.join(restored))

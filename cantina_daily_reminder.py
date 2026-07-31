#!/usr/bin/env python3
"""Daily reminder to ops channel: Angello's Cantina project needs 10 videos/day.

Cron: daily at 03:30 UTC (9AM IST), alongside daily_digest.
Stops itself once the Active Queue page leaves In Progress (delivered/removed).
Remove the cron entry once the project wraps.
"""
import json
import os
import sys

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
PAGE_ID = '391e637c-317d-81f1-ae86-c4a159e3f82d'  # Angello / Cantina
TOTAL_VIDEOS = 70
DAILY_TARGET = 10

cfg = json.load(open(os.path.join(BASE, 'config.json')))
ntok = cfg.get('notion_token') or cfg.get('notion_api_key')

r = requests.get(
    f'https://api.notion.com/v1/pages/{PAGE_ID}',
    headers={'Authorization': f'Bearer {ntok}', 'Notion-Version': '2022-06-28'},
    timeout=30,
)
r.raise_for_status()
props = r.json()['properties']
status = (props['Status']['select'] or {}).get('name', '')
editor = (props['Editor']['select'] or {}).get('name', '?')
if status != 'In Progress':
    print(f'Cantina status is {status!r} — no reminder sent. Remove this cron entry.')
    sys.exit(0)

delivered = props.get('Videos Completed', {}).get('number') or 0
msg = {
    'embeds': [{
        'title': '🌮 Cantina daily pace check',
        'color': 0xe67e22,
        'description': (
            f'**Angello / Cantina** — {editor} needs to deliver '
            f'**{DAILY_TARGET} videos today** ({TOTAL_VIDEOS} total scope).\n'
            f'Videos Completed so far (Notion): **{delivered}**'
        ),
    }]
}
resp = requests.post(
    f"https://discord.com/api/v10/channels/{cfg['ops_channel_id']}/messages",
    headers={'Authorization': f"Bot {cfg['discord_bot_token']}",
             'Content-Type': 'application/json'},
    json=msg, timeout=30,
)
print('reminder sent:', resp.status_code)

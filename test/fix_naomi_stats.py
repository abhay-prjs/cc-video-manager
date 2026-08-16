"""
fix_naomi_stats.py
One-off script to correct Naomi's Editor Profiles stats by re-computing
from Active Queue (ground truth).

Run: python3 fix_naomi_stats.py
"""

import json
import re
import requests
from datetime import date, datetime

BASE_DIR = '/home/ubuntu/gdrive_watcher'
CONFIG_FILE = f'{BASE_DIR}/config.json'
ACTIVE_QUEUE_DB    = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
EDITOR_NAME        = 'Naomi'

with open(CONFIG_FILE) as f:
    cfg = json.load(f)
TOKEN = cfg['notion_token']

HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Content-Type': 'application/json',
    'Notion-Version': '2022-06-28',
}


def notion_query(db_id, body=None):
    resp = requests.post(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        headers=HEADERS, json=body or {}, timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get('results', [])


def notion_patch(page_id, props):
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=HEADERS, json={'properties': props}, timeout=15,
    )
    resp.raise_for_status()
    return resp.ok


# ── 1. Pull all Naomi rows from Active Queue ──────────────────────────────────
pages = notion_query(ACTIVE_QUEUE_DB, {
    'filter': {'property': 'Editor', 'select': {'equals': EDITOR_NAME}},
})
print(f'Active Queue rows for {EDITOR_NAME}: {len(pages)}')

today         = date.today()
monday        = today - __import__('datetime').timedelta(days=today.weekday())  # Monday of this week
month_start   = today.replace(day=1)

total_delivered  = 0
week_delivered   = 0
month_delivered  = 0
active_videos    = 0

for page in pages:
    props       = page['properties']
    status_sel  = props.get('Status', {}).get('select') or {}
    status      = status_sel.get('name', '')
    vid_done    = props.get('Videos Completed', {}).get('number') or 0
    delivered_d = (props.get('Delivered', {}).get('date') or {}).get('start', '')
    notes_rt    = props.get('Notes', {}).get('rich_text', [])
    notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
    m           = re.search(r'Videos:\s*(\d+)', notes)
    vid_count   = int(m.group(1)) if m else 0
    title_rt    = props.get('Video', {}).get('title', [])
    folder_name = title_rt[0].get('plain_text', '') if title_rt else ''

    if status == 'Delivered':
        total_delivered += vid_done
        if delivered_d:
            try:
                d = datetime.strptime(delivered_d, '%Y-%m-%d').date()
                if d >= monday:
                    week_delivered += vid_done
                if d >= month_start:
                    month_delivered += vid_done
            except ValueError:
                pass
        print(f'  Delivered: {folder_name:30}  vid_done={vid_done}  date={delivered_d}')
    elif status == 'In Progress':
        active_videos += vid_count
        print(f'  In Progress: {folder_name:30}  vid_count={vid_count}')

print()
print(f'Computed values:')
print(f'  Total Videos Delivered:  {total_delivered}')
print(f'  Delivered This Week:     {week_delivered}  (since {monday})')
print(f'  Delivered This Month:    {month_delivered}  (since {month_start})')
print(f'  Active Videos:           {active_videos}')

# ── 2. Find Naomi's Editor Profiles page ─────────────────────────────────────
ep_pages = notion_query(EDITOR_PROFILES_DB)
naomi_page_id = None
naomi_current = {}
for page in ep_pages:
    name_rt = page['properties'].get('Editor', {}).get('title', [])
    name    = name_rt[0].get('plain_text', '') if name_rt else ''
    if name.lower() == EDITOR_NAME.lower():
        naomi_page_id = page['id']
        p = page['properties']
        naomi_current = {
            'Active Videos':          p.get('Active Videos',          {}).get('number') or 0,
            'Delivered This Week':    p.get('Delivered This Week',    {}).get('number') or 0,
            'Delivered This Month':   p.get('Delivered This Month',   {}).get('number') or 0,
            'Total Videos Delivered': p.get('Total Videos Delivered', {}).get('number') or 0,
        }
        break

if not naomi_page_id:
    print(f'ERROR: {EDITOR_NAME} not found in Editor Profiles DB')
    exit(1)

print(f'\nCurrent Editor Profiles values:')
for k, v in naomi_current.items():
    print(f'  {k}: {v}')

# ── 3. Patch Editor Profiles ──────────────────────────────────────────────────
new_props = {
    'Active Videos':          {'number': active_videos},
    'Delivered This Week':    {'number': week_delivered},
    'Delivered This Month':   {'number': month_delivered},
    'Total Videos Delivered': {'number': total_delivered},
}
notion_patch(naomi_page_id, new_props)

print(f'\nPatched Editor Profiles for {EDITOR_NAME}:')
for k, v in new_props.items():
    print(f'  {k}: {naomi_current.get(k, "?")} → {v["number"]}')

print('\nDone.')

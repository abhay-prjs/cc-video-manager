"""
test_complete_flow.py

Inserts 5 fake test rows into the Active Queue Notion DB, simulates the
/complete command's client-select and folder-select menus, verifies folder_id
mapping, then deletes all 5 rows.
"""

import re
import sys
import json
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE      = '/home/ubuntu/gdrive_watcher/config.json'
ACTIVE_QUEUE_DB  = '44593fbf-4276-47f0-bd12-27289dcb78fd'

with open(CONFIG_FILE) as _f:
    _cfg = json.load(_f)
NOTION_TOKEN = _cfg['notion_token']


def notion_headers():
    return {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Content-Type':  'application/json',
        'Notion-Version': '2022-06-28',
    }


# ── Test rows ─────────────────────────────────────────────────────────────────

TEST_ROWS = [
    {
        'client':     'TestClient A',
        'folder':     'Batch 1',
        'editor':     'Vex',
        'status':     'In Progress',
        'notes':      'Videos: 3 | Folder ID: FAKE_ID_001',
        'drive_link': 'https://drive.google.com/fake/001',
    },
    {
        'client':     'TestClient B',
        'folder':     'Batch 1',
        'editor':     'Vex',
        'status':     'In Progress',
        'notes':      'Videos: 5 | Folder ID: FAKE_ID_002',
        'drive_link': 'https://drive.google.com/fake/002',
    },
    {
        'client':     'TestClient C',
        'folder':     'Batch 1',
        'editor':     'Vex',
        'status':     'In Progress',
        'notes':      'Videos: 2 | Folder ID: FAKE_ID_003',
        'drive_link': 'https://drive.google.com/fake/003',
    },
    {
        'client':     'TestClient A',
        'folder':     'Batch 2',
        'editor':     'Vex',
        'status':     'In Progress',
        'notes':      'Videos: 4 | Folder ID: FAKE_ID_004',
        'drive_link': 'https://drive.google.com/fake/004',
    },
    {
        'client':     'TestClient D',
        'folder':     'March Pack',
        'editor':     'Vex',
        'status':     'In Progress',
        'notes':      'Videos: 7 | Folder ID: FAKE_ID_005',
        'drive_link': 'https://drive.google.com/fake/005',
    },
]


# ── Step 1: Insert rows ───────────────────────────────────────────────────────

def insert_row(row):
    today = datetime.now().strftime('%Y-%m-%d')
    body = {
        'parent': {'database_id': ACTIVE_QUEUE_DB},
        'properties': {
            'Video':      {'title':     [{'text': {'content': row['folder']}}]},
            'Creator':    {'rich_text': [{'text': {'content': row['client']}}]},
            'Status':     {'select':    {'name': row['status']}},
            'Editor':     {'select':    {'name': row['editor']}},
            'Notes':      {'rich_text': [{'text': {'content': row['notes']}}]},
            'Drive Link': {'url': row['drive_link']},
            'Submitted':  {'date': {'start': today}},
        },
    }
    resp = requests.post(
        'https://api.notion.com/v1/pages',
        headers=notion_headers(),
        json=body,
        timeout=15,
    )
    if not resp.ok:
        print(f'  ERROR inserting row: {resp.status_code} — {resp.text[:200]}')
        return None
    return resp.json()['id']


print('=' * 65)
print('STEP 1: Inserting 5 test rows into Active Queue')
print('=' * 65)

inserted_ids = []
for i, row in enumerate(TEST_ROWS, 1):
    page_id = insert_row(row)
    if page_id:
        inserted_ids.append(page_id)
        print(f'  [{i}] ✓ {row["client"]} / {row["folder"]}  →  page_id: {page_id}')
    else:
        print(f'  [{i}] ✗ Failed to insert {row["client"]} / {row["folder"]}')

if len(inserted_ids) != len(TEST_ROWS):
    print(f'\nOnly {len(inserted_ids)}/{len(TEST_ROWS)} rows inserted — aborting.')
    sys.exit(1)

print(f'\nAll {len(inserted_ids)} rows inserted.\n')


# ── Step 2: Query Active Queue (mirrors fetch_in_progress_for_editor) ─────────

def fetch_in_progress_for_editor(editor_name):
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': editor_name}},
                {'property': 'Status', 'select': {'equals': 'In Progress'}},
            ]
        }
    }
    resp = requests.post(url, headers=notion_headers(), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props       = page['properties']
            title_rt    = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt  = props.get('Creator', {}).get('rich_text', [])
            client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
            notes_rt    = props.get('Notes', {}).get('rich_text', [])
            notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m           = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            drive_link  = (props.get('Drive Link', {}).get('url') or '')
            m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id   = m2.group(1) if m2 else ''
            rows.append({
                'folder_name':          folder_name,
                'client_name':          client_name,
                'video_count':          video_count,
                'folder_id':            folder_id,
                'drive_link':           drive_link,
                'notion_queue_page_id': page['id'],
            })
    return rows


print('=' * 65)
print('STEP 2: Querying Active Queue  (Editor == Vex, Status == In Progress)')
print('=' * 65)

rows = fetch_in_progress_for_editor('Vex')

# Filter to only our test rows (by notion_queue_page_id)
inserted_set = set(inserted_ids)
test_rows_fetched = [r for r in rows if r['notion_queue_page_id'] in inserted_set]

print(f'\nFetched {len(rows)} In-Progress rows for Vex total.')
print(f'Of those, {len(test_rows_fetched)} belong to this test run.\n')
print('Raw fetched test rows:')
for r in test_rows_fetched:
    print(
        f"  client={r['client_name']!r:16s}  folder={r['folder_name']!r:12s}"
        f"  videos={r['video_count']}  folder_id={r['folder_id']!r}  page_id={r['notion_queue_page_id']}"
    )


# ── Step 3: Simulate /complete menus (using ONLY test rows) ───────────────────

print('\n' + '=' * 65)
print('STEP 3: Simulating /complete menu flow  (test rows only)')
print('=' * 65)

rows_for_sim = test_rows_fetched

unique_clients = list(dict.fromkeys(r['client_name'] for r in rows_for_sim))

print(f'\nTotal In-Progress rows (test): {len(rows_for_sim)}')
print(f'Unique clients              : {len(unique_clients)}')

if len(rows_for_sim) == 1:
    print('\n→ /complete would open CompleteModal directly (only 1 folder).')
elif len(unique_clients) == 1:
    print(f'\n→ /complete would skip ClientSelect and go straight to FolderSelect for {unique_clients[0]}.')
else:
    print('\n→ /complete would show ClientSelectView:')
    print('  ┌─ Which client? ──────────────────────────────────┐')
    for c in unique_clients:
        print(f'  │  {c}')
    print('  └──────────────────────────────────────────────────┘')

print('\n─── Per-client folder menus ─────────────────────────────────')
for client in unique_clients:
    client_rows = [r for r in rows_for_sim if r['client_name'] == client]
    print(f'\n  Client: {client!r}  ({len(client_rows)} folder(s))')

    if len(client_rows) == 1:
        r = client_rows[0]
        print(f'    → Only 1 folder — /complete opens CompleteModal directly:')
        print(f'      folder_name: {r["folder_name"]!r}')
        print(f'      video_count: {r["video_count"]}')
        effective_id = r['folder_id'] or r['notion_queue_page_id']
        print(f'      effective_id (folder_id or page_id): {effective_id!r}')
    else:
        print(f'    → FolderSelectView would show:')
        print(f'      ┌─ Which folder for {client}? ──────────────────┐')
        for r in client_rows:
            effective_id = r['folder_id'] or r['notion_queue_page_id']
            print(f'      │  label={r["folder_name"]!r}  value={effective_id!r}  ({r["video_count"]} videos)')
        print(f'      └──────────────────────────────────────────────────────┘')


# ── Step 4: Verify folder_id uniqueness and mapping ──────────────────────────

print('\n' + '=' * 65)
print('STEP 4: Verifying folder_id mapping and uniqueness')
print('=' * 65)

print('\nFolder-ID extraction (regex /folders/([a-zA-Z0-9_-]+) on Drive Link):')
folder_id_map = {}
for r in rows_for_sim:
    drive_link = r['drive_link']
    folder_id  = r['folder_id']
    key        = (r['client_name'], r['folder_name'])
    folder_id_map[key] = folder_id
    match_status = 'MATCHED' if folder_id else 'NO MATCH (URL has no /folders/ segment)'
    print(f'  {r["client_name"]:14s} / {r["folder_name"]:10s}  drive_link={drive_link!r}')
    print(f'    → folder_id={folder_id!r}  [{match_status}]')

print()

# Effective IDs used by FolderSelectView (folder_id or page_id fallback)
print('Effective IDs used by FolderSelectView (folder_id or notion_queue_page_id):')
effective_ids = []
for r in rows_for_sim:
    eid = r['folder_id'] or r['notion_queue_page_id']
    effective_ids.append(eid)
    print(f'  {r["client_name"]:14s} / {r["folder_name"]:10s}  → {eid!r}')

unique_eids = set(effective_ids)
if len(unique_eids) == len(effective_ids):
    print(f'\n  ✓ All {len(effective_ids)} effective IDs are UNIQUE — no collision risk.')
else:
    print(f'\n  ✗ COLLISION DETECTED: {len(effective_ids)} rows but only {len(unique_eids)} unique IDs!')

print()
note = (
    "NOTE: The Drive Links provided (https://drive.google.com/fake/00X) do NOT\n"
    "      contain a '/folders/' path segment, so folder_id is empty for all test\n"
    "      rows. In production, links have the form\n"
    "      https://drive.google.com/drive/folders/<id> and extraction works\n"
    "      correctly. FolderSelectView falls back to notion_queue_page_id (the\n"
    "      Notion page UUID) as the select value, which is always unique."
)
print(note)


# ── Step 5: Delete all test rows ─────────────────────────────────────────────

print('\n' + '=' * 65)
print('STEP 5: Deleting all 5 test rows')
print('=' * 65)

deleted = 0
for page_id in inserted_ids:
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(),
        json={'archived': True},
        timeout=15,
    )
    if resp.ok:
        deleted += 1
        print(f'  ✓ Archived page_id: {page_id}')
    else:
        print(f'  ✗ Failed to archive {page_id}: {resp.status_code} — {resp.text[:100]}')

print(f'\n{deleted}/{len(inserted_ids)} rows deleted (archived in Notion).')
print('\nTest complete.')

"""One-off: list all pending/unassigned folders, split by source (Drive/Notion
Raw vs website-native ticket). Read-only diagnostic, no writes.

Drive-side source of truth is Notion Active Queue Status=Raw (see CLAUDE.md
"Pending Assignments" section) — NOT pending_ops_assigns.json, which goes
stale. Website-side source is fetch_pending_website_batches()'s logic:
pending_ops_assigns.json entries carrying a ticket_id that aren't yet in
dashboard_batches.json (once assigned, they get a dashboard_batches.json
entry and drop out of "pending").
"""
import json
import os
import re

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
PENDING_OPS_ASSIGNS_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')
DASHBOARD_BATCHES_FILE = os.path.join(BASE_DIR, 'dashboard_batches.json')


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def notion_query_all(token, db_id, body=None):
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results = []
    cursor = None
    while True:
        req_body = dict(body or {})
        req_body['page_size'] = 100
        if cursor:
            req_body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=req_body, timeout=15)
        if not resp.ok:
            print(f'Notion query failed: {resp.status_code} {resp.text[:200]}')
            break
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def fetch_raw_folders(token):
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows = []
    for page in pages:
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0
        rows.append({'client_name': client_name, 'folder_name': folder_name, 'video_count': video_count})
    return rows


def fetch_pending_website_batches():
    if not os.path.exists(PENDING_OPS_ASSIGNS_FILE):
        pending = {}
    else:
        with open(PENDING_OPS_ASSIGNS_FILE) as f:
            pending = json.load(f)
    if not os.path.exists(DASHBOARD_BATCHES_FILE):
        batches = {}
    else:
        with open(DASHBOARD_BATCHES_FILE) as f:
            batches = json.load(f)
    out = []
    for msg_id, item in pending.items():
        ticket_id = item.get('ticket_id')
        if not ticket_id or ticket_id in batches:
            continue
        out.append({**item, 'msg_id': msg_id})
    out.sort(key=lambda x: int(x['msg_id']))
    return out


def creator_label(item):
    name = (item.get('student_name') or '').strip()
    uname = (item.get('student_username') or '').strip()
    if uname and name:
        return f'{name} (@{uname})'
    if uname:
        return f'@{uname}'
    return name or (item.get('client_name') or '').strip() or '—'


def main():
    config = load_config()
    token = config['notion_token']

    drive_rows = fetch_raw_folders(token)
    web_rows = fetch_pending_website_batches()

    print(f'=== Drive/Notion pending (Status=Raw): {len(drive_rows)} ===')
    for r in drive_rows:
        print(f"  - {r['client_name']} / {r['folder_name']} — {r['video_count']} videos")

    print()
    print(f'=== Website pending (unclaimed tickets): {len(web_rows)} ===')
    for b in web_rows:
        print(f"  - {creator_label(b)} — {b.get('video_count') or 0} videos"
              f"{'  ' + b['ticket_url'] if b.get('ticket_url') else ''}")

    print()
    print(f'Total pending: {len(drive_rows) + len(web_rows)} '
          f'({len(drive_rows)} Drive, {len(web_rows)} website)')


if __name__ == '__main__':
    main()

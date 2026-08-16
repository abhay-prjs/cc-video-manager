"""One-off: list every editor's currently active work — Drive/Notion
In Progress folders (Active Queue Status=In Progress, grouped by Editor)
plus website-native active batches (dashboard_batches.json, status=active).
Read-only diagnostic, no writes.
"""
import json
import os
import re
from collections import defaultdict

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
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


def fetch_in_progress(token):
    body = {'filter': {'property': 'Status', 'select': {'equals': 'In Progress'}}}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows = []
    for page in pages:
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
        editor_sel = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '') or '(no editor set)'
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0
        submitted = (props.get('Submitted', {}).get('date') or {}).get('start', '')
        rows.append({
            'editor_name': editor_name,
            'client_name': client_name,
            'folder_name': folder_name,
            'video_count': video_count,
            'submitted': submitted,
        })
    return rows


def fetch_active_website_batches():
    if not os.path.exists(DASHBOARD_BATCHES_FILE):
        return []
    with open(DASHBOARD_BATCHES_FILE) as f:
        data = json.load(f)
    rows = []
    for ticket_id, item in data.items():
        if item.get('status') != 'active':
            continue
        rows.append({
            'editor_name': item.get('editor_name', '') or '(no editor set)',
            'client_name': item.get('client_name', ''),
            'folder_name': item.get('folder_name', ''),
            'video_count': item.get('video_count', 0),
            'ticket_url': item.get('ticket_url', ''),
        })
    return rows


def main():
    config = load_config()
    token = config['notion_token']

    in_progress = fetch_in_progress(token)
    website = fetch_active_website_batches()

    by_editor = defaultdict(lambda: {'drive': [], 'website': []})
    for r in in_progress:
        by_editor[r['editor_name']]['drive'].append(r)
    for r in website:
        by_editor[r['editor_name']]['website'].append(r)

    for editor in sorted(by_editor.keys()):
        d = by_editor[editor]
        total_videos = sum(x['video_count'] for x in d['drive']) + sum(x['video_count'] for x in d['website'])
        print(f'\n=== {editor} — {len(d["drive"]) + len(d["website"])} active item(s), {total_videos} videos ===')
        for r in d['drive']:
            print(f'  [Drive] {r["client_name"]} / {r["folder_name"]} — {r["video_count"]} videos'
                  f'{" (submitted " + r["submitted"] + ")" if r["submitted"] else ""}')
        for r in d['website']:
            print(f'  [Website] {r["client_name"]} / {r["folder_name"]} — {r["video_count"]} videos  {r["ticket_url"]}')


if __name__ == '__main__':
    main()

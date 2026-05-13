"""
sync_project_numbers.py
One-shot script: reads all Active Queue rows from Notion that have a Project #
and a Drive Link, then builds/updates project_numbers.json.
Run once after manually assigning numbers in Notion, or any time they drift.
"""

import json
import os
import re
import requests

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE          = os.path.join(BASE_DIR, 'config.json')
PROJECT_NUMBERS_FILE = os.path.join(BASE_DIR, 'project_numbers.json')
ACTIVE_QUEUE_DB      = '44593fbf-4276-47f0-bd12-27289dcb78fd'


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def fetch_all_rows(token):
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    rows   = []
    cursor = None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
        if not resp.ok:
            print(f'Notion query failed: {resp.status_code} {resp.text}')
            break
        data = resp.json()
        rows.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return rows


def main():
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token = config['notion_token']

    rows = fetch_all_rows(token)
    print(f'Fetched {len(rows)} rows from Active Queue')

    mapping = {}
    next_num = 1

    for page in rows:
        props = page['properties']

        drive_link = props.get('Drive Link', {}).get('url') or ''
        m = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        if not m:
            continue
        folder_id = m.group(1)

        pnum = props.get('Project #', {}).get('number')
        if pnum is None:
            continue

        mapping[folder_id] = int(pnum)
        if int(pnum) >= next_num:
            next_num = int(pnum) + 1

    mapping['_next'] = next_num

    with open(PROJECT_NUMBERS_FILE, 'w') as f:
        json.dump(mapping, f, indent=2)

    print(f'Saved {len(mapping) - 1} entries to project_numbers.json (next = #{next_num})')


if __name__ == '__main__':
    main()

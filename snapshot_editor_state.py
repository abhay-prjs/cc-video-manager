"""
snapshot_editor_state.py
Appends a timestamped snapshot of every editor's In Progress / Review
Active Queue folders to editor_state_history.jsonl. Intended to run
hourly via cron.

Purpose: delivery data (Delivery History, delivery_meta.json) only
records state at completion time — there was no way to answer "what
was in an editor's queue during their lowest-delivery week" after the
fact. This gives future reports that history to look back on.
"""

import json
import logging
import os
import re
import time
import requests

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE       = os.path.join(BASE_DIR, 'config.json')
HISTORY_FILE      = os.path.join(BASE_DIR, 'editor_state_history.jsonl')

ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
TRACKED_STATUSES = ('In Progress', 'Review', 'Revision')

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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
    url     = f'https://api.notion.com/v1/databases/{db_id}/query'
    results = []
    cursor  = None
    while True:
        req_body = dict(body or {})
        req_body['page_size'] = 100
        if cursor:
            req_body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=req_body, timeout=15)
        if not resp.ok:
            logger.error(f'notion_query_all failed for {db_id}: {resp.status_code} {resp.text[:200]}')
            break
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def fetch_active_rows(token):
    """Returns one dict per Active Queue row currently in a tracked (non-terminal) status."""
    body = {'filter': {'or': [{'property': 'Status', 'select': {'equals': s}} for s in TRACKED_STATUSES]}}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)

    rows = []
    for page in pages:
        props = page['properties']

        title_rt    = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''

        creator_rt  = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''

        editor_sel  = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '')
        if not editor_name:
            continue

        status_sel  = props.get('Status', {}).get('select') or {}
        status      = status_sel.get('name', '')

        notes_rt    = props.get('Notes', {}).get('rich_text', [])
        notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m           = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else None

        proj_num    = props.get('Project #', {}).get('number')
        project_number = f'#{int(proj_num)}' if proj_num else ''

        rows.append({
            'notion_page_id': page['id'],
            'folder_name':    folder_name,
            'client_name':    client_name,
            'editor_name':    editor_name,
            'status':         status,
            'video_count':    video_count,
            'project_number': project_number,
        })
    return rows


def main():
    config = load_config()
    token  = config['notion_token']

    rows = fetch_active_rows(token)
    snapshot = {
        'ts': time.time(),
        'folders': rows,
    }

    with open(HISTORY_FILE, 'a') as f:
        f.write(json.dumps(snapshot) + '\n')

    by_editor = {}
    for r in rows:
        by_editor.setdefault(r['editor_name'], 0)
        by_editor[r['editor_name']] += 1
    logger.info(f'Snapshot written: {len(rows)} folder(s) across {len(by_editor)} editor(s) — {by_editor}')


if __name__ == '__main__':
    main()

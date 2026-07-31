"""One-off backfill: re-send post_dashboard_assignment for every folder the
bot already tracks in Active Queue (Status in Raw / In Progress / Revision),
this time carrying the folder's real Drive createdTime.

Why this exists: folder_created_at was added to the assign/detection push
after tickets already existed on the dashboard from before that field shipped
(see discord_bot.py's post_dashboard_assignment). Those tickets are stuck
showing mirror-time as their age. This re-announces each one with
editor_name/editor_discord_id omitted — the dashboard treats an editor-less
push as a re-announcement, not an unassign, so it only corrects the age.

Rate limited to ~2/sec. Logs every non-200 response and prints a final count.

Usage:
    python3 test/backfill_dashboard_folder_created_at.py          # dry run
    python3 test/backfill_dashboard_folder_created_at.py --live   # actually POST
"""
import argparse
import json
import os
import re
import time

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'

POSTS_PER_SECOND = 2
POST_DELAY_SECONDS = 1.0 / POSTS_PER_SECOND


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
        resp = requests.post(url, headers=notion_headers(token), json=req_body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def extract_row(page):
    props = page['properties']
    title_rt = props.get('Video', {}).get('title', [])
    folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
    creator_rt = props.get('Creator', {}).get('rich_text', [])
    client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
    drive_link = props.get('Drive Link', {}).get('url') or ''
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
    folder_id = m.group(1) if m else ''
    notes_rt = props.get('Notes', {}).get('rich_text', [])
    notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
    vm = re.search(r'Videos:\s*(\d+)', notes)
    video_count = int(vm.group(1)) if vm else 0
    project_number = props.get('Project #', {}).get('number')
    return {
        'notion_page_id': page['id'],
        'folder_id': folder_id,
        'folder_name': folder_name,
        'client_name': client_name,
        'video_count': video_count,
        'project_number': str(project_number) if project_number is not None else '',
    }


def fetch_tracked_rows(token):
    """Active Queue rows the bot currently tracks — anything not yet delivered."""
    body = {
        'filter': {
            'or': [
                {'property': 'Status', 'select': {'equals': 'Raw'}},
                {'property': 'Status', 'select': {'equals': 'In Progress'}},
                {'property': 'Status', 'select': {'equals': 'Revision'}},
            ]
        }
    }
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    return [extract_row(p) for p in pages]


def get_drive_service():
    creds = Credentials.from_authorized_user_file(
        TOKEN_FILE, ['https://www.googleapis.com/auth/drive']
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)


def get_folder_created_at(service, folder_id):
    """Real Drive createdTime (ISO 8601 UTC), or None if unavailable — never
    fall back to now()."""
    if not folder_id:
        return None
    try:
        meta = service.files().get(
            fileId=folder_id, fields='createdTime', supportsAllDrives=True
        ).execute()
        return meta.get('createdTime')
    except Exception as e:
        print(f'  WARN: createdTime lookup failed for {folder_id}: {e}')
        return None


def post_assignment(url, secret, row, folder_created_at):
    payload = {
        'folder_id': row['folder_id'],
        'creator_name': row['client_name'],
        'folder_name': row['folder_name'],
        'video_count': row['video_count'],
        'raw_footage_link': f"https://drive.google.com/drive/folders/{row['folder_id']}",
        'project_number': row['project_number'],
    }
    if folder_created_at:
        payload['folder_created_at'] = folder_created_at
    resp = requests.post(
        url,
        headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=10,
    )
    return resp, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='actually POST (default is dry run)')
    args = ap.parse_args()
    dry_run = not args.live

    config = load_config()
    token = config.get('notion_token')
    url = config.get('dashboard_url')
    secret = config.get('dashboard_secret')

    if not url:
        print("ABORT: config.json has no 'dashboard_url' key — nothing to post to.")
        return
    if not secret:
        print("ABORT: config.json has no 'dashboard_secret' key.")
        return

    rows = fetch_tracked_rows(token)
    skip_no_fid = [r for r in rows if not r['folder_id']]
    rows = [r for r in rows if r['folder_id']]

    print(f'Active Queue tracked (Raw/In Progress/Revision): {len(rows)} folder(s) with a '
          f'resolvable folder_id, {len(skip_no_fid)} skipped for no folder_id.')
    print(f'Mode: {"LIVE POST" if not dry_run else "DRY RUN (no requests sent)"}\n')

    drive_service = get_drive_service()

    posted = 0
    no_created_at = 0
    failures = 0

    for r in rows:
        label = f"{r['client_name']} / {r['folder_name']} (folder_id={r['folder_id']})"
        created_at = get_folder_created_at(drive_service, r['folder_id'])
        if not created_at:
            no_created_at += 1

        if dry_run:
            print(f"  DRY RUN would POST: {label}  folder_created_at={created_at!r}")
            continue

        resp, payload = post_assignment(url, secret, r, created_at)
        if resp.status_code == 200:
            print(f"  OK: {label}  folder_created_at={created_at!r}")
            posted += 1
        else:
            print(f'  {resp.status_code}: {label} :: {resp.text[:200]}')
            failures += 1

        time.sleep(POST_DELAY_SECONDS)

    print('\n=== Summary ===')
    if dry_run:
        print(f'{len(rows)} would be posted ({no_created_at} with no createdTime available). '
              f'Re-run with --live to actually send.')
        return
    print(f'posted OK: {posted}')
    print(f'no createdTime available (key omitted): {no_created_at}')
    print(f'failures (non-200): {failures}')


if __name__ == '__main__':
    main()

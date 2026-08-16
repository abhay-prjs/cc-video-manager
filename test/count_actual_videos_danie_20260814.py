"""One-off: for each of Danie's currently-assigned Drive folders, count the
actual video files in Drive (recursive, same matching rule as production:
extension in VIDEO_EXTENSIONS OR mimeType starts with 'video/' — see
CLAUDE.md 'Video Counting') and compare against:
  - what Notion's Notes 'Videos:N' says (what /stats and assignment counts
    are based on)
  - what the folder NAME itself claims, when it embeds a count like
    '(20 vids)' or '(2 vids)' — several of Henry's folders do this

Read-only. Nothing written anywhere.
"""
import json
import os
import re

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.avi'}
MAX_DEPTH = 5

NAME_COUNT_RE = re.compile(r'\((\d+)\s*vids?\)', re.IGNORECASE)


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_drive_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, ['https://www.googleapis.com/auth/drive'])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)


def find_videos_recursive(service, folder_id, depth=0):
    all_files = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id, name, mimeType)',
            pageToken=page_token, pageSize=100,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        all_files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break

    videos = [
        f['name'] for f in all_files
        if os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS
        or f.get('mimeType', '').startswith('video/')
    ]
    if depth < MAX_DEPTH:
        for f in all_files:
            if f.get('mimeType') == 'application/vnd.google-apps.folder':
                videos.extend(find_videos_recursive(service, f['id'], depth + 1))
    return videos


def fetch_editor_rows(token, editor_name):
    headers = {
        'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'and': [
        {'property': 'Editor', 'select': {'equals': editor_name}},
        {'or': [
            {'property': 'Status', 'select': {'equals': 'In Progress'}},
            {'property': 'Status', 'select': {'equals': 'Revision'}},
        ]},
    ]}}
    results, cursor = [], None
    while True:
        b = dict(body, page_size=100)
        if cursor:
            b['start_cursor'] = cursor
        resp = requests.post(url, headers=headers, json=b, timeout=15)
        d = resp.json()
        results.extend(d.get('results', []))
        if not d.get('has_more'):
            break
        cursor = d.get('next_cursor')

    rows = []
    for page in results:
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        fname = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        cname = creator_rt[0].get('plain_text', '') if creator_rt else ''
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m = re.search(r'Videos:\s*(\d+)', notes)
        vc = int(m.group(1)) if m else 0
        link = props.get('Drive Link', {}).get('url') or ''
        m2 = re.search(r'/folders/([a-zA-Z0-9_-]+)', link)
        fid = m2.group(1) if m2 else ''
        rows.append({'client_name': cname, 'folder_name': fname, 'notion_count': vc, 'folder_id': fid})
    return rows


def main():
    config = load_config()
    token = config['notion_token']
    rows = fetch_editor_rows(token, 'Danie')

    svc = get_drive_service()

    print(f"Danie — {len(rows)} folders\n")
    for r in rows:
        name_match = NAME_COUNT_RE.search(r['folder_name'])
        name_claim = int(name_match.group(1)) if name_match else None

        if not r['folder_id']:
            print(f"{r['client_name']:10} / {r['folder_name']:38} — no folder_id parsed, skipping Drive scan")
            continue

        try:
            videos = find_videos_recursive(svc, r['folder_id'])
            actual = len(videos)
        except Exception as e:
            print(f"{r['client_name']:10} / {r['folder_name']:38} — Drive scan failed: {e}")
            continue

        flags = []
        if actual != r['notion_count']:
            flags.append(f"≠ Notion({r['notion_count']})")
        if name_claim is not None and actual != name_claim:
            flags.append(f"≠ folder-name({name_claim})")
        flag_str = '  ⚠️ ' + ', '.join(flags) if flags else '  ✓'

        print(
            f"{r['client_name']:10} / {r['folder_name']:38} — "
            f"Notion:{r['notion_count']:>3}  name-claim:{str(name_claim) if name_claim is not None else '—':>3}  "
            f"actual-in-Drive:{actual:>3}{flag_str}"
        )
        if flags:
            print(f"    files: {videos}")


if __name__ == '__main__':
    main()

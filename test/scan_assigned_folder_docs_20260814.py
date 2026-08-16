"""One-off: for the 36 folders bulk-assigned on 2026-08-14
(bulk_assign_pending_20260814.py), scan each Drive folder for any Google Docs
(scripts/briefs/instructions) and check whether they mention how many videos
are actually in the batch and/or a daily video quota — Notion's 'Videos:N'
note can drift from what the client doc actually says, and daily-quota asks
live in doc text, not in any Notion field. Read-only: lists files, exports
Doc text, greps for count/quota patterns. Nothing written anywhere.
"""
import json
import os
import re

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')

# The 36 (page_id -> editor) assignments from bulk_assign_pending_20260814.py
PLAN = {
    '3bbe637c-317d-81f4-95b2-f138c4bef161': 'Jill', '3bbe637c-317d-813f-ab48-f7874f913cb5': 'Jill',
    '3bbe637c-317d-8171-b27f-cb216970ec06': 'Danie', '3bbe637c-317d-81d7-86ae-eb50b60e934c': 'Danie',
    '3bbe637c-317d-815b-bd74-cbe53aaf072e': 'Danie', '3bbe637c-317d-8188-bf6f-c21417a8007f': 'Danie',
    '3bbe637c-317d-8160-9146-cbfdc560fc69': 'Danie', '3bbe637c-317d-81b7-9470-e4242d98fa66': 'Danie',
    '3bbe637c-317d-810d-9bbd-e29663cd3503': 'Danie',
    '3bce637c-317d-8165-937d-f5956e67153e': 'Storm', '3bbe637c-317d-8125-9357-e390d70ec6aa': 'Storm',
    '3bbe637c-317d-81e9-8cd5-f978732abe63': 'Storm', '3bbe637c-317d-8161-abde-dee9505a30d8': 'Storm',
    '3bae637c-317d-81a2-bcde-e487b64f5d80': 'Naomi', '3bae637c-317d-81b2-881a-ff0e1e64cc36': 'Naomi',
    '3bae637c-317d-81f7-860b-e3b7fc18ed36': 'Naomi', '3bae637c-317d-8157-9995-e690dd025e56': 'Naomi',
    '3bae637c-317d-8160-b6a4-e378f8453c76': 'Naomi', '3bbe637c-317d-8162-a1ba-c932437716e1': 'Naomi',
    '3bbe637c-317d-819f-97fc-ce9943a103f8': 'Naomi',
    '3bbe637c-317d-8172-96e0-da213beae7f7': 'Aki', '3bbe637c-317d-8139-991e-f878f515f4cb': 'Aki',
    '3bbe637c-317d-8170-8812-dd252ff64331': 'Aki',
    '3bbe637c-317d-81cc-bd27-d0e63dcf8c92': 'Josh', '3bbe637c-317d-812b-9250-eec30324efd9': 'Josh',
    '3bbe637c-317d-814a-b5fe-cb6216b53307': 'Josh', '3bce637c-317d-814b-9b17-dca2295a6eef': 'Josh',
    '3bce637c-317d-81aa-9d42-cdb71c64eed7': 'Josh', '3bbe637c-317d-8139-ab0a-e32ca082a8a4': 'Josh',
    '3bbe637c-317d-8150-b3dd-ccc9d0f5246f': 'Josh',
    '3bbe637c-317d-8177-b19c-fc643ce0bff5': 'Jewel', '3bbe637c-317d-81d6-9dad-cb55fc534e79': 'Jewel',
    '3bbe637c-317d-8171-8585-d78a9128f9b8': 'Jewel',
    '3bbe637c-317d-811b-881f-e5361b1a811d': 'Zyon', '3bbe637c-317d-8186-a996-e5d8cfc77434': 'Zyon',
    '3bbe637c-317d-810e-b288-dc73c21acd23': 'Zyon',
}

RAW_ROWS_CACHE = '/tmp/claude-1001/-home-ubuntu-gdrive-watcher/35a92d2f-c88b-4ef9-a8fe-3ed5f146217e/scratchpad/raw_rows.json'

DOC_MIME = 'application/vnd.google-apps.document'
SHEET_MIME = 'application/vnd.google-apps.spreadsheet'
TEXT_MIMES = {'text/plain', 'application/pdf'}

COUNT_RE = re.compile(r'(\d+)\s*(?:videos?|vids?|clips?)\b', re.IGNORECASE)
DAILY_RE = re.compile(
    r'(\d+)\s*(?:videos?|vids?|clips?)\s*(?:per|/|a)\s*day|'
    r'per\s*day[:\s]*(\d+)|daily\s*(?:quota|target|goal)[:\s]*(\d+)',
    re.IGNORECASE,
)


def get_drive_service():
    creds = Credentials.from_authorized_user_file(
        TOKEN_FILE, ['https://www.googleapis.com/auth/drive']
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)


def list_children(svc, folder_id):
    files = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields='nextPageToken, files(id, name, mimeType)',
            supportsAllDrives=True, includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return files


def read_doc_text(svc, file_id, mime_type):
    try:
        if mime_type == DOC_MIME:
            data = svc.files().export(fileId=file_id, mimeType='text/plain').execute()
            return data.decode('utf-8', errors='ignore') if isinstance(data, bytes) else data
        if mime_type == 'text/plain':
            data = svc.files().get_media(fileId=file_id).execute()
            return data.decode('utf-8', errors='ignore') if isinstance(data, bytes) else data
    except Exception as e:
        return f'[read error: {e}]'
    return None


def scan_folder_for_docs(svc, folder_id, depth=0, max_depth=2):
    """Returns list of (name, mimeType, id) for Doc/text files found, searching
    up to max_depth levels deep (folders often nest a script doc one level in)."""
    found = []
    try:
        children = list_children(svc, folder_id)
    except Exception as e:
        return [('(listing failed)', str(e), '')]
    for f in children:
        mt = f.get('mimeType', '')
        if mt in (DOC_MIME, 'text/plain') or mt == 'application/pdf':
            found.append((f['name'], mt, f['id']))
        elif mt == 'application/vnd.google-apps.folder' and depth < max_depth:
            found.extend(scan_folder_for_docs(svc, f['id'], depth + 1, max_depth))
    return found


def main():
    with open(RAW_ROWS_CACHE) as f:
        raw_rows = {r['page_id']: r for r in json.load(f)}

    svc = get_drive_service()

    results = []
    for page_id, editor in PLAN.items():
        row = raw_rows.get(page_id)
        if not row:
            continue
        label = f"{row['client_name']} / {row['folder_name']}"
        print(f'--- {label} ({editor}) — Notion says {row["video_count"]} videos ---')
        docs = scan_folder_for_docs(svc, row['folder_id'])
        entry = {
            'client_name': row['client_name'], 'folder_name': row['folder_name'],
            'editor': editor, 'notion_video_count': row['video_count'],
            'docs_found': [], 'mentioned_counts': [], 'daily_mentions': [],
        }
        if not docs:
            print('  (no docs/text/pdf files found)')
        for name, mt, fid in docs:
            print(f'  doc: {name} ({mt})')
            entry['docs_found'].append(name)
            if mt in (DOC_MIME, 'text/plain'):
                text = read_doc_text(svc, fid, mt)
                if text:
                    counts = COUNT_RE.findall(text)
                    daily = DAILY_RE.findall(text)
                    if counts:
                        print(f'    video-count mentions: {counts[:10]}')
                        entry['mentioned_counts'].extend(counts)
                    if daily:
                        flat = [x for tup in daily for x in tup if x]
                        print(f'    DAILY QUOTA mention: {flat}')
                        entry['daily_mentions'].extend(flat)
        results.append(entry)
        print()

    out_path = '/tmp/claude-1001/-home-ubuntu-gdrive-watcher/35a92d2f-c88b-4ef9-a8fe-3ed5f146217e/scratchpad/folder_doc_scan.json'
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2)
    print(f'Saved full results to {out_path}')


if __name__ == '__main__':
    main()

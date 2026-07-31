"""
notion_bridge.py
Telegram bot that handles new folder assignment notifications with inline buttons.
Runs as a long-polling bot on the Oracle VM.

Install:
    pip install python-telegram-bot==20.* requests

Run:
    python3 notion_bridge.py

Keep alive:
    Add to systemd or run with: nohup python3 notion_bridge.py &
"""

import os
import re
import json
import threading
import requests
import ai_ops
from filelock import FileLock
from datetime import datetime, timedelta, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters, ContextTypes
)

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
STATE_FILE = os.path.join(BASE_DIR, 'watched_files.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_assignments.json')
PENDING_REVIEWS_FILE = os.path.join(BASE_DIR, 'pending_reviews.json')
PENDING_FOLDERS_FILE    = os.path.join(BASE_DIR, 'pending_folders.json')
DISCORD_QUEUE_FILE      = os.path.join(BASE_DIR, 'discord_queue.json')
IGNORED_FOLDERS_FILE    = os.path.join(BASE_DIR, 'ignored_folders.json')
REMOVED_FOLDERS_FILE    = os.path.join(BASE_DIR, 'removed_folders.json')
DEADLINES_FILE          = os.path.join(BASE_DIR, 'deadlines.json')
PROJECT_NUMBERS_FILE    = os.path.join(BASE_DIR, 'project_numbers.json')

DISCORD_QUEUE_LOCK      = FileLock(DISCORD_QUEUE_FILE    + '.lock')
PENDING_FOLDERS_LOCK    = FileLock(PENDING_FOLDERS_FILE  + '.lock')
IGNORED_FOLDERS_LOCK    = FileLock(IGNORED_FOLDERS_FILE  + '.lock')
REMOVED_FOLDERS_LOCK    = FileLock(REMOVED_FOLDERS_FILE  + '.lock')
PENDING_REVIEWS_LOCK    = FileLock(PENDING_REVIEWS_FILE  + '.lock')
PROJECT_NUMBERS_LOCK    = FileLock(PROJECT_NUMBERS_FILE  + '.lock')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.avi'}

from logger_setup import get_logger
logger = get_logger('notion_bridge')


# ── Project number helpers ────────────────────────────────────────────────────

def _load_project_numbers():
    if not os.path.exists(PROJECT_NUMBERS_FILE):
        return {'_next': 1}
    with open(PROJECT_NUMBERS_FILE) as f:
        return json.load(f)


def get_project_number(folder_id):
    """Returns '#N' for the given folder_id, or '' if not assigned yet."""
    if not folder_id:
        return ''
    n = _load_project_numbers().get(folder_id)
    return f'#{n}' if n else ''


def assign_project_number(folder_id):
    """Assigns the next sequential number to folder_id if not already done. Returns '#N'."""
    with PROJECT_NUMBERS_LOCK:
        data = _load_project_numbers()
        if folder_id in data:
            return f'#{data[folder_id]}'
        n = data.get('_next', 1)
        data[folder_id] = n
        data['_next']   = n + 1
        with open(PROJECT_NUMBERS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
        return f'#{n}'

EDT = timezone(timedelta(hours=-4))
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt_edt):
    """Format an EDT datetime as a human-readable IST string for Telegram display."""
    return dt_edt.astimezone(IST).strftime('%d %b %Y %I:%M %p IST')


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _send_discord_ops_channel(config, message=None, embed=None):
    channel_id = config.get('ops_channel_id')
    token = config.get('discord_bot_token')
    if not channel_id or not token:
        logger.error('ops_channel_id or discord_bot_token missing in config')
        return
    payload = {}
    if message:
        payload['content'] = message
    if embed:
        payload['embeds'] = [embed]
    try:
        requests.post(
            f'https://discord.com/api/v10/channels/{channel_id}/messages',
            headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=10,
        )
    except Exception as e:
        logger.error(f'Discord ops channel error: {e}')


def _send_telegram(url, payload, **kwargs):
    """POSTs to Telegram, swallowing connection errors.
    Telegram has been unreachable from this box since 2026-06-18 (network-level
    outage, not a code bug) — callers must not let this raise, since the Discord
    queue (enqueue_*) is the real notification path now."""
    try:
        return requests.post(url, json=payload, **kwargs)
    except requests.exceptions.RequestException as e:
        logger.error(f'Telegram send failed (expected — Telegram unreachable): {e}')
        return None


# ── Drive API helpers ─────────────────────────────────────────────────────────

def get_drive_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, DRIVE_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)


def _list_folder(service, folder_id):
    """List all items directly inside folder_id."""
    items, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return items


def _is_video_file(f):
    # Extension OR mimeType — some clients upload videos with extension-less names.
    return (f['mimeType'] != 'application/vnd.google-apps.folder'
            and (os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS
                 or f['mimeType'].startswith('video/')))


def fetch_folder_video_tree(folder_id, folder_name=None):
    """
    Returns (total_count, video_tree, flat_names) from Drive, or (None, None, None) on error.
    video_tree maps section label → [filenames]. Recurses to arbitrary depth — a fixed
    depth limit silently misses videos some clients nest 3+ levels deep.
    """
    try:
        service = get_drive_service()
        if not folder_name:
            meta = service.files().get(fileId=folder_id, fields='name',
                                       supportsAllDrives=True).execute()
            folder_name = meta.get('name', 'Folder')

        video_tree, flat_names = {}, []

        def walk(fid, label, is_root):
            items = _list_folder(service, fid)
            videos = [f['name'] for f in items if _is_video_file(f)]
            if videos:
                key = f'{label} (root)' if is_root else label
                video_tree[key] = videos
                flat_names.extend(videos)
            for item in items:
                if item['mimeType'] != 'application/vnd.google-apps.folder':
                    continue
                child_label = item['name'] if is_root else f"{label} / {item['name']}"
                walk(item['id'], child_label, False)

        walk(folder_id, folder_name, True)
        return len(flat_names), video_tree, flat_names
    except Exception as e:
        logger.error(f'Drive API error fetching tree for folder {folder_id}: {e}')
        return None, None, None


def fetch_folder_video_info(folder_id):
    """Returns (total_count, flat_names) across root + sub-subfolders. (None, None) on error."""
    count, _, flat = fetch_folder_video_tree(folder_id)
    if count is None:
        return None, None
    return count, flat


def find_edited_folder_id_from_raw(raw_folder_id):
    """Walk up from raw_folder_id to find the Edited/ sibling folder; return its Drive ID or ''."""
    try:
        service = get_drive_service()
        current_id = raw_folder_id
        for _ in range(4):
            meta = service.files().get(fileId=current_id, fields='parents', supportsAllDrives=True).execute()
            parents = meta.get('parents', [])
            if not parents:
                break
            parent_id = parents[0]
            resp = service.files().list(
                q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id,name)',
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            # Matched by stripped name, not an exact Drive-side filter — a stray
            # leading/trailing space on the folder name (seen for real on client
            # folders) makes an exact `name='Edited'` match silently miss it.
            for f in resp.get('files', []):
                if f['name'].strip() == 'Edited':
                    return f['id']
            current_id = parent_id
        return ''
    except Exception as e:
        logger.error(f'Drive error finding Edited folder from raw {raw_folder_id}: {e}')
        return ''


# ── Notion API ────────────────────────────────────────────────────────────────

ACTIVE_QUEUE_DB      = '44593fbf-4276-47f0-bd12-27289dcb78fd'
ASSIGNMENTS_DB       = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'
EDITOR_PROFILES_DB   = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB  = '733883073ccf48f2a83953ba2d5ad36d'
EDITOR_SCHEDULES_DB  = 'a02419d207604357a27698d559160436'
DELIVERY_DATE_PROP   = 'date:Delivered Date:start'  # actual Notion property name in Delivery History DB

DAYS_OF_WEEK = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28'
    }


def notion_query_all(token, db_id, body=None):
    """Queries a Notion database, following has_more/next_cursor until all rows are fetched.
    `body` may include 'filter'/'sorts' but should not set 'page_size'/'start_cursor'."""
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


def get_editor_loads(token):
    """Returns {editor_name: {active, capacity, page_id}} from Editor Profiles DB.
    Excludes editors where Capacity is None or 0 (treated as inactive)."""
    url = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={})
    loads = {}
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            name = props.get('Editor', {}).get('title', [{}])
            name = name[0].get('plain_text', '') if name else ''
            active = props.get('Active Videos', {}).get('number') or 0
            capacity = props.get('Capacity', {}).get('number')
            is_active = props.get('Active', {}).get('checkbox', True)
            page_id = page['id']
            if name and capacity and is_active:
                loads[name] = {'active': active, 'capacity': capacity, 'page_id': page_id}
    return loads


# ── Editor Schedules (Notion DB) ──────────────────────────────────────────────

def _time_to_minutes(time_str):
    """Parse 'HH:MM' where HH can be ≥24 (e.g. '26:00' = next-day 02:00). Returns total minutes from midnight."""
    try:
        h, m = map(int, time_str.strip().split(':'))
        return h * 60 + m
    except Exception:
        return 0


def get_editor_schedules(token):
    """Query Editor Schedules DB. Returns {editor_name: [{'page_id','day','start','end','available'}]}."""
    url = f'https://api.notion.com/v1/databases/{EDITOR_SCHEDULES_DB}/query'
    schedules = {}
    cursor = None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
        if not resp.ok:
            logger.error(f'get_editor_schedules failed: {resp.status_code} {resp.text}')
            break
        data = resp.json()
        for page in data.get('results', []):
            props = page['properties']
            editor = (props.get('Editor', {}).get('select') or {}).get('name', '')
            day    = (props.get('Day',    {}).get('select') or {}).get('name', '')
            start_rt = props.get('Start EDT', {}).get('rich_text', [])
            start    = start_rt[0].get('plain_text', '') if start_rt else ''
            end_rt   = props.get('End EDT', {}).get('rich_text', [])
            end      = end_rt[0].get('plain_text', '') if end_rt else ''
            available = props.get('Available', {}).get('checkbox', False)
            if editor and day:
                schedules.setdefault(editor, []).append({
                    'page_id':   page['id'],
                    'day':       day,
                    'start':     start,
                    'end':       end,
                    'available': available,
                })
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return schedules


def is_editor_available(editor_name, schedules, now_edt):
    """True if editor has an Available=True shift covering now_edt. No schedule = always available."""
    rows = schedules.get(editor_name, [])
    if not rows:
        return True

    today     = now_edt.strftime('%A')
    yesterday = (now_edt - timedelta(days=1)).strftime('%A')
    now_mins  = now_edt.hour * 60 + now_edt.minute

    for row in rows:
        if not row['available'] or not row['start'] or not row['end']:
            continue
        start_m = _time_to_minutes(row['start'])
        end_m   = _time_to_minutes(row['end'])

        if row['day'] == today:
            if end_m > 1440:
                # e.g. 22:00-26:00 → covers now ≥ 22:00 OR now < 02:00
                if now_mins >= start_m or now_mins < (end_m - 1440):
                    return True
            elif end_m <= start_m:
                # overnight without >24 notation (e.g. 22:00-02:00)
                if now_mins >= start_m or now_mins < end_m:
                    return True
            else:
                if start_m <= now_mins < end_m:
                    return True

        elif row['day'] == yesterday:
            # Check if yesterday's shift overflows into today
            if end_m > 1440 and now_mins < (end_m - 1440):
                return True
            elif end_m < start_m and now_mins < end_m:
                return True

    return False


def get_next_available_minutes(editor_name, schedules, now_edt):
    """Minutes until editor's next Available shift. 0 = available now. None = no schedule."""
    rows = schedules.get(editor_name, [])
    if not rows:
        return None  # no schedule configured

    if is_editor_available(editor_name, schedules, now_edt):
        return 0

    today_idx = now_edt.weekday()
    now_mins  = now_edt.hour * 60 + now_edt.minute
    min_wait  = None

    for day_offset in range(1, 8):
        check_day = DAYS_OF_WEEK[(today_idx + day_offset) % 7]
        for row in rows:
            if not row['available'] or row['day'] != check_day or not row['start']:
                continue
            start_m = _time_to_minutes(row['start'])
            wait = day_offset * 1440 - now_mins + start_m
            if min_wait is None or wait < min_wait:
                min_wait = wait

    return min_wait


def _rank_editors(loads, schedules, now_edt):
    """Rank editors by tier: (0) in scheduled shift → (1) no schedule set → (2) out of shift."""
    ranked = []
    for editor, info in loads.items():
        ratio        = info['active'] / info['capacity'] if info['capacity'] else 0
        has_schedule = bool(schedules.get(editor))
        avail_now    = is_editor_available(editor, schedules, now_edt)
        mins_until   = 0 if avail_now else get_next_available_minutes(editor, schedules, now_edt)

        # tier 0 = in shift right now, tier 1 = no schedule (unknown), tier 2 = out of shift
        if avail_now and has_schedule:
            tier = 0
        elif not has_schedule:
            tier = 1
        else:
            tier = 2

        ranked.append({
            'editor':        editor,
            'ratio':         ratio,
            'available_now': avail_now,
            'has_schedule':  has_schedule,
            'mins_until':    mins_until,
            'tier':          tier,
            'info':          info,
        })
    ranked.sort(key=lambda x: (x['tier'], x['ratio'], x['mins_until'] if x['mins_until'] is not None else 9999))
    return ranked


def get_recommendation(token):
    """Returns ranked editor list (same format as _rank_editors). Fetches loads + schedules fresh."""
    loads     = get_editor_loads(token)
    schedules = get_editor_schedules(token)
    return _rank_editors(loads, schedules, datetime.now(EDT))


def _upsert_schedule_row(token, editor_name, day, start, end, available=True):
    """Create or update the Editor Schedules row for editor+day. Returns page_id or None."""
    url  = f'https://api.notion.com/v1/databases/{EDITOR_SCHEDULES_DB}/query'
    body = {'filter': {'and': [
        {'property': 'Editor', 'select': {'equals': editor_name}},
        {'property': 'Day',    'select': {'equals': day}},
    ]}}
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    existing_id = None
    if resp.ok:
        results = resp.json().get('results', [])
        if results:
            existing_id = results[0]['id']

    props = {
        'Editor':    {'select':    {'name': editor_name}},
        'Day':       {'select':    {'name': day}},
        'Start EDT': {'rich_text': [{'text': {'content': start}}]},
        'End EDT':   {'rich_text': [{'text': {'content': end}}]},
        'Available': {'checkbox':  available},
    }

    if existing_id:
        r = requests.patch(
            f'https://api.notion.com/v1/pages/{existing_id}',
            headers=notion_headers(token), json={'properties': props}, timeout=15,
        )
        return existing_id if r.ok else None
    else:
        r = requests.post(
            'https://api.notion.com/v1/pages',
            headers=notion_headers(token),
            json={'parent': {'database_id': EDITOR_SCHEDULES_DB}, 'properties': props},
            timeout=15,
        )
        return r.json().get('id') if r.ok else None


def get_folder_assignment(token, client, folder):
    """
    Looks up Creator Assignments for client+folder combo.
    Returns editor name or None.
    Priority: exact folder match > client-only match (blank folder)
    """
    url = f'https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={})
    if not resp.ok:
        return None

    exact_match = None
    client_match = None

    for page in resp.json().get('results', []):
        props = page['properties']
        row_client = props.get('Creator/Folder', {}).get('title', [{}])
        row_client = row_client[0].get('plain_text', '') if row_client else ''
        row_folder = props.get('Folder', {}).get('rich_text', [{}])
        row_folder = row_folder[0].get('plain_text', '') if row_folder else ''
        editor = props.get('Primary Editor', {}).get('select', {})
        editor = editor.get('name', '') if editor else ''

        if row_client.lower() == client.lower():
            if row_folder and row_folder.lower() == folder.lower():
                exact_match = editor
            elif not row_folder:
                client_match = editor

    return exact_match or client_match


def get_active_queue_page_id_by_folder_id(token, folder_id):
    """Returns the Notion page_id of any existing Active Queue row for this folder_id."""
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Drive Link', 'url': {'contains': folder_id}}}
    resp = requests.post(url, headers=notion_headers(token), json=body)
    if resp.ok:
        results = resp.json().get('results', [])
        if results:
            return results[0]['id']
    return None


def assign_all_active_queue_rows(token, folder_id, editor, status='In Progress'):
    """Updates ALL Active Queue rows for this folder_id to the given editor + status.
    Returns the first page_id found, or None."""
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Drive Link', 'url': {'contains': folder_id}}}
    resp = requests.post(url, headers=notion_headers(token), json=body)
    first_id = None
    if resp.ok:
        for page in resp.json().get('results', []):
            pid = page['id']
            if first_id is None:
                first_id = pid
            requests.patch(
                f'https://api.notion.com/v1/pages/{pid}',
                headers=notion_headers(token),
                json={'properties': {
                    'Editor': {'select': {'name': editor}},
                    'Status': {'select': {'name': status}},
                }},
                timeout=15,
            )
    return first_id


def get_active_queue_row_by_folder_id(token, folder_id):
    """Queries Active Queue by Drive Link URL containing folder_id; falls back to Notes scan."""
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Drive Link', 'url': {'contains': folder_id}}}
    resp = requests.post(url, headers=notion_headers(token), json=body)
    if resp.ok:
        for page in resp.json().get('results', []):
            editor_sel = page['properties'].get('Editor', {}).get('select') or {}
            name = editor_sel.get('name', '')
            if name:
                return name
    # Fallback: full scan checking Notes field (covers rows created before Drive Link was added)
    for page in notion_query_all(token, ACTIVE_QUEUE_DB):
        props = page['properties']
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        if folder_id in notes:
            editor_sel = props.get('Editor', {}).get('select') or {}
            name = editor_sel.get('name', '')
            if name:
                return name
    return None


def get_active_queue_editor(token, folder_id, client, folder_name):
    """Queries Active Queue DB to find which editor is assigned to the given folder."""
    # Primary: search by Drive Link containing folder_id
    if folder_id:
        editor = get_active_queue_row_by_folder_id(token, folder_id)
        if editor:
            return editor
    # Fallback: match Video title == folder_name AND Creator == client
    for page in notion_query_all(token, ACTIVE_QUEUE_DB):
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        title = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        creator = creator_rt[0].get('plain_text', '') if creator_rt else ''
        editor_sel = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '')
        if title.lower() == folder_name.lower() and creator.lower() == client.lower() and editor_name:
            return editor_name
    return None


def create_active_queue_folder_row(token, folder_name, client, folder_id, video_count, editor=None, status='Raw', project_number=None):
    """Creates a new folder-based row in the Active Queue Notion database."""
    url = 'https://api.notion.com/v1/pages'
    # Store the full timestamp (not just a date) — a date-only value gets parsed
    # elsewhere as UTC midnight, which drifts by hours against this EDT-labeled
    # date and made "just submitted" folders show up as ~1 day old (fmt_age in
    # dashboard.py, unassigned_reminder.py's hours_ago check).
    submitted_ts = datetime.now(EDT).isoformat()

    properties = {
        'Video': {'title': [{'text': {'content': folder_name}}]},
        'Creator': {'rich_text': [{'text': {'content': client}}]},
        'Status': {'select': {'name': status}},
        'Submitted': {'date': {'start': submitted_ts}},
        'Notes': {'rich_text': [{'text': {'content': f'Videos: {video_count} | Folder ID: {folder_id}'}}]},
        'Drive Link': {'url': f'https://drive.google.com/drive/folders/{folder_id}'},
    }

    if editor:
        properties['Editor'] = {'select': {'name': editor}}
    if project_number:
        try:
            properties['Project #'] = {'number': int(str(project_number).lstrip('#'))}
        except (ValueError, TypeError):
            pass

    body = {
        'parent': {'database_id': ACTIVE_QUEUE_DB},
        'properties': properties,
    }

    resp = requests.post(url, headers=notion_headers(token), json=body)
    if resp.ok:
        return resp.json().get('id')
    else:
        logger.error(f'Failed to create Notion folder row: {resp.text}')
        return None


def update_notion_editor(token, page_id, editor):
    """Updates the Editor field on an Active Queue row."""
    url = f'https://api.notion.com/v1/pages/{page_id}'
    body = {'properties': {'Editor': {'select': {'name': editor}}, 'Status': {'select': {'name': 'Raw'}}}}
    resp = requests.patch(url, headers=notion_headers(token), json=body)
    return resp.ok


def update_editor_load(token, editor_name, delta):
    """Increments or decrements Active Videos count for an editor."""
    loads = get_editor_loads(token)
    if editor_name not in loads:
        return
    page_id = loads[editor_name]['page_id']
    new_count = max(0, loads[editor_name]['active'] + delta)

    capacity = loads[editor_name]['capacity']
    ratio = new_count / capacity if capacity else 0
    if ratio >= 0.85:
        status = 'Overloaded'
    elif ratio >= 0.6:
        status = 'Busy'
    else:
        status = 'Available'

    url = f'https://api.notion.com/v1/pages/{page_id}'
    body = {'properties': {
        'Active Videos': {'number': new_count},
        'Status': {'select': {'name': status}}
    }}
    requests.patch(url, headers=notion_headers(token), json=body)


def recalculate_active_videos(token, editor_name):
    """Recompute Active Videos from Active Queue (In Progress + Raw) and sync Editor Profiles."""
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': editor_name}},
                {'or': [
                    {'property': 'Status', 'select': {'equals': 'In Progress'}},
                    {'property': 'Status', 'select': {'equals': 'Raw'}},
                ]},
            ]
        }
    }
    resp = requests.post(url, headers=notion_headers(token), json=body)
    total = 0
    if resp.ok:
        for page in resp.json().get('results', []):
            notes_rt = page['properties'].get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            total += int(m.group(1)) if m else 0

    loads = get_editor_loads(token)
    if editor_name not in loads:
        logger.warning(f'recalculate_active_videos: editor {editor_name} not found in profiles')
        return total
    page_id = loads[editor_name]['page_id']
    capacity = loads[editor_name]['capacity']
    ratio = total / capacity if capacity else 0
    status = 'Overloaded' if ratio >= 0.85 else 'Busy' if ratio >= 0.6 else 'Available'

    requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(token),
        json={'properties': {
            'Active Videos': {'number': total},
            'Status': {'select': {'name': status}},
        }},
    )
    logger.info(f'recalculate_active_videos: {editor_name} -> {total} (status: {status})')
    return total


def create_delivery_history_row(token, folder_name, client_name, editor_name,
                                 confirmed_count, today_str, edited_folder, drive_link):
    count = int(confirmed_count) if confirmed_count is not None else 0
    logger.info(f"create_delivery_history_row: folder={folder_name}, editor={editor_name}, count={count}")
    props = {
        'Folder':             {'title':     [{'text': {'content': folder_name}}]},
        'Client':             {'rich_text': [{'text': {'content': client_name}}]},
        'Editor':             {'select':    {'name': editor_name}},
        'Videos Completed':   {'number':    count},
        DELIVERY_DATE_PROP:   {'date':      {'start': today_str}},
        'Edited Folder Name': {'rich_text': [{'text': {'content': edited_folder}}]},
    }
    if drive_link:
        props['Drive Link'] = {'url': drive_link}
    resp = requests.post(
        'https://api.notion.com/v1/pages',
        headers=notion_headers(token),
        json={'parent': {'database_id': DELIVERY_HISTORY_DB}, 'properties': props},
        timeout=15,
    )
    if resp.ok:
        logger.info(f"Delivery History row created: {folder_name} — {count} videos by {editor_name}")
    else:
        logger.error(f'Failed to create Delivery History row: {resp.status_code} {resp.text}')


# ── Discord Queue IPC ──────────────────────────────────────────────────────────

def _append_to_discord_queue(item):
    """Append one item to discord_queue.json under a file lock."""
    with DISCORD_QUEUE_LOCK:
        queue = []
        if os.path.exists(DISCORD_QUEUE_FILE):
            with open(DISCORD_QUEUE_FILE) as f:
                queue = json.load(f)
        queue.append(item)
        with open(DISCORD_QUEUE_FILE, 'w') as f:
            json.dump(queue, f, indent=2)


def enqueue_discord_assignment(client_name, folder_name, video_count, folder_id, editor_name, notion_page_id=None, project_number=''):
    """Writes an assignment to discord_queue.json for discord_bot.py to pick up."""
    try:
        _append_to_discord_queue({
            'client_name':          client_name,
            'folder_name':          folder_name,
            'video_count':          video_count,
            'folder_id':            folder_id,
            'editor_name':          editor_name,
            'notion_queue_page_id': notion_page_id,
            'project_number':       project_number or get_project_number(folder_id),
        })
    except Exception as e:
        logger.error(f'Failed to enqueue Discord assignment: {e}')


def enqueue_creator_notify(client_name, folder_name, editor_name, video_count, folder_id=''):
    """Writes a creator_notify item to discord_queue.json for the creator's channel."""
    try:
        _append_to_discord_queue({
            'type':           'creator_notify',
            'client_name':    client_name,
            'folder_name':    folder_name,
            'editor_name':    editor_name,
            'video_count':    video_count,
            'folder_id':      folder_id,
            'project_number': get_project_number(folder_id) if folder_id else '',
            'timestamp':      datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f'Failed to enqueue creator notify: {e}')


def enqueue_creator_detected(client_name, folder_name, video_count, folder_id=''):
    """Pings the creator's Discord channel the moment a folder is detected (before assignment)."""
    try:
        _append_to_discord_queue({
            'type':        'creator_detected',
            'client_name': client_name,
            'folder_name': folder_name,
            'video_count': video_count,
            'folder_id':   folder_id,
            'timestamp':   datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f'Failed to enqueue creator_detected: {e}')


def enqueue_ops_assign_request(client_name, folder_name, video_count, folder_id, project_number='', notion_page_id=''):
    """Posts a new-folder assignment request to the Discord assignments channel."""
    try:
        _append_to_discord_queue({
            'type':           'ops_assign_request',
            'client_name':    client_name,
            'folder_name':    folder_name,
            'video_count':    video_count,
            'folder_id':      folder_id,
            'notion_page_id': notion_page_id,
            'project_number': project_number,
            'timestamp':      datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f'Failed to enqueue ops_assign_request: {e}')


# ── Pending Assignments State ─────────────────────────────────────────────────

def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f)
    return {}


def save_pending(data):
    with open(PENDING_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def add_pending(callback_key, data):
    pending = load_pending()
    pending[callback_key] = data
    save_pending(pending)


def get_pending_item(callback_key):
    return load_pending().get(callback_key)


def remove_pending(callback_key):
    pending = load_pending()
    pending.pop(callback_key, None)
    save_pending(pending)


def load_pending_folders():
    if os.path.exists(PENDING_FOLDERS_FILE):
        with open(PENDING_FOLDERS_FILE) as f:
            return json.load(f)
    return {}


def save_pending_folders(data):
    with open(PENDING_FOLDERS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# ── Ignored Folders ───────────────────────────────────────────────────────────

def load_ignored_folders():
    if os.path.exists(IGNORED_FOLDERS_FILE):
        with IGNORED_FOLDERS_LOCK:
            with open(IGNORED_FOLDERS_FILE) as f:
                return json.load(f)
    return []


def save_ignored_folders(folder_ids):
    with IGNORED_FOLDERS_LOCK:
        with open(IGNORED_FOLDERS_FILE, 'w') as f:
            json.dump(folder_ids, f, indent=2)


def add_ignored_folder(folder_id):
    ids = load_ignored_folders()
    if folder_id not in ids:
        ids.append(folder_id)
        save_ignored_folders(ids)


def remove_ignored_folder(folder_id):
    ids = load_ignored_folders()
    if folder_id in ids:
        ids.remove(folder_id)
        save_ignored_folders(ids)


def is_folder_ignored(folder_id):
    return folder_id in load_ignored_folders()


# ── Removed Folders Cache ─────────────────────────────────────────────────────

def load_removed_folders():
    if os.path.exists(REMOVED_FOLDERS_FILE):
        with REMOVED_FOLDERS_LOCK:
            with open(REMOVED_FOLDERS_FILE) as f:
                return json.load(f)
    return {}


def save_removed_folders(data):
    with REMOVED_FOLDERS_LOCK:
        with open(REMOVED_FOLDERS_FILE, 'w') as f:
            json.dump(data, f, indent=2)


def cache_removed_folder(page_id, row, status):
    data = load_removed_folders()
    data[page_id] = {**row, 'status': status, 'removed_at': datetime.now(timezone.utc).isoformat()}
    save_removed_folders(data)


def pop_removed_folder(page_id):
    data = load_removed_folders()
    row  = data.pop(page_id, None)
    save_removed_folders(data)
    return row


def pop_deadline_entry(folder_id, notion_page_id):
    """Removes a deadlines.json entry on folder removal/archival, so archived folders
    stop showing up as perpetually-overdue. Mirrors discord_bot.py's pop_deadline_entry()."""
    if not os.path.exists(DEADLINES_FILE):
        return
    with open(DEADLINES_FILE) as f:
        deadlines = json.load(f)
    key = folder_id if (folder_id and folder_id in deadlines) else None
    if key is None and notion_page_id:
        for fid, d in deadlines.items():
            if d.get('notion_page_id') == notion_page_id:
                key = fid
                break
    if key is not None and key in deadlines:
        del deadlines[key]
        with open(DEADLINES_FILE, 'w') as f:
            json.dump(deadlines, f, indent=2)


# ── Telegram Helpers ──────────────────────────────────────────────────────────

def build_folder_notification_message(client, folder_name, video_count, suggested_editor, loads, project_num=''):
    load_hints  = [f"{e}:{round((l['active'] / l['capacity']) * 100) if l['capacity'] > 0 else 0}%" for e, l in loads.items()]
    proj_prefix = f"<b>{project_num}</b> · " if project_num else ''
    return (
        f"📁 {proj_prefix}{client} / {folder_name}\n"
        f" {video_count} videos inside\n"
        f" Editor load: {' · '.join(load_hints)}\n"
        f" Suggested: <b>{suggested_editor}</b>\n\n"
        f"⚠️ <i>More videos may be added to this folder. You'll be notified if the count increases.</i>"
    )


def build_folder_keyboard(callback_key, editors):
    keyboard = [
        [InlineKeyboardButton("📋 Show Contents", callback_data=f"show:{callback_key}")],
        [InlineKeyboardButton(e, callback_data=f"assign:{callback_key}:{e}") for e in editors],
        [InlineKeyboardButton("🚫 Ignore", callback_data=f"ignore:{callback_key}")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ── Bot Handlers ──────────────────────────────────────────────────────────────

async def handle_show_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies with the video tree inside the folder."""
    query = update.callback_query
    await query.answer()

    callback_key = query.data[len('show:'):]
    pending = get_pending_item(callback_key)
    if not pending:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Folder data expired or already handled.",
        )
        return

    folder_name = pending.get('folder_name', 'Unknown')
    folder_id   = pending.get('folder_id', '')
    client      = pending.get('client', '')

    # Fetch fresh tree from Drive; update pending so a subsequent assign tap
    # reflects the current file count.
    fresh_count, fresh_tree, fresh_flat = fetch_folder_video_tree(folder_id, folder_name)
    if fresh_count is not None:
        all_pending = load_pending()
        all_pending[callback_key]['video_count'] = fresh_count
        all_pending[callback_key]['video_names'] = fresh_flat
        all_pending[callback_key]['video_tree']  = fresh_tree
        save_pending(all_pending)
        video_tree  = fresh_tree
        total_count = fresh_count
    else:
        video_tree  = pending.get('video_tree', {})
        total_count = pending.get('video_count', 0)

    drive_url    = f"https://drive.google.com/drive/folders/{folder_id}"
    display_name = f"{client} / {folder_name}" if client else folder_name
    label        = "video" if total_count == 1 else "videos"
    header       = f"📁 <a href='{drive_url}'>{display_name}</a> — {total_count} {label}"

    if not total_count:
        text = header + "\n\n📭 No video files found."
    elif not video_tree or len(video_tree) <= 1:
        # Flat list — single section or no tree (no grouping header needed)
        flat_names = fresh_flat if fresh_count is not None else pending.get('video_names', [])
        lines = [header, '']
        for i, name in enumerate(flat_names, 1):
            lines.append(f"{i}. {name}")
        text = '\n'.join(lines)
    else:
        # Grouped by subfolder
        lines = [header, '']
        counter = 1
        for section, names in video_tree.items():
            sec_label = "video" if len(names) == 1 else "videos"
            lines.append(f"📂 {section} ({len(names)} {sec_label}):")
            for name in names:
                lines.append(f"{counter}. {name}")
                counter += 1
            lines.append('')
        text = '\n'.join(lines).rstrip()

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=text,
        parse_mode='HTML',
    )


async def _clear_update_assignment_buttons(context, folder_id, editor_name):
    """Replace all stored update-notification messages with a clean assigned confirmation."""
    with PENDING_FOLDERS_LOCK:
        pending_folders = load_pending_folders()
        folder_data = pending_folders.get(folder_id)
        if not folder_data:
            return
        msg_ids = folder_data.get('update_message_ids', [])
        del pending_folders[folder_id]
        save_pending_folders(pending_folders)

    if not msg_ids:
        return

    config  = load_config()
    chat_id = config.get('notion_bridge_chat_id')
    clean_text = f"✅ Assigned to: {editor_name}"
    for msg_id in msg_ids:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=clean_text,
                reply_markup=InlineKeyboardMarkup([]),
            )
        except Exception as e:
            logger.error(f'Failed to clear update message {msg_id}: {e}')


async def handle_assignment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles editor button taps — creates Notion row, pings Discord, confirms to Vex."""
    query = update.callback_query

    parts = query.data.split(':')
    if parts[0] != 'assign':
        await query.answer()
        return

    config = load_config()
    notion_token = config.get('notion_token', '')

    if len(parts) == 3:
        # Original format: assign:{callback_key}:{editor}
        _, callback_key, editor = parts

        pending = get_pending_item(callback_key)
        if pending is None:
            await query.answer()
            await query.edit_message_text(
                text=query.message.text + "\n\n❌ Assignment expired or already handled.",
                parse_mode='HTML',
            )
            return

        # Duplicate tap — folder already assigned
        if pending.get('status') == 'assigned':
            folder_id = pending.get('folder_id', '')
            editor_name = get_active_queue_row_by_folder_id(notion_token, folder_id)
            if not editor_name:
                editor_name = pending.get('assigned_editor', 'unknown')
            msg = f"Already assigned to {editor_name} ✅"
            await query.answer(text=msg, show_alert=False)
            await context.bot.send_message(chat_id=query.message.chat_id, text=msg)
            return

        await query.answer()

        client      = pending['client']
        folder_name = pending['folder_name']
        folder_id   = pending['folder_id']
        video_count = pending['video_count']

        # Re-fetch current count from Drive so the Notion row and Discord ping
        # always reflect how many videos are actually in the folder right now.
        fresh_count, _ = fetch_folder_video_info(folder_id)
        if fresh_count is not None:
            video_count = fresh_count

        notion_page_id = assign_all_active_queue_rows(notion_token, folder_id, editor)
        if notion_page_id:
            logger.info(f"Updated all Raw rows to In Progress for {client}/{folder_name} → {editor}")
        else:
            notion_page_id = create_active_queue_folder_row(
                notion_token, folder_name, client, folder_id, video_count, editor,
                status='In Progress',
            )

        enqueue_discord_assignment(client, folder_name, video_count, folder_id, editor, notion_page_id)
        recalculate_active_videos(notion_token, editor)

        # Mark as assigned (keep entry so duplicate taps can identify the editor)
        all_pending = load_pending()
        if callback_key in all_pending:
            all_pending[callback_key]['status'] = 'assigned'
            all_pending[callback_key]['assigned_editor'] = editor
            save_pending(all_pending)

        loads = get_editor_loads(notion_token)
        load_line = ' · '.join(f"{e}:{round((loads[e]['active'] / loads[e]['capacity']) * 100) if loads[e]['capacity'] > 0 else 0}%" for e in loads)

        await query.edit_message_text(
            text=query.message.text + f"\n\n✅ <b>Assigned to {editor}</b>\n📊 Updated load: {load_line}",
            parse_mode='HTML',
        )

        # Clear any unassigned update-notification messages for this folder
        if folder_id:
            await _clear_update_assignment_buttons(context, folder_id, editor)

        logger.info(f"Assigned folder '{folder_name}' ({client}) to {editor}")

    elif len(parts) == 5:
        # Update-notification format: assign:{editor}:{folder_id}:{client_name}:{video_count}
        _, editor, folder_id, client_name, video_count_str = parts
        try:
            video_count = int(video_count_str)
        except ValueError:
            await query.answer()
            return

        with PENDING_FOLDERS_LOCK:
            pending_folders = load_pending_folders()
            folder_data = pending_folders.get(folder_id, {})
            folder_name = folder_data.get('folder_name', '')

        # If the pending_folders entry is gone, the folder was already assigned via another
        # button. Check Notion before overwriting — stale update-notification taps should be blocked.
        if not folder_data:
            existing_editor = get_active_queue_row_by_folder_id(notion_token, folder_id)
            if existing_editor:
                await query.answer(f"⚠️ Already assigned to {existing_editor}", show_alert=True)
                logger.info(
                    f"Blocked stale update-notification tap: {folder_id} already assigned to {existing_editor}"
                )
                return
            # Unassigned but entry is gone — look up folder_name from Notion
            if not folder_name:
                try:
                    _nq = requests.post(
                        f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query',
                        headers=notion_headers(notion_token),
                        json={'filter': {'property': 'Drive Link', 'url': {'contains': folder_id}}},
                        timeout=10,
                    )
                    if _nq.ok:
                        _pages = _nq.json().get('results', [])
                        if _pages:
                            _title_rt = _pages[0]['properties'].get('Video', {}).get('title', [])
                            folder_name = _title_rt[0].get('plain_text', '') if _title_rt else ''
                except Exception:
                    pass
                folder_name = folder_name or folder_id

        await query.answer()

        notion_page_id = assign_all_active_queue_rows(notion_token, folder_id, editor)
        if notion_page_id:
            logger.info(f"Updated all Raw rows to In Progress for {client_name}/{folder_name} → {editor} (update-notification)")
        else:
            notion_page_id = create_active_queue_folder_row(
                notion_token, folder_name, client_name, folder_id, video_count, editor,
                status='In Progress',
            )
        enqueue_discord_assignment(client_name, folder_name, video_count, folder_id, editor, notion_page_id)
        recalculate_active_videos(notion_token, editor)

        loads = get_editor_loads(notion_token)
        load_line = ' · '.join(f"{e}:{round((loads[e]['active'] / loads[e]['capacity']) * 100) if loads[e]['capacity'] > 0 else 0}%" for e in loads)

        await query.edit_message_text(
            text=query.message.text + f"\n\n✅ <b>Assigned to {editor}</b>\n📊 Updated load: {load_line}",
            parse_mode='HTML',
        )

        await _clear_update_assignment_buttons(context, folder_id, editor)

        logger.info(f"Assigned folder '{folder_name}' ({client_name}) to {editor} via update notification")

    else:
        await query.answer()


async def handle_ignore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vex taps 🚫 Ignore — save folder_id to ignored_folders.json and update ALL related messages."""
    query = update.callback_query
    await query.answer()

    callback_key = query.data[len('ignore:'):]
    pending      = get_pending_item(callback_key)
    if not pending:
        await query.edit_message_text(text="❌ Folder data expired.")
        return

    folder_id   = pending['folder_id']
    folder_name = pending['folder_name']

    add_ignored_folder(folder_id)
    logger.info(f"Ignored folder: {folder_name} ({folder_id})")

    # callback_data kept short — folder_id alone is ≤33 chars, safely under Telegram's 64-byte limit.
    # callback_key is recovered from pending_folders.json in handle_unignore_callback.
    unignore_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Unignore", callback_data=f"unignore:{folder_id}")]
    ])
    ignored_text = f"🚫 Ignored — {folder_name}"

    # Edit the message Vex tapped
    await query.edit_message_text(text=ignored_text, reply_markup=unignore_keyboard)

    # Also edit every other message related to this folder (original ping + update notifications)
    config  = load_config()
    chat_id = config.get('notion_bridge_chat_id')
    tapped_msg_id = query.message.message_id

    with PENDING_FOLDERS_LOCK:
        pending_folders = load_pending_folders()
        folder_data = pending_folders.get(folder_id, {})

    other_msg_ids = []
    orig_msg_id = folder_data.get('telegram_message_id')
    if orig_msg_id and orig_msg_id != tapped_msg_id:
        other_msg_ids.append(orig_msg_id)
    for mid in folder_data.get('update_message_ids', []):
        if mid != tapped_msg_id:
            other_msg_ids.append(mid)

    for mid in other_msg_ids:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=mid,
                text=ignored_text,
                reply_markup=unignore_keyboard,
            )
        except Exception as e:
            logger.warning(f"Could not edit related message {mid} on ignore: {e}")


async def handle_unignore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vex taps ↩️ Unignore — remove from ignored list and restore original notification."""
    query = update.callback_query
    await query.answer()

    folder_id = query.data[len('unignore:'):]

    remove_ignored_folder(folder_id)
    logger.info(f"Unignored folder: {folder_id}")

    # Recover callback_key from pending_folders.json (stored there by send_new_folder_notification)
    with PENDING_FOLDERS_LOCK:
        pending_folders = load_pending_folders()
        callback_key = pending_folders.get(folder_id, {}).get('callback_key', '')

    pending = get_pending_item(callback_key) if callback_key else None
    if not pending:
        await query.edit_message_text(text="↩️ Unignored — folder data expired, re-check pending.")
        return

    config       = load_config()
    notion_token = config.get('notion_token', '')
    loads        = get_editor_loads(notion_token)
    if not loads:
        await query.edit_message_text(text="↩️ Unignored — could not reload editor data.")
        return

    schedules = get_editor_schedules(notion_token)
    ranked    = _rank_editors(loads, schedules, datetime.now(EDT))
    suggested = ranked[0]['editor'] if ranked else next(iter(loads), '')
    pre_assigned = pending.get('pre_assigned')
    msg          = build_folder_notification_message(
        pending['client'], pending['folder_name'], pending['video_count'], suggested, loads
    )
    if pre_assigned:
        msg += f"\n\n🔖 <i>Folder rule suggests: {pre_assigned}</i>"

    keyboard = build_folder_keyboard(callback_key, list(loads.keys()))
    await query.edit_message_text(text=msg, parse_mode='HTML', reply_markup=keyboard)


async def handle_text_assignment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles text editor-name replies to assign the most recent pending folder."""
    text = update.message.text.strip()
    config = load_config()
    notion_token = config.get('notion_token', '')

    loads = get_editor_loads(notion_token)
    editors = list(loads.keys())

    if text not in editors:
        return

    pending = load_pending()
    if not pending:
        await update.message.reply_text("No pending assignments found.")
        return

    latest_key = sorted(pending.keys())[-1]
    item = pending[latest_key]

    client      = item['client']
    folder_name = item['folder_name']
    folder_id   = item['folder_id']
    video_count = item['video_count']

    notion_page_id = create_active_queue_folder_row(
        notion_token, folder_name, client, folder_id, video_count, text,
        status='In Progress',
    )
    enqueue_discord_assignment(client, folder_name, video_count, folder_id, text, notion_page_id)
    enqueue_creator_notify(client, folder_name, text, video_count, folder_id)
    recalculate_active_videos(notion_token, text)
    remove_pending(latest_key)

    loads = get_editor_loads(notion_token)
    load_line = ' · '.join(f"{e}:{round((loads[e]['active'] / loads[e]['capacity']) * 100) if loads[e]['capacity'] > 0 else 0}%" for e in editors if e in loads)

    await update.message.reply_text(
        f"✅ <b>{folder_name}</b> ({client}) assigned to <b>{text}</b>\n"
        f"📊 Load: {load_line}",
        parse_mode='HTML'
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>CC Video Manager — Telegram Commands</b>\n\n"

        "── <b>Queue</b> ──\n"
        "📊 <b>/load</b> — Editor load bars (active vs capacity).\n"
        "📁 <b>/pending</b> — Unassigned folders with inline assignment buttons.\n"
        "✅ <b>/today</b> — All deliveries completed today.\n"
        "🔍 <b>/pending_reviews</b> — Submissions waiting for your review.\n\n"

        "── <b>Editors & Clients</b> ──\n"
        "👤 <b>/editor &lt;name&gt;</b> — Stats + active folders for one editor.\n"
        "🎬 <b>/client &lt;name&gt;</b> — Active folders for a specific client.\n"
        "🔁 <b>/reassign</b> — Move an in-progress folder to a different editor.\n"
        "🗑️ <b>/remove</b> — Remove a folder from pending or active queue (cached).\n"
        "♻️ <b>/recover</b> — Restore a folder removed via /remove.\n\n"

        "── <b>Assignment</b> ──\n"
        "🤖 <b>/recommend</b> — Ranked editor list by availability + load.\n"
        "   When ON: new folders assign to the top recommendation automatically.\n"
        "   A single ↩️ Override button lets you swap if needed.\n\n"

        "── <b>Schedules</b> ──\n"
        "📅 <b>/schedule [name]</b> — View editor schedules from Notion.\n"
        "🕐 <b>/setschedule &lt;name&gt; &lt;day&gt; &lt;HH:MM-HH:MM&gt;</b> — Set working hours.\n"
        "   <i>Example:</i> /setschedule Karlo Monday 09:00-23:00\n"
        "   <i>Overnight:</i> /setschedule Karlo Friday 20:00-26:00\n"
        "❌ <b>/markoff &lt;name&gt; &lt;day&gt;</b> — Mark editor unavailable that day.\n"
        "👀 <b>/whosout</b> — See which editors are currently unavailable.\n\n"

        "── <b>Notes</b> ──\n"
        "📢 <b>/note &lt;message&gt;</b> — Send a note to all editors' Discord channels.\n"
        "📢 <b>/note &lt;name&gt; &lt;message&gt;</b> — Send a note to one editor only.\n\n"

        "<i>Assignment and review buttons appear automatically when new folders are "
        "detected or editors submit completions — no command needed.</i>"
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def cmd_load(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows current editor loads."""
    config = load_config()
    token = config.get('notion_token', '')
    loads = get_editor_loads(token)

    lines = ["📊 <b>Current Editor Load</b>\n"]
    for editor, l in loads.items():
        pct = int((l['active'] / l['capacity']) * 100) if l['capacity'] else 0
        bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
        status = '🔴' if pct >= 85 else '🟡' if pct >= 60 else '🟢'
        lines.append(f"{status} <b>{editor}</b>: {l['active']}/{l['capacity']} [{bar}] {pct}%")

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows unassigned folders (Status=Raw in Active Queue)."""
    config = load_config()
    token  = config.get('notion_token', '')
    body   = {
        'filter': {'property': 'Status', 'select': {'equals': 'Raw'}},
        'sorts':  [{'property': 'Submitted', 'direction': 'ascending'}],
    }
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    if not pages:
        await update.message.reply_text('✅ No unassigned folders.')
        return
    lines = [f"⏳ <b>{len(pages)} unassigned folder(s)</b>\n"]
    for p in pages:
        pr      = p['properties']
        folder  = (pr.get('Video', {}).get('title', [{}]) or [{}])[0].get('plain_text', '')
        client  = (pr.get('Creator', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
        notes   = (pr.get('Notes', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
        m       = re.search(r'Videos:\s*(\d+)', notes)
        videos  = int(m.group(1)) if m else 0
        dl      = (pr.get('Drive Link', {}).get('url') or '')
        fid_m   = re.search(r'/folders/([a-zA-Z0-9_-]+)', dl)
        pnum    = get_project_number(fid_m.group(1) if fid_m else '')
        prefix  = f"<b>{pnum}</b> · " if pnum else ''
        lines.append(f"• {prefix}{client} / {folder} — {videos} videos")
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows deliveries completed today."""
    config = load_config()
    token  = config.get('notion_token', '')
    now_edt      = datetime.now(EDT)
    today_str    = now_edt.strftime('%Y-%m-%d')
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    body = {'filter': {'and': [
        {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after': today_str}},
        {'property': DELIVERY_DATE_PROP, 'date': {'before': tomorrow_str}},
    ]}}
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    if not resp.ok:
        await update.message.reply_text('Notion error.')
        return
    pages = resp.json().get('results', [])
    if not pages:
        await update.message.reply_text(f'No deliveries today ({today_str}).')
        return
    total = 0
    lines = []
    for p in pages:
        pr = p['properties']
        folder = (pr.get('Folder', {}).get('title', [{}]) or [{}])[0].get('plain_text', '')
        client = (pr.get('Client', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
        editor_sel = pr.get('Editor', {}).get('select') or {}
        editor = editor_sel.get('name', '')
        videos = pr.get('Videos Completed', {}).get('number') or 0
        total += videos
        lines.append(f"• {editor}: {client} / {folder} — {videos}")
    header = f"✅ <b>Deliveries today</b> — {len(pages)} folders, {total} videos\n"
    await update.message.reply_text(header + '\n'.join(lines), parse_mode='HTML')


async def cmd_editor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows stats for a specific editor. Usage: /editor Name"""
    if not context.args:
        await update.message.reply_text('Usage: /editor <name>')
        return
    name  = ' '.join(context.args)
    config = load_config()
    token  = config.get('notion_token', '')

    # Find editor profile (case-insensitive prefix match)
    url  = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    profile, matched_name = None, name
    if resp.ok:
        for p in resp.json().get('results', []):
            pr = p['properties']
            ename_rt = pr.get('Editor', {}).get('title', [])
            ename = ename_rt[0].get('plain_text', '') if ename_rt else ''
            if ename.lower().startswith(name.lower()):
                profile      = pr
                matched_name = ename
                break
    if not profile:
        await update.message.reply_text(f'Editor not found: {name}')
        return

    active   = (profile.get('Active Videos', {}).get('number') or 0)
    capacity = (profile.get('Capacity', {}).get('number') or 70)
    week     = (profile.get('Delivered This Week', {}).get('number') or 0)
    month    = (profile.get('Delivered This Month', {}).get('number') or 0)
    total    = (profile.get('Total Videos Delivered', {}).get('number') or 0)
    pct      = round((active / capacity) * 100) if capacity else 0
    status   = '🔴' if pct >= 85 else '🟡' if pct >= 60 else '🟢'

    lines = [f"{status} <b>{matched_name}</b> — {active}/{capacity} ({pct}%)",
             f"This week: {week} · Month: {month} · Total: {total}"]

    # Active folders
    url2  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body2 = {'filter': {'and': [
        {'property': 'Editor', 'select': {'equals': matched_name}},
        {'property': 'Status', 'select': {'does_not_equal': 'Delivered'}},
    ]}}
    resp2 = requests.post(url2, headers=notion_headers(token), json=body2, timeout=15)
    if resp2.ok:
        active_pages = resp2.json().get('results', [])
        if active_pages:
            lines.append(f"\nActive ({len(active_pages)}):")
            for p in active_pages:
                pr     = p['properties']
                folder = (pr.get('Video', {}).get('title', [{}]) or [{}])[0].get('plain_text', '')
                client = (pr.get('Creator', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
                notes  = (pr.get('Notes', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
                m      = re.search(r'Videos:\s*(\d+)', notes)
                vids   = int(m.group(1)) if m else 0
                dl     = (pr.get('Drive Link', {}).get('url') or '')
                fid_m  = re.search(r'/folders/([a-zA-Z0-9_-]+)', dl)
                pnum   = get_project_number(fid_m.group(1) if fid_m else '')
                prefix = f"<b>{pnum}</b> · " if pnum else ''
                lines.append(f"• {prefix}{client} / {folder} — {vids} videos")

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def cmd_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows active folders for a client. Usage: /client Name"""
    if not context.args:
        await update.message.reply_text('Usage: /client <name>')
        return
    name   = ' '.join(context.args)
    config = load_config()
    token  = config.get('notion_token', '')
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {
        'filter': {'property': 'Creator', 'rich_text': {'contains': name}},
        'sorts':  [{'property': 'Submitted', 'direction': 'ascending'}],
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    if not resp.ok:
        await update.message.reply_text('Notion error.')
        return
    pages = [p for p in resp.json().get('results', [])
             if (p['properties'].get('Status', {}).get('select') or {}).get('name') != 'Delivered']
    if not pages:
        await update.message.reply_text(f'No active folders for: {name}')
        return
    lines = [f"📋 <b>{name}</b> — {len(pages)} active folder(s)"]
    for p in pages:
        pr       = p['properties']
        folder   = (pr.get('Video', {}).get('title', [{}]) or [{}])[0].get('plain_text', '')
        status   = (pr.get('Status', {}).get('select') or {}).get('name', '')
        editor_s = (pr.get('Editor', {}).get('select') or {}).get('name', 'unassigned')
        notes    = (pr.get('Notes', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
        m        = re.search(r'Videos:\s*(\d+)', notes)
        vids     = int(m.group(1)) if m else 0
        dl       = (pr.get('Drive Link', {}).get('url') or '')
        fid_m    = re.search(r'/folders/([a-zA-Z0-9_-]+)', dl)
        pnum     = get_project_number(fid_m.group(1) if fid_m else '')
        prefix   = f"<b>{pnum}</b> · " if pnum else ''
        lines.append(f"• {prefix}{folder} — {editor_s} — {status} — {vids} videos")
    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


# ── Reassign command (Telegram) ──────────────────────────────────────────────

def _fetch_in_progress_folders(token):
    """Returns list of {notion_page_id, folder_id, folder_name, client_name, editor_name, video_count}."""
    body = {'filter': {'property': 'Status', 'select': {'equals': 'In Progress'}}}
    rows = []
    for page in notion_query_all(token, ACTIVE_QUEUE_DB, body):
            props  = page['properties']
            folder = (props.get('Video', {}).get('title', [{}]) or [{}])[0].get('plain_text', '')
            client = (props.get('Creator', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
            editor = (props.get('Editor', {}).get('select') or {}).get('name', '')
            notes  = (props.get('Notes', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
            m      = re.search(r'Videos:\s*(\d+)', notes)
            vids   = int(m.group(1)) if m else 0
            dl     = props.get('Drive Link', {}).get('url') or ''
            m2     = re.search(r'/folders/([a-zA-Z0-9_-]+)', dl)
            fid    = m2.group(1) if m2 else ''
            rows.append({
                'notion_page_id': page['id'],
                'folder_id':      fid,
                'folder_name':    folder,
                'client_name':    client,
                'editor_name':    editor,
                'video_count':    vids,
            })
    return rows


def _fetch_pending_folders(token):
    """Returns list of {notion_page_id, folder_id, folder_name, client_name, editor_name, video_count} for Status=Raw."""
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
    rows = []
    for page in notion_query_all(token, ACTIVE_QUEUE_DB, body):
            props  = page['properties']
            folder = (props.get('Video', {}).get('title', [{}]) or [{}])[0].get('plain_text', '')
            client = (props.get('Creator', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
            editor = (props.get('Editor', {}).get('select') or {}).get('name', '')
            notes  = (props.get('Notes', {}).get('rich_text', [{}]) or [{}])[0].get('plain_text', '')
            m      = re.search(r'Videos:\s*(\d+)', notes)
            vids   = int(m.group(1)) if m else 0
            dl     = props.get('Drive Link', {}).get('url') or ''
            m2     = re.search(r'/folders/([a-zA-Z0-9_-]+)', dl)
            fid    = m2.group(1) if m2 else ''
            rows.append({
                'notion_page_id': page['id'],
                'folder_id':      fid,
                'folder_name':    folder,
                'client_name':    client,
                'editor_name':    editor,
                'video_count':    vids,
            })
    return rows


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows inline keyboard of all Pending (Raw) and Active (In Progress) folders for removal."""
    config = load_config()
    token  = config.get('notion_token', '')
    pending = [{**r, 'status': 'Pending'} for r in _fetch_pending_folders(token)]
    active  = [{**r, 'status': 'Active'}  for r in _fetch_in_progress_folders(token)]
    rows    = pending + active

    if not rows:
        await update.message.reply_text('No pending or active folders to remove.')
        return

    keyboard = []
    for r in rows:
        tag   = '⏳' if r['status'] == 'Pending' else '🔧'
        label = f"{tag} {r['client_name']} / {r['folder_name']} ({r['status']})"[:64]
        data  = f"rm:{r['notion_page_id']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=data)])

    await update.message.reply_text(
        '🗑️ Which folder to remove? (cached — use /recover to restore)',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Archives the selected Active Queue row and caches it for /recover."""
    query = update.callback_query
    await query.answer()

    _, notion_page_id = query.data.split(':', 1)

    config = load_config()
    token  = config.get('notion_token', '')

    pending = {r['notion_page_id']: {**r, 'status': 'Pending'} for r in _fetch_pending_folders(token)}
    active  = {r['notion_page_id']: {**r, 'status': 'Active'}  for r in _fetch_in_progress_folders(token)}
    row     = pending.get(notion_page_id) or active.get(notion_page_id)

    if not row:
        await query.edit_message_text('Folder not found (maybe already removed).')
        return

    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{notion_page_id}',
        headers=notion_headers(token),
        json={'archived': True},
        timeout=15,
    )
    if not resp.ok:
        await query.edit_message_text('Notion error — could not remove folder.')
        return

    cache_removed_folder(notion_page_id, row, row['status'])
    pop_deadline_entry(row.get('folder_id', ''), notion_page_id)
    await query.edit_message_text(
        f"🗑️ Removed <b>{row['client_name']} / {row['folder_name']}</b> ({row['status']}).\n"
        f"Use /recover to restore it.",
        parse_mode='HTML',
    )


async def cmd_recover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows inline keyboard of recently removed folders to restore."""
    data = load_removed_folders()
    if not data:
        await update.message.reply_text('No removed folders to recover.')
        return

    keyboard = []
    for page_id, row in data.items():
        tag   = '⏳' if row['status'] == 'Pending' else '🔧'
        label = f"{tag} {row['client_name']} / {row['folder_name']} ({row['status']})"[:64]
        keyboard.append([InlineKeyboardButton(label, callback_data=f"rc:{page_id}")])

    await update.message.reply_text(
        '♻️ Which folder to recover?',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_recover_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Un-archives the selected folder's Active Queue row and removes it from the cache."""
    query = update.callback_query
    await query.answer()

    _, notion_page_id = query.data.split(':', 1)

    row = pop_removed_folder(notion_page_id)
    if row is None:
        await query.edit_message_text('That folder is no longer in the removed cache.')
        return

    config = load_config()
    token  = config.get('notion_token', '')

    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{notion_page_id}',
        headers=notion_headers(token),
        json={'archived': False},
        timeout=15,
    )
    if not resp.ok:
        # Put it back in the cache so the user can retry
        cache_removed_folder(notion_page_id, row, row['status'])
        await query.edit_message_text('Notion error — could not recover folder.')
        return

    await query.edit_message_text(
        f"♻️ Recovered <b>{row['client_name']} / {row['folder_name']}</b> ({row['status']}).",
        parse_mode='HTML',
    )


async def cmd_reassign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows inline keyboard of all In Progress folders for reassignment."""
    config = load_config()
    token  = config.get('notion_token', '')
    rows   = _fetch_in_progress_folders(token)

    if not rows:
        await update.message.reply_text('No folders currently In Progress.')
        return

    keyboard = []
    for r in rows:
        label = f"{r['client_name']} / {r['folder_name']} ({r['editor_name'] or 'unassigned'})"[:64]
        # encode: reassign_folder:{notion_page_id}:{folder_id}:{client}:{folder_name}
        # Keep callback_data ≤64 bytes — use short keys
        data  = f"rf:{r['notion_page_id']}:{r['folder_id']}:{r['video_count']}"
        keyboard.append([InlineKeyboardButton(label, callback_data=data)])

    await update.message.reply_text(
        '📁 Which folder to reassign?',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_reassign_folder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a folder — show editor selection."""
    query = update.callback_query
    await query.answer()

    # rf:{notion_page_id}:{folder_id}:{video_count}
    _, notion_page_id, folder_id, video_count_str = query.data.split(':', 3)
    video_count = int(video_count_str) if video_count_str.isdigit() else 0

    config  = load_config()
    token   = config.get('notion_token', '')
    rows    = _fetch_in_progress_folders(token)
    row     = next((r for r in rows if r['notion_page_id'] == notion_page_id), {})
    client  = row.get('client_name', '')
    folder  = row.get('folder_name', '')

    editors = get_editor_loads(token)
    keyboard = []
    for editor_name in editors:
        data = f"re:{notion_page_id}:{folder_id}:{video_count}:{editor_name}"
        keyboard.append([InlineKeyboardButton(editor_name, callback_data=data)])

    await query.edit_message_text(
        f'Reassigning <b>{client} / {folder}</b>\nPick new editor:',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_reassign_editor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User picked a new editor — update Notion and enqueue Discord notification."""
    query = update.callback_query
    await query.answer()

    # re:{notion_page_id}:{folder_id}:{video_count}:{editor_name}
    parts          = query.data.split(':', 4)
    _, notion_page_id, folder_id, video_count_str, new_editor = parts
    video_count    = int(video_count_str) if video_count_str.isdigit() else 0

    config = load_config()
    token  = config.get('notion_token', '')

    rows       = _fetch_in_progress_folders(token)
    row        = next((r for r in rows if r['notion_page_id'] == notion_page_id), {})
    client     = row.get('client_name', '')
    folder     = row.get('folder_name', '')
    old_editor = row.get('editor_name', '')

    # Update Notion
    requests.patch(
        f'https://api.notion.com/v1/pages/{notion_page_id}',
        headers=notion_headers(token),
        json={'properties': {
            'Editor': {'select': {'name': new_editor}},
            'Status': {'select': {'name': 'In Progress'}},
        }},
        timeout=15,
    )

    # Update deadline owner if tracked
    deadlines_path = os.path.join(BASE_DIR, 'deadlines.json')
    if folder_id and os.path.exists(deadlines_path):
        with open(deadlines_path) as f:
            deadlines = json.load(f)
        if folder_id in deadlines:
            import time as _time
            deadlines[folder_id]['editor_name'] = new_editor
            due_ts = deadlines[folder_id].get('due_ts')
            if not deadlines[folder_id].get('indefinite') and due_ts and (due_ts - _time.time()) > 6 * 3600:
                deadlines[folder_id]['warned_6h'] = False
            with open(deadlines_path, 'w') as f:
                json.dump(deadlines, f, indent=2)

    # Recalculate active videos for both editors
    recalculate_active_videos(token, new_editor)
    if old_editor and old_editor != new_editor:
        recalculate_active_videos(token, old_editor)

    # Enqueue Discord notification to new editor
    enqueue_discord_assignment(client, folder, video_count, folder_id, new_editor, notion_page_id)

    # Notify creator + old editor via Discord IPC
    _append_to_discord_queue({
        'type':        'reassign_notify',
        'client_name': client,
        'folder_name': folder,
        'old_editor':  old_editor,
        'new_editor':  new_editor,
    })

    await query.edit_message_text(
        f'✅ <b>{client} / {folder}</b> reassigned to <b>{new_editor}</b>.',
        parse_mode='HTML',
    )
    logger.info(f'Telegram reassign: {folder} ({client}) → {new_editor}')


# ── Pending Reviews (written by discord_bot.py, read here) ───────────────────

def load_pending_reviews():
    """Load pending_reviews.json, purging resolved entries older than 7 days."""
    if not os.path.exists(PENDING_REVIEWS_FILE):
        return {}
    with PENDING_REVIEWS_LOCK:
        with open(PENDING_REVIEWS_FILE) as f:
            reviews = json.load(f)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    cleaned, changed = {}, False
    for rid, rv in reviews.items():
        if rv.get('status') == 'resolved':
            try:
                ca = datetime.fromisoformat(rv['created_at'])
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                if ca < cutoff:
                    changed = True
                    continue
            except Exception:
                pass
        cleaned[rid] = rv
    if changed:
        with PENDING_REVIEWS_LOCK:
            with open(PENDING_REVIEWS_FILE, 'w') as f:
                json.dump(cleaned, f, indent=2)
    return cleaned


def get_pending_review(review_id):
    return load_pending_reviews().get(review_id)


def resolve_pending_review(review_id):
    with PENDING_REVIEWS_LOCK:
        reviews = {}
        if os.path.exists(PENDING_REVIEWS_FILE):
            with open(PENDING_REVIEWS_FILE) as f:
                reviews = json.load(f)
        if review_id in reviews:
            reviews[review_id]['status'] = 'resolved'
            with open(PENDING_REVIEWS_FILE, 'w') as f:
                json.dump(reviews, f, indent=2)


# ── Finalize delivery (Notion updates + Discord queue item) ───────────────────

def finalize_notion_delivery(review, confirmed_count):
    config = load_config()
    token  = config['notion_token']
    notion_page_id = review.get('notion_page_id')
    editor_page_id = review.get('editor_page_id')
    edited_folder  = review.get('edited_folder', '')
    editor_name    = review.get('editor_name', 'Unknown')
    client_name    = review.get('client_name', 'Unknown')
    folder_name_r  = review.get('folder_name', 'Unknown')
    now_edt        = datetime.now(EDT)
    today_str      = now_edt.strftime('%Y-%m-%d')
    logger.info(f"finalize_notion_delivery() called for {editor_name}, count={confirmed_count}, folder={folder_name_r}")

    drive_link = ''
    if notion_page_id:
        resp = requests.get(
            f'https://api.notion.com/v1/pages/{notion_page_id}',
            headers=notion_headers(token), timeout=15,
        )
        if resp.ok:
            page_props  = resp.json().get('properties', {})
            drive_link = page_props.get('Drive Link', {}).get('url') or ''

        requests.patch(
            f'https://api.notion.com/v1/pages/{notion_page_id}',
            headers=notion_headers(token),
            json={'properties': {
                'Status':             {'select':    {'name': 'Delivered'}},
                'Videos Completed':   {'number':    confirmed_count},
                'Edited Folder Name': {'rich_text': [{'text': {'content': edited_folder}}]},
                'Delivered':          {'date':      {'start': today_str}},
            }},
            timeout=15,
        )

    # Resolve editor page ID — use stored ID if available, else look up by editor name
    if not editor_page_id and editor_name and editor_name != 'Unknown':
        loads = get_editor_loads(token)
        editor_page_id = loads.get(editor_name, {}).get('page_id', '')

    if editor_page_id:
        get_resp = requests.get(
            f'https://api.notion.com/v1/pages/{editor_page_id}',
            headers=notion_headers(token), timeout=15,
        )
        if not get_resp.ok:
            logger.error(f"finalize_notion_delivery: GET Editor Profiles failed for {editor_name}: {get_resp.status_code} {get_resp.text}")
        props = get_resp.json().get('properties', {}) if get_resp.ok else {}
        week  = props.get('Delivered This Week',    {}).get('number') or 0
        month = props.get('Delivered This Month',   {}).get('number') or 0
        total = props.get('Total Videos Delivered', {}).get('number') or 0
        new_week  = week  + confirmed_count
        new_month = month + confirmed_count
        logger.info(f"Before update — {editor_name} This Week: {week}, This Month: {month}")
        patch_resp = requests.patch(
            f'https://api.notion.com/v1/pages/{editor_page_id}',
            headers=notion_headers(token),
            json={'properties': {
                'Delivered This Week':    {'number': new_week},
                'Delivered This Month':   {'number': new_month},
                'Total Videos Delivered': {'number': total + confirmed_count},
            }},
            timeout=15,
        )
        if patch_resp.ok:
            logger.info(f"After update — {editor_name} This Week: {new_week}, This Month: {new_month}")
        else:
            logger.error(f"Failed to update Editor Profiles for {editor_name}: {patch_resp.status_code} {patch_resp.text}")
        recalculate_active_videos(token, editor_name)
    else:
        logger.warning(f'finalize_notion_delivery: no editor_page_id resolved for {editor_name}, skipping stats update')

    # Clear deadline so discord_bot deadline_checker stops warning after delivery
    m_dl = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
    raw_folder_id_dl = m_dl.group(1) if m_dl else review.get('folder_id')
    if raw_folder_id_dl and os.path.exists(DEADLINES_FILE):
        try:
            with open(DEADLINES_FILE) as _f:
                _dl = json.load(_f)
            if raw_folder_id_dl in _dl:
                del _dl[raw_folder_id_dl]
                with open(DEADLINES_FILE, 'w') as _f:
                    json.dump(_dl, _f, indent=2)
                logger.info(f'finalize_notion_delivery: cleared deadline for {raw_folder_id_dl}')
        except Exception as _e:
            logger.error(f'finalize_notion_delivery: failed to clear deadline: {_e}')

    create_delivery_history_row(
        token,
        folder_name_r,
        client_name,
        editor_name,
        confirmed_count,
        today_str,
        edited_folder,
        drive_link,
    )

    # Completion notification → Discord ops channel
    _send_discord_ops_channel(config, embed={
        'title': '🎬 Delivery',
        'description': f"**{editor_name}** completed **{confirmed_count}** videos\n"
                       f"Client: **{client_name} / {folder_name_r}**\n"
                       f"Delivered: {to_ist(now_edt)}",
        'color': 0x2ecc71,
    })

    # Build the edited folder Drive link for the creator notification
    edited_folder_drive_link = None
    if drive_link:
        m = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        if m:
            raw_folder_id = m.group(1)
            edited_folder_id = find_edited_folder_id_from_raw(raw_folder_id)
            if edited_folder_id:
                edited_folder_drive_link = f'https://drive.google.com/drive/folders/{edited_folder_id}'

    try:
        payload = {
            'type':                     'creator_complete_notify',
            'client_name':              client_name,
            'folder_name':              folder_name_r,
            'editor_name':              editor_name,
            'confirmed_count':          confirmed_count,
            'edited_folder':            edited_folder,
            'edited_folder_drive_link': edited_folder_drive_link,
        }
        logger.info(f"creator_complete_notify payload from notion_bridge: {payload}")
        _append_to_discord_queue(payload)
    except Exception as e:
        logger.error(f'Failed to enqueue creator complete notify: {e}')

    return {'drive_link': drive_link, 'edited_folder_drive_link': edited_folder_drive_link}


def enqueue_discord_finalize(discord_message_id, discord_channel_id, confirmed_count,
                              drive_link=None, edited_folder_drive_link=None):
    try:
        _append_to_discord_queue({
            'type':                     'finalize',
            'discord_message_id':       discord_message_id,
            'discord_channel_id':       discord_channel_id,
            'confirmed_count':          confirmed_count,
            'drive_link':               drive_link,
            'edited_folder_drive_link': edited_folder_drive_link,
        })
    except Exception as e:
        logger.error(f'Failed to enqueue Discord finalize: {e}')


# ── Review / Count callback handlers ─────────────────────────────────────────

async def handle_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vex taps [🔍 Review] — show flags detail and Accept/Drive count buttons."""
    query = update.callback_query
    await query.answer()

    review_id = query.data[len('review:'):]
    review = get_pending_review(review_id)
    if not review:
        await query.edit_message_text(text=query.message.text + "\n\n❌ Review data expired.")
        return

    videos_done   = review['videos_done']
    drive_count   = review.get('drive_count')
    drive_str     = str(drive_count) if drive_count is not None else 'N/A'

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"✅ Accept Editor Count ({videos_done})",
                callback_data=f"accept_count:{review_id}:{videos_done}",
            )
        ],
        [
            InlineKeyboardButton(
                f"📁 Use Drive Count ({drive_str})",
                callback_data=f"use_drive_count:{review_id}:{drive_count if drive_count is not None else 0}",
            )
        ],
    ])

    await query.edit_message_text(
        text=query.message.text + f"\n\n🔍 <b>Review</b> — choose which count to finalize:",
        parse_mode='HTML',
        reply_markup=keyboard,
    )


async def handle_count_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vex taps Accept Editor Count or Use Drive Count."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(':')
    action          = parts[0]
    review_id       = parts[1]
    confirmed_count = int(parts[2])

    review = get_pending_review(review_id)
    if not review:
        await query.edit_message_text(text=query.message.text + "\n\n❌ Review data expired.")
        return

    editor_name = review['editor_name']
    folder_name = review['folder_name']

    link_info = finalize_notion_delivery(review, confirmed_count)
    enqueue_discord_finalize(
        review['discord_message_id'],
        review['discord_channel_id'],
        confirmed_count,
        drive_link=link_info.get('drive_link'),
        edited_folder_drive_link=link_info.get('edited_folder_drive_link'),
    )
    resolve_pending_review(review_id)

    await query.edit_message_text(
        text=query.message.text + f"\n\n✅ Finalized with {confirmed_count} videos.",
        parse_mode='HTML',
    )
    logger.info(f"Finalized via Telegram: {folder_name} — {confirmed_count} videos by {editor_name}")


async def cmd_pending_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all unresolved pending reviews so Vex can finalize them even after Telegram timeout."""
    reviews = load_pending_reviews()
    pending = {
        rid: rv for rid, rv in reviews.items()
        if rv.get('status', 'pending') == 'pending'
    }
    if not pending:
        await update.message.reply_text("✅ No pending reviews.")
        return

    lines = ["⏳ <b>Pending Reviews:</b>\n"]
    keyboard = []
    for i, (rid, rv) in enumerate(
        sorted(pending.items(), key=lambda x: x[1].get('created_at', '')), 1
    ):
        editor_name = rv.get('editor_name', '?')
        client_name = rv.get('client_name', '?')
        folder_name = rv.get('folder_name', '?')
        videos_done = rv.get('videos_done', 0)
        drive_count = rv.get('drive_count')
        drive_str   = str(drive_count) if drive_count is not None else 'N/A'
        lines.append(f"{i}. {editor_name} — {client_name} / {folder_name} — {videos_done} videos")
        keyboard.append([
            InlineKeyboardButton(
                f"✅ Accept ({videos_done})",
                callback_data=f"accept_count:{rid}:{videos_done}",
            ),
            InlineKeyboardButton(
                f"📁 Drive ({drive_str})",
                callback_data=f"use_drive_count:{rid}:{drive_count if drive_count is not None else 0}",
            ),
        ])

    await update.message.reply_text(
        '\n'.join(lines),
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Recommendation & Auto-assign commands ────────────────────────────────────

async def cmd_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/recommend — ranked editor list weighted by load + current availability."""
    config = load_config()
    ranked = get_recommendation(config.get('notion_token', ''))

    if not ranked:
        await update.message.reply_text('No editors found.')
        return

    lines = ['🤖 <b>Editor Recommendation</b>\n']
    for i, r in enumerate(ranked, 1):
        pct    = round(r['ratio'] * 100)
        status = '🔴' if pct >= 85 else '🟡' if pct >= 60 else '🟢'
        if r['available_now'] and r['has_schedule']:
            avail_str = 'in shift'
        elif not r['has_schedule']:
            avail_str = 'no schedule set'
        elif r['mins_until'] is not None:
            h, m = divmod(int(r['mins_until']), 60)
            avail_str = f"available in {h}h {m}m" if h else f"available in {m}m"
        else:
            avail_str = 'out of shift'
        arrow = '  ← pick this' if i == 1 else ''
        lines.append(f"{i}. {status} <b>{r['editor']}</b> — {pct}% load, {avail_str}{arrow}")

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ask <question> — ask the AI ops assistant anything about editor availability, load, etc."""
    question = ' '.join(context.args).strip() if context.args else ''
    if not question:
        await update.message.reply_text(
            '❓ Usage: /ask <question>\n\nExamples:\n'
            '• /ask who is available right now\n'
            '• /ask who can take a folder in 2 hours\n'
            '• /ask who has the lightest load today'
        )
        return

    config       = load_config()
    notion_token = config.get('notion_token', '')
    loads        = get_editor_loads(notion_token)
    schedules    = get_editor_schedules(notion_token)
    ranked       = _rank_editors(loads, schedules, datetime.now(EDT))
    profile_schedules = ai_ops.fetch_schedules_from_profiles(notion_token)

    ctx_str = ai_ops.build_context_from_ranked(ranked, profile_schedules=profile_schedules)
    logger.info(f'cmd_ask question="{question}" schedules={list(profile_schedules.keys())}')
    msg    = await update.message.reply_text('🤔 Thinking…')
    answer = ai_ops.ai_answer_query(ctx_str, question, profile_schedules=profile_schedules)
    logger.info(f'cmd_ask answer="{answer[:120]}"')
    await msg.edit_text(f'🤖 <b>AI Ops</b>\n\n{answer}', parse_mode='HTML')


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/schedule [EditorName] — show editor schedules from Notion."""
    config    = load_config()
    token     = config.get('notion_token', '')
    schedules = get_editor_schedules(token)

    filter_name = ' '.join(context.args).strip().lower() if context.args else ''

    if not schedules:
        await update.message.reply_text('No schedules found in Notion.')
        return

    lines = ['📅 <b>Editor Schedules (EDT)</b>\n']
    for editor, rows in sorted(schedules.items()):
        if filter_name and not editor.lower().startswith(filter_name):
            continue
        day_rows = sorted(rows, key=lambda r: DAYS_OF_WEEK.index(r['day']) if r['day'] in DAYS_OF_WEEK else 99)
        lines.append(f'<b>{editor}</b>')
        for r in day_rows:
            mark     = '✅' if r['available'] else '❌'
            time_str = f"{r['start']} – {r['end']}" if r['start'] and r['end'] else 'no times set'
            lines.append(f"  {mark} {r['day']}: {time_str}")
        lines.append('')

    await update.message.reply_text('\n'.join(lines).strip(), parse_mode='HTML')


async def cmd_setschedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setschedule EditorName Day HH:MM-HH:MM — create/update a schedule row in Notion.
    Example: /setschedule Karlo Monday 09:00-23:00
    Overnight: /setschedule Karlo Friday 20:00-26:00  (26:00 = 02:00 next day)"""
    if len(context.args) < 3:
        await update.message.reply_text(
            'Usage: /setschedule EditorName Day HH:MM-HH:MM\n'
            'Example: /setschedule Karlo Monday 09:00-23:00\n'
            'Overnight: /setschedule Karlo Friday 20:00-26:00\n\n'
            f'Valid days: {", ".join(DAYS_OF_WEEK)}',
        )
        return

    config = load_config()
    token  = config.get('notion_token', '')

    editor_name = context.args[0]
    day         = context.args[1].capitalize()
    time_range  = context.args[2]

    if day not in DAYS_OF_WEEK:
        await update.message.reply_text(f'Invalid day: {day}\nValid: {", ".join(DAYS_OF_WEEK)}')
        return
    if '-' not in time_range:
        await update.message.reply_text('Time range must be HH:MM-HH:MM  e.g. 09:00-23:00')
        return

    start, end = [t.strip() for t in time_range.split('-', 1)]

    # Resolve editor name (case-insensitive)
    loads = get_editor_loads(token)
    if editor_name not in loads:
        match = next((e for e in loads if e.lower() == editor_name.lower()), None)
        if match:
            editor_name = match
        else:
            await update.message.reply_text(
                f'Editor not found: {editor_name}\nKnown editors: {", ".join(loads.keys())}'
            )
            return

    page_id = _upsert_schedule_row(token, editor_name, day, start, end, available=True)
    if page_id:
        await update.message.reply_text(
            f'✅ Schedule set: <b>{editor_name}</b> — {day}: {start} – {end} EDT',
            parse_mode='HTML',
        )
    else:
        await update.message.reply_text('❌ Failed to update Notion. Check logs.')


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/note [EditorName] message — send a short note to one editor or all editors via Discord.
    Examples:
      /note Please wrap up all folders by EOD          ← sent to everyone
      /note Karlo Can you prioritise the Julia folder  ← sent only to Karlo"""
    if not context.args:
        await update.message.reply_text(
            'Usage:\n'
            '/note message              — send to all editors\n'
            '/note EditorName message   — send to one editor'
        )
        return

    config = load_config()
    token  = config.get('notion_token', '')
    loads  = get_editor_loads(token)

    # If first word matches a known editor name (case-insensitive), target them
    first_word = context.args[0]
    match = next((e for e in loads if e.lower() == first_word.lower()), None)
    if match:
        targets = [match]
        message = ' '.join(context.args[1:]).strip()
    else:
        targets = []  # empty = all editors
        message = ' '.join(context.args).strip()

    if not message:
        await update.message.reply_text('Message cannot be empty.')
        return

    try:
        _append_to_discord_queue({
            'type':    'announce',
            'message': message,
            'targets': targets,
        })
    except Exception as e:
        logger.error(f'Failed to enqueue announce: {e}')
        await update.message.reply_text('❌ Failed to send note. Check logs.')
        return

    if targets:
        await update.message.reply_text(f'📢 Note sent to <b>{targets[0]}</b>.', parse_mode='HTML')
    else:
        await update.message.reply_text(f'📢 Note sent to <b>all editors</b>.', parse_mode='HTML')


async def cmd_markoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/markoff EditorName Day — mark an editor as unavailable on that day.
    Example: /markoff Karlo Saturday"""
    if len(context.args) < 2:
        await update.message.reply_text('Usage: /markoff EditorName Day\nExample: /markoff Karlo Saturday')
        return

    config = load_config()
    token  = config.get('notion_token', '')

    editor_name = context.args[0]
    day         = context.args[1].capitalize()

    if day not in DAYS_OF_WEEK:
        await update.message.reply_text(f'Invalid day: {day}\nValid: {", ".join(DAYS_OF_WEEK)}')
        return

    loads = get_editor_loads(token)
    if editor_name not in loads:
        match = next((e for e in loads if e.lower() == editor_name.lower()), None)
        if match:
            editor_name = match
        else:
            await update.message.reply_text(f'Editor not found: {editor_name}')
            return

    # Preserve existing times if the row already exists
    schedules = get_editor_schedules(token)
    existing  = next((r for r in schedules.get(editor_name, []) if r['day'] == day), None)
    start = existing['start'] if existing else ''
    end   = existing['end']   if existing else ''

    page_id = _upsert_schedule_row(token, editor_name, day, start, end, available=False)
    if page_id:
        await update.message.reply_text(
            f'❌ <b>{editor_name}</b> marked <b>unavailable</b> on <b>{day}</b>.',
            parse_mode='HTML',
        )
    else:
        await update.message.reply_text('Failed to update Notion. Check logs.')


async def cmd_whosout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whosout — show which editors are currently marked unavailable."""
    config    = load_config()
    token     = config.get('notion_token', '')
    schedules = get_editor_schedules(token)
    now_edt   = datetime.now(EDT)

    if not schedules:
        await update.message.reply_text('No schedule data found in Notion.')
        return

    lines = [f'📋 <b>Editor Availability ({now_edt.strftime("%a %H:%M")} EDT)</b>\n']
    for editor in sorted(schedules.keys()):
        avail = is_editor_available(editor, schedules, now_edt)
        rows_today = [r for r in schedules[editor] if r['day'] == now_edt.strftime('%A')]
        if avail:
            mark = '✅'
            note = ''
        else:
            mark = '❌'
            if rows_today:
                note = ' — marked off today'
            else:
                note = ' — no shift scheduled'
        lines.append(f'{mark} <b>{editor}</b>{note}')

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


async def handle_override_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vex taps ↩️ Override on an auto-assigned notification — show editor selection keyboard."""
    query = update.callback_query
    await query.answer()

    # callback_data: override:{notion_page_id}:{folder_id}:{video_count}
    parts = query.data.split(':', 3)
    if len(parts) < 4:
        return
    _, notion_page_id, folder_id, video_count_str = parts
    video_count = int(video_count_str) if video_count_str.isdigit() else 0

    config  = load_config()
    token   = config.get('notion_token', '')
    editors = get_editor_loads(token)

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"re:{notion_page_id}:{folder_id}:{video_count}:{name}")]
        for name in editors
    ]

    await query.edit_message_text(
        query.message.text + '\n\n🔄 Pick a different editor:',
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── New Folder Notification (called from gdrive_watcher.py) ──────────────────

def send_new_folder_notification(config, folder_info):
    """
    Called by gdrive_watcher when a new subfolder is detected in Raw Footage.
    Sends a Telegram message with Show Contents + editor assignment buttons.
    """
    send_token   = config['notion_bridge_token']
    chat_id      = config['notion_bridge_chat_id']
    notion_token = config.get('notion_token', '')

    client      = folder_info['client']
    folder_name = folder_info['folder_name']
    folder_id   = folder_info['folder_id']
    video_count = folder_info['video_count']
    video_names = folder_info.get('video_names', [])
    video_tree  = folder_info.get('video_tree', {})

    loads = get_editor_loads(notion_token)
    if not loads:
        url = f"https://api.telegram.org/bot{send_token}/sendMessage"
        _send_telegram(url, {
            'chat_id': chat_id,
            'text': '⚠️ Notion API unavailable — cannot assign editor right now',
        })
        logger.error(f'Notion API unavailable, could not create Active Queue row for {client}/{folder_name}')
        return

    # Assign project number on first detection
    project_num = assign_project_number(folder_id)

    # Create Active Queue row with Status=Raw so unassigned folders are visible in /stats
    _ops_page_id = ''
    _is_new_folder = False
    existing_page_id = get_active_queue_page_id_by_folder_id(notion_token, folder_id)
    if not existing_page_id:
        raw_page_id = create_active_queue_folder_row(
            notion_token, folder_name, client, folder_id, video_count, status='Raw',
            project_number=project_num,
        )
        _ops_page_id = raw_page_id
        _is_new_folder = True
        logger.info(f"Created Raw Active Queue row for {client}/{folder_name}, page_id={raw_page_id}")
        # Ping creator only once — on first detection
        enqueue_creator_detected(client, folder_name, video_count, folder_id)
    else:
        _ops_page_id = existing_page_id
        logger.info(f"Active Queue row already exists for {client}/{folder_name}, skipping Raw creation")

    # Ranking is a suggestion only — it orders the buttons and picks the name
    # shown as recommended. Assignment always waits for a human tap.
    schedules = get_editor_schedules(notion_token)
    ranked    = _rank_editors(loads, schedules, datetime.now(EDT))
    suggested = ranked[0]['editor'] if ranked else next(iter(loads), '')

    pre_assigned = get_folder_assignment(notion_token, client, folder_name)

    callback_key = f"{int(datetime.now().timestamp())}_{client[:3]}_{folder_name[:8]}".replace(' ', '_')

    add_pending(callback_key, {
        'client':       client,
        'folder_name':  folder_name,
        'folder_id':    folder_id,
        'video_count':  video_count,
        'video_names':  video_names,
        'video_tree':   video_tree,
        'pre_assigned': pre_assigned,
        'chat_id':      chat_id,
    })

    # Post to Discord assignments channel so Vex can assign even when Telegram is down
    # Only fire for new folders — skip if watcher already saw this folder before
    if _is_new_folder:
        enqueue_ops_assign_request(client, folder_name, video_count, folder_id, project_num, _ops_page_id)

    msg = build_folder_notification_message(client, folder_name, video_count, suggested, loads, project_num)
    if pre_assigned:
        msg += f"\n\n🔖 <i>Folder rule suggests: {pre_assigned}</i>"

    keyboard = build_folder_keyboard(callback_key, list(loads.keys()))

    url  = f"https://api.telegram.org/bot{send_token}/sendMessage"
    resp = _send_telegram(url, {
        'chat_id':      chat_id,
        'text':         msg,
        'parse_mode':   'HTML',
        'reply_markup': keyboard.to_dict(),
    })

    # Store the sent message_id so unassigned_reminder can deep-link back to it
    if resp and resp.ok:
        tg_msg_id = resp.json().get('result', {}).get('message_id')
        if tg_msg_id:
            # Store in pending so ignore/unignore callbacks can edit the message
            all_pending = load_pending()
            if callback_key in all_pending:
                all_pending[callback_key]['message_id'] = tg_msg_id
                save_pending(all_pending)
            with PENDING_FOLDERS_LOCK:
                pending_folders = load_pending_folders()
                if folder_id not in pending_folders:
                    pending_folders[folder_id] = {}
                pending_folders[folder_id]['folder_name']         = folder_name
                pending_folders[folder_id]['telegram_message_id'] = tg_msg_id
                pending_folders[folder_id]['callback_key']        = callback_key
                save_pending_folders(pending_folders)

    # Start 24hr deadline clock from the moment the folder was detected
    # (not gated on the Telegram send — Discord is the real notification path now)
    import time as _time
    deadlines_path = os.path.join(BASE_DIR, 'deadlines.json')
    try:
        with open(deadlines_path) as _f:
            _deadlines = json.load(_f)
    except Exception:
        _deadlines = {}
    _deadlines[folder_id] = {
        'due_ts':        _time.time() + 86400,
        'indefinite':    False,
        'warned_6h':     False,
        'editor_name':   '',
        'client_name':   client,
        'folder_name':   folder_name,
        'notion_page_id': existing_page_id or '',
    }
    try:
        with open(deadlines_path, 'w') as _f:
            json.dump(_deadlines, _f, indent=2)
    except Exception as _e:
        logger.error(f'Failed to write deadline for {folder_id}: {_e}')

    logger.info(f"Sent folder notification: {client} / {folder_name} ({video_count} videos)")


# ── Folder Update Notification (called from gdrive_watcher.py) ───────────────

def send_folder_update_notification(config, folder_info, new_count, previous_count):
    """
    Called by gdrive_watcher when an existing subfolder gains more videos.
    Pings the assigned editor on Discord and sends a Telegram update to Vex.
    Deduplicates: skips sending if the same folder was already notified < 60 s ago.
    """
    send_token   = config['notion_bridge_token']
    chat_id      = config['notion_bridge_chat_id']
    notion_token = config.get('notion_token', '')

    client      = folder_info['client']
    folder_name = folder_info['folder_name']
    folder_id   = folder_info['folder_id']
    diff        = new_count - previous_count
    now_ts      = datetime.now().timestamp()

    editor_name = get_active_queue_editor(notion_token, folder_id, client, folder_name)

    # Keep the Notes "Videos: N" count in sync with the real Drive count on every
    # increase — it used to only be written once at row creation and silently went
    # stale (e.g. Jasmine/Invo 1 stuck showing "Videos: 2" while Drive had 23).
    try:
        page_id = get_active_queue_page_id_by_folder_id(notion_token, folder_id)
        if page_id:
            requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=notion_headers(notion_token),
                json={'properties': {'Notes': {'rich_text': [
                    {'text': {'content': f'Videos: {new_count} | Folder ID: {folder_id}'}}
                ]}}},
                timeout=15,
            )
    except Exception as e:
        logger.error(f'Failed to sync Notes video count for {folder_id}: {e}')

    tg_msg = (
        f"📥 <b>{client} / {folder_name}</b> updated: {previous_count} → {new_count} videos\n"
        f"Assigned to: {editor_name or 'unassigned'}"
    )
    url = f"https://api.telegram.org/bot{send_token}/sendMessage"

    if editor_name:
        # Always notify when a folder is assigned — no dedup needed
        _send_telegram(url, {'chat_id': chat_id, 'text': tg_msg, 'parse_mode': 'HTML'})
        try:
            _append_to_discord_queue({
                'type':           'update',
                'client_name':    client,
                'folder_name':    folder_name,
                'folder_id':      folder_id,
                'editor_name':    editor_name,
                'previous_count': previous_count,
                'new_count':      new_count,
                'diff':           diff,
            })
        except Exception as e:
            logger.error(f'Failed to enqueue Discord update: {e}')

    else:
        # Unassigned: only send if no notification was sent in the last 60 seconds
        with PENDING_FOLDERS_LOCK:
            pending_folders = load_pending_folders()
            last_update = pending_folders.get(folder_id, {}).get('last_update_time', 0)
            if now_ts - last_update < 60:
                logger.info(
                    f"Skipping duplicate unassigned update for {folder_name} "
                    f"(last sent {now_ts - last_update:.1f}s ago)"
                )
                return

        loads = get_editor_loads(notion_token)
        editors = list(loads.keys())
        tg_msg_with_note = tg_msg + "\n\n⚠️ Not yet assigned — tap to assign:"
        keyboard = {
            'inline_keyboard': [[
                {'text': e, 'callback_data': f'assign:{e}:{folder_id}:{client}:{new_count}'}
                for e in editors
            ]]
        }
        resp = _send_telegram(url, {
            'chat_id':      chat_id,
            'text':         tg_msg_with_note,
            'parse_mode':   'HTML',
            'reply_markup': json.dumps(keyboard),
        })
        msg_id = resp.json().get('result', {}).get('message_id') if resp and resp.ok else None
        with PENDING_FOLDERS_LOCK:
            pending_folders = load_pending_folders()
            if folder_id not in pending_folders:
                pending_folders[folder_id] = {
                    'folder_name':        folder_name,
                    'client_name':        client,
                    'video_count':        new_count,
                    'update_message_ids': [],
                }
            pending_folders[folder_id].setdefault('update_message_ids', [])
            if msg_id:
                pending_folders[folder_id]['update_message_ids'].append(msg_id)
            pending_folders[folder_id]['last_update_time'] = now_ts
            save_pending_folders(pending_folders)

    logger.info(f"Folder update: {client} / {folder_name} {previous_count}→{new_count} (editor: {editor_name})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    config  = load_config()
    token   = config['notion_bridge_token']
    chat_id = int(config['notion_bridge_chat_id'])

    app = Application.builder().token(token).build()

    app.add_handler(CallbackQueryHandler(handle_show_callback,         pattern='^show:'))
    app.add_handler(CallbackQueryHandler(handle_assignment_callback,   pattern='^assign:'))
    app.add_handler(CallbackQueryHandler(handle_ignore_callback,       pattern='^ignore:'))
    app.add_handler(CallbackQueryHandler(handle_unignore_callback,     pattern='^unignore:'))
    app.add_handler(CallbackQueryHandler(handle_review_callback,       pattern='^review:'))
    app.add_handler(CallbackQueryHandler(handle_count_choice_callback, pattern='^accept_count:'))
    app.add_handler(CallbackQueryHandler(handle_count_choice_callback, pattern='^use_drive_count:'))
    app.add_handler(CallbackQueryHandler(handle_reassign_folder_callback, pattern='^rf:'))
    app.add_handler(CallbackQueryHandler(handle_reassign_editor_callback, pattern='^re:'))
    app.add_handler(CallbackQueryHandler(handle_remove_callback,       pattern='^rm:'))
    app.add_handler(CallbackQueryHandler(handle_recover_callback,      pattern='^rc:'))
    app.add_handler(CallbackQueryHandler(handle_override_callback,     pattern='^override:'))
    app.add_handler(CommandHandler('help',            cmd_help))
    app.add_handler(CommandHandler('load',            cmd_load))
    app.add_handler(CommandHandler('pending',         cmd_pending))
    app.add_handler(CommandHandler('today',           cmd_today))
    app.add_handler(CommandHandler('editor',          cmd_editor))
    app.add_handler(CommandHandler('client',          cmd_client))
    app.add_handler(CommandHandler('reassign',        cmd_reassign))
    app.add_handler(CommandHandler('remove',          cmd_remove))
    app.add_handler(CommandHandler('recover',         cmd_recover))
    app.add_handler(CommandHandler('pending_reviews', cmd_pending_reviews))
    app.add_handler(CommandHandler('recommend',       cmd_recommend))
    app.add_handler(CommandHandler('ask',             cmd_ask))
    app.add_handler(CommandHandler('schedule',        cmd_schedule))
    app.add_handler(CommandHandler('setschedule',     cmd_setschedule))
    app.add_handler(CommandHandler('markoff',         cmd_markoff))
    app.add_handler(CommandHandler('whosout',         cmd_whosout))
    app.add_handler(CommandHandler('note',            cmd_note))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Chat(chat_id=chat_id),
        handle_text_assignment,
    ))

    logger.info("notion_bridge.py started — polling for Telegram updates")
    threading.Thread(target=ai_ops.warmup, daemon=True).start()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

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
import requests
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
PENDING_FOLDERS_FILE = os.path.join(BASE_DIR, 'pending_folders.json')
DISCORD_QUEUE_FILE    = os.path.join(BASE_DIR, 'discord_queue.json')
IGNORED_FOLDERS_FILE  = os.path.join(BASE_DIR, 'ignored_folders.json')

DISCORD_QUEUE_LOCK    = FileLock(DISCORD_QUEUE_FILE    + '.lock')
PENDING_FOLDERS_LOCK  = FileLock(PENDING_FOLDERS_FILE  + '.lock')
IGNORED_FOLDERS_LOCK  = FileLock(IGNORED_FOLDERS_FILE  + '.lock')
PENDING_REVIEWS_LOCK  = FileLock(PENDING_REVIEWS_FILE  + '.lock')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive']
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.avi'}

from logger_setup import get_logger
logger = get_logger('notion_bridge')

EDT = timezone(timedelta(hours=-4))
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt_edt):
    """Format an EDT datetime as a human-readable IST string for Telegram display."""
    return dt_edt.astimezone(IST).strftime('%d %b %Y %I:%M %p IST')


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


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
    return (f['mimeType'] != 'application/vnd.google-apps.folder'
            and os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS)


def fetch_folder_video_tree(folder_id, folder_name=None):
    """
    Returns (total_count, video_tree, flat_names) from Drive, or (None, None, None) on error.
    video_tree maps section label → [filenames]. Covers root + 2 levels of sub-subfolders.
    """
    try:
        service = get_drive_service()
        if not folder_name:
            meta = service.files().get(fileId=folder_id, fields='name',
                                       supportsAllDrives=True).execute()
            folder_name = meta.get('name', 'Folder')

        video_tree, flat_names = {}, []
        items = _list_folder(service, folder_id)

        root_videos = [f['name'] for f in items if _is_video_file(f)]
        if root_videos:
            video_tree[f'{folder_name} (root)'] = root_videos
            flat_names.extend(root_videos)

        for item in items:
            if item['mimeType'] != 'application/vnd.google-apps.folder':
                continue
            # Level 1 sub-folder
            sub_items = _list_folder(service, item['id'])
            sub_videos = [f['name'] for f in sub_items if _is_video_file(f)]
            if sub_videos:
                video_tree[item['name']] = sub_videos
                flat_names.extend(sub_videos)
            # Level 2 sub-sub-folders
            for sub_item in sub_items:
                if sub_item['mimeType'] != 'application/vnd.google-apps.folder':
                    continue
                subsub_videos = [f['name'] for f in _list_folder(service, sub_item['id']) if _is_video_file(f)]
                if subsub_videos:
                    label = f"{item['name']} / {sub_item['name']}"
                    video_tree[label] = subsub_videos
                    flat_names.extend(subsub_videos)

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
                q=f"'{parent_id}' in parents and name='Edited' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id)',
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            if resp.get('files'):
                return resp['files'][0]['id']
            current_id = parent_id
        return ''
    except Exception as e:
        logger.error(f'Drive error finding Edited folder from raw {raw_folder_id}: {e}')
        return ''


# ── Notion API ────────────────────────────────────────────────────────────────

ACTIVE_QUEUE_DB     = '44593fbf-4276-47f0-bd12-27289dcb78fd'
ASSIGNMENTS_DB      = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'
EDITOR_PROFILES_DB  = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'
DELIVERY_DATE_PROP  = 'date:Delivered Date:start'  # actual Notion property name in Delivery History DB


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28'
    }


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
            page_id = page['id']
            if name and capacity:
                loads[name] = {'active': active, 'capacity': capacity, 'page_id': page_id}
    return loads


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
    resp = requests.post(url, headers=notion_headers(token), json={})
    if resp.ok:
        for page in resp.json().get('results', []):
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
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={})
    if not resp.ok:
        return None
    for page in resp.json().get('results', []):
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


def create_active_queue_folder_row(token, folder_name, client, folder_id, video_count, editor=None, status='Raw'):
    """Creates a new folder-based row in the Active Queue Notion database."""
    url = 'https://api.notion.com/v1/pages'
    today = datetime.now(EDT).strftime('%Y-%m-%d')

    properties = {
        'Video': {'title': [{'text': {'content': folder_name}}]},
        'Creator': {'rich_text': [{'text': {'content': client}}]},
        'Status': {'select': {'name': status}},
        'Submitted': {'date': {'start': today}},
        'Notes': {'rich_text': [{'text': {'content': f'Videos: {video_count} | Folder ID: {folder_id}'}}]},
        'Drive Link': {'url': f'https://drive.google.com/drive/folders/{folder_id}'},
    }

    if editor:
        properties['Editor'] = {'select': {'name': editor}}

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


def enqueue_discord_assignment(client_name, folder_name, video_count, folder_id, editor_name, notion_page_id=None):
    """Writes an assignment to discord_queue.json for discord_bot.py to pick up."""
    try:
        _append_to_discord_queue({
            'client_name':          client_name,
            'folder_name':          folder_name,
            'video_count':          video_count,
            'folder_id':            folder_id,
            'editor_name':          editor_name,
            'notion_queue_page_id': notion_page_id,
        })
    except Exception as e:
        logger.error(f'Failed to enqueue Discord assignment: {e}')


def enqueue_creator_notify(client_name, folder_name, editor_name, video_count):
    """Writes a creator_notify item to discord_queue.json for the creator's channel."""
    try:
        _append_to_discord_queue({
            'type':        'creator_notify',
            'client_name': client_name,
            'folder_name': folder_name,
            'editor_name': editor_name,
            'video_count': video_count,
            'timestamp':   datetime.now().isoformat(),
        })
    except Exception as e:
        logger.error(f'Failed to enqueue creator notify: {e}')


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


# ── Telegram Helpers ──────────────────────────────────────────────────────────

def build_folder_notification_message(client, folder_name, video_count, suggested_editor, loads):
    load_hints = [f"{e}:{round((l['active'] / l['capacity']) * 100) if l['capacity'] > 0 else 0}%" for e, l in loads.items()]
    return (
        f"📁 <b>New Folder</b> — {client} / {folder_name}\n"
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

        notion_page_id = create_active_queue_folder_row(
            notion_token, folder_name, client, folder_id, video_count, editor,
            status='In Progress',
        )

        enqueue_discord_assignment(client, folder_name, video_count, folder_id, editor, notion_page_id)
        enqueue_creator_notify(client, folder_name, editor, video_count)
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
            folder_name = folder_data.get('folder_name', folder_id)

        await query.answer()

        notion_page_id = create_active_queue_folder_row(
            notion_token, folder_name, client_name, folder_id, video_count, editor,
            status='In Progress',
        )
        enqueue_discord_assignment(client_name, folder_name, video_count, folder_id, editor, notion_page_id)
        enqueue_creator_notify(client_name, folder_name, editor, video_count)
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

    suggested    = min(loads, key=lambda e: loads[e]['active'] / loads[e]['capacity'] if loads[e]['capacity'] else 0)
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
    enqueue_creator_notify(client, folder_name, text, video_count)
    recalculate_active_videos(notion_token, text)
    remove_pending(latest_key)

    loads = get_editor_loads(notion_token)
    load_line = ' · '.join(f"{e}:{round((loads[e]['active'] / loads[e]['capacity']) * 100) if loads[e]['capacity'] > 0 else 0}%" for e in editors if e in loads)

    await update.message.reply_text(
        f"✅ <b>{folder_name}</b> ({client}) assigned to <b>{text}</b>\n"
        f"📊 Load: {load_line}",
        parse_mode='HTML'
    )


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
    """Shows all unassigned pending folders."""
    pending = load_pending()
    active = {k: v for k, v in pending.items() if v.get('status') != 'assigned'}
    if not active:
        await update.message.reply_text("✅ No pending assignments.")
        return

    lines = [f"⏳ <b>{len(active)} unassigned folder(s)</b>\n"]
    for key, item in sorted(active.items()):
        lines.append(f"• {item['client']} / {item['folder_name']} — {item['video_count']} videos")

    await update.message.reply_text('\n'.join(lines), parse_mode='HTML')


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

    # Completion notification ONLY to notion_bridge_chat_id (spec §5)
    completion_msg = (
        f"🎬 {editor_name} completed {confirmed_count} videos\n"
        f"Client: {client_name} / {folder_name_r}\n"
        f"Delivered: {to_ist(now_edt)}"
    )
    send_token = config.get('notion_bridge_token', '')
    target_chat = config.get('notion_bridge_chat_id')
    if target_chat:
        requests.post(
            f"https://api.telegram.org/bot{send_token}/sendMessage",
            json={'chat_id': target_chat, 'text': completion_msg},
            timeout=10,
        )

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


def enqueue_discord_finalize(discord_message_id, discord_channel_id, confirmed_count):
    try:
        _append_to_discord_queue({
            'type':               'finalize',
            'discord_message_id': discord_message_id,
            'discord_channel_id': discord_channel_id,
            'confirmed_count':    confirmed_count,
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

    finalize_notion_delivery(review, confirmed_count)
    enqueue_discord_finalize(
        review['discord_message_id'],
        review['discord_channel_id'],
        confirmed_count,
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
        requests.post(url, json={
            'chat_id': chat_id,
            'text': '⚠️ Notion API unavailable — cannot assign editor right now',
        })
        return

    suggested = min(loads, key=lambda e: loads[e]['active'] / loads[e]['capacity'] if loads[e]['capacity'] else 0)

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

    msg = build_folder_notification_message(client, folder_name, video_count, suggested, loads)
    if pre_assigned:
        msg += f"\n\n🔖 <i>Folder rule suggests: {pre_assigned}</i>"

    keyboard = build_folder_keyboard(callback_key, list(loads.keys()))

    url  = f"https://api.telegram.org/bot{send_token}/sendMessage"
    resp = requests.post(url, json={
        'chat_id':      chat_id,
        'text':         msg,
        'parse_mode':   'HTML',
        'reply_markup': keyboard.to_dict(),
    })

    # Store the sent message_id so unassigned_reminder can deep-link back to it
    if resp.ok:
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

    tg_msg = (
        f"📥 <b>{client} / {folder_name}</b> updated: {previous_count} → {new_count} videos\n"
        f"Assigned to: {editor_name or 'unassigned'}"
    )
    url = f"https://api.telegram.org/bot{send_token}/sendMessage"

    if editor_name:
        # Always notify when a folder is assigned — no dedup needed
        requests.post(url, json={'chat_id': chat_id, 'text': tg_msg, 'parse_mode': 'HTML'})
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
        resp = requests.post(url, json={
            'chat_id':      chat_id,
            'text':         tg_msg_with_note,
            'parse_mode':   'HTML',
            'reply_markup': json.dumps(keyboard),
        })
        if resp.ok:
            msg_id = resp.json().get('result', {}).get('message_id')
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
    app.add_handler(CommandHandler('load',            cmd_load))
    app.add_handler(CommandHandler('pending',         cmd_pending))
    app.add_handler(CommandHandler('pending_reviews', cmd_pending_reviews))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Chat(chat_id=chat_id),
        handle_text_assignment,
    ))

    logger.info("notion_bridge.py started — polling for Telegram updates")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

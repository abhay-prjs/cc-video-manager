"""
discord_bot.py
Discord bot for CC Video Manager editing operations.
Handles editor assignment (no buttons) with Notion and Telegram integration.
IPC: notion_bridge.py writes to discord_queue.json; this bot polls it every 3 s.
"""

import asyncio
import calendar
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import unicodedata
import uuid
import requests
import discord
import ai_ops
from discord import app_commands
from discord.ext import tasks
from filelock import FileLock
from datetime import date, datetime, timedelta, timezone
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
QUEUE_FILE  = os.path.join(BASE_DIR, 'discord_queue.json')

from logger_setup import get_logger
logger = get_logger('discord_bot')

ACTIVE_QUEUE_DB         = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB      = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
CREATOR_ASSIGNMENTS_DB  = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'
DELIVERY_HISTORY_DB     = '733883073ccf48f2a83953ba2d5ad36d'
EDITOR_SCHEDULES_DB     = 'a02419d207604357a27698d559160436'
REVISION_LOG_DB         = 'a05a523e-2489-45f4-ae69-4aaf3178aca7'
DELIVERY_DATE_PROP      = 'date:Delivered Date:start'  # actual Notion property name in Delivery History DB
DAYS_OF_WEEK            = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
TOKEN_FILE           = os.path.join(BASE_DIR, 'token.json')
PENDING_REVIEWS_FILE     = os.path.join(BASE_DIR, 'pending_reviews.json')
PENDING_ASSIGNMENTS_FILE = os.path.join(BASE_DIR, 'pending_assignments.json')
VIDEO_EXTENSIONS     = {'.mp4', '.mov', '.webm', '.avi'}
DRIVE_ROOT_ID        = '1hKXUhKZZo1WN-B5h309CEiSgZbogUoum'

ASSIGNMENT_MESSAGES_FILE  = os.path.join(BASE_DIR, 'assignment_messages.json')
PENDING_OPS_ASSIGNS_FILE  = os.path.join(BASE_DIR, 'pending_ops_assigns.json')
LEADERBOARD_CHANNEL_ID    = 1499407261381038242
MONTHLY_LEADERBOARD_AUTOPOST_ENABLED = False  # paused 2026-07-31 — Vex sends monthly manually now
WEEKLY_LEADERBOARD_AUTOPOST_ENABLED  = False  # 2026-08-08 — weekly posting moved to weekly_leaderboard_post.py (cron, Sunday 15:30 UTC / 11:30 PM PHT); this Monday-00:00-UTC path would duplicate it
PROVISION_CREATE_CHANNELS_ENABLED    = False  # paused 2026-08-03 — PR #18's auto-create burst-created 59 channels on one boot; onboarding channel creation to be handled on the website instead. Linking (provision_link_pass) stays on.

with open(CONFIG_FILE) as _cfg_assignments:
    _cfg_a = json.load(_cfg_assignments)
    ASSIGNMENTS_CHANNEL_ID = int(_cfg_a.get('assignments_channel_id', 0))
    COMPLETION_CHANNEL_ID  = int(_cfg_a.get('completion_channel_id', 0))
    REVIEW_CHANNEL_ID      = int(_cfg_a.get('review_channel_id', 0)) or COMPLETION_CHANNEL_ID
    VEX_USER_ID            = _cfg_a.get('vex_discord_user_id', '')

QUEUE_LOCK               = FileLock(QUEUE_FILE               + '.lock')
PENDING_REVIEW_LOCK      = FileLock(PENDING_REVIEWS_FILE     + '.lock')
ASSIGNMENT_MESSAGES_LOCK = FileLock(ASSIGNMENT_MESSAGES_FILE + '.lock')
PENDING_OPS_ASSIGNS_LOCK = FileLock(PENDING_OPS_ASSIGNS_FILE + '.lock')

DEADLINES_FILE         = os.path.join(BASE_DIR, 'deadlines.json')
EDITOR_COUNTERS_FILE   = os.path.join(BASE_DIR, 'editor_counters.json')
PROJECT_NUMBERS_FILE   = os.path.join(BASE_DIR, 'project_numbers.json')
IGNORED_FOLDERS_FILE   = os.path.join(BASE_DIR, 'ignored_folders.json')
REMOVED_FOLDERS_FILE   = os.path.join(BASE_DIR, 'removed_folders.json')
DELIVERY_META_FILE     = os.path.join(BASE_DIR, 'delivery_meta.json')
DASHBOARD_BATCHES_FILE = os.path.join(BASE_DIR, 'dashboard_batches.json')
_DEADLINES_LOCK        = threading.Lock()
_EDITOR_COUNTERS_LOCK  = threading.Lock()
_PROJECT_NUMBERS_LOCK  = FileLock(PROJECT_NUMBERS_FILE + '.lock')
_REMOVED_FOLDERS_LOCK  = FileLock(REMOVED_FOLDERS_FILE + '.lock')
_DELIVERY_META_LOCK    = FileLock(DELIVERY_META_FILE + '.lock')
_DASHBOARD_BATCHES_LOCK = FileLock(DASHBOARD_BATCHES_FILE + '.lock')
# Test batches off a staff account, swept into the dashboard mirror before the
# site gated ticket creation on the creator being role 'student'. Dropped for
# good in dashboard_commands_loop — see the ticket_id check there.
TEST_DASHBOARD_TICKET_IDS = {
    '0a8b1d38-5f3e-4582-83c8-35965e980abd',
    '1de04c2a-ef04-4140-b1e5-ba3371023c42',
    'b2aeb913-f315-4550-bb06-90b8429dd668',
    '0116e64e-233a-4dea-880f-c33fca4f5afc',
    'e4adb2c8-23b6-4778-9e75-c1e13e5fd342',
    'b74c6e51-c4a2-48cb-a3e8-f1f525803ff0',
    '7a40a3d9-2eb1-45d9-8f58-1c74649ef626',
}

_TICKET_URL_ID_RE = re.compile(r'/tickets/([0-9a-fA-F-]{36})')


def _cmd_ticket_id(cmd):
    """The dashboard command's ticket_id, or one recovered from ticket_url.

    As of 2026-08-05, 'notify' commands aren't actually carrying a top-level
    ticket_id — every batch it mirrors lands on the editor|folder_name
    fallback key in dashboard_batches.json instead of the real one, even
    though the same id is right there in ticket_url. That's fine in
    isolation, but 'delivered'/'reopen' (per the site's own spec) key on the
    real ticket_id — so once those ship, they'd look up a batch notify never
    filed under that key and silently fail to credit it. Recovering the id
    from ticket_url here means every kind agrees on the same key regardless
    of which one the site remembers to populate."""
    tid = str(cmd.get('ticket_id') or '').strip()
    if tid:
        return tid
    m = _TICKET_URL_ID_RE.search(str(cmd.get('ticket_url') or ''))
    return m.group(1) if m else ''


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


with open(CONFIG_FILE) as _cf:
    _cfg_tmp = json.load(_cf)
    _GUILD_ID          = int(_cfg_tmp.get('discord_guild_id',   0))
    _CREATOR_GUILD_ID  = int(_cfg_tmp.get('creator_guild_id',   0))
GUILD_OBJ          = discord.Object(id=_GUILD_ID)
CREATOR_GUILD_OBJ  = discord.Object(id=_CREATOR_GUILD_ID)

EDT = timezone(timedelta(hours=-4))
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist(dt_edt):
    """Format an EDT datetime as a human-readable IST string for Telegram display."""
    return dt_edt.astimezone(IST).strftime('%d %b %Y %I:%M %p IST')


# ── Notion API ─────────────────────────────────────────────────────────────────

def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
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


def fetch_editors_from_notion():
    """Returns {name: {page_id, active, capacity, discord_channel_id, discord_user_id}}.
    Excludes editors where Capacity is None or 0, or the Active checkbox is
    unchecked (off-team editors like Danna/Karlo — keeps their rows and stats
    but stops all notifications/assignments to them)."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    editors = {}
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            name_rt    = props.get('Editor',           {}).get('title',      [])
            name       = name_rt[0].get('plain_text', '') if name_rt else ''
            active     = props.get('Active Videos',    {}).get('number') or 0
            capacity   = props.get('Capacity',          {}).get('number')
            is_active  = props.get('Active',            {}).get('checkbox', True)
            ch_rt      = props.get('Discord Channel ID',{}).get('rich_text', [])
            channel_id = ch_rt[0].get('plain_text', '') if ch_rt else ''
            uid_rt     = props.get('Discord User ID',  {}).get('rich_text', [])
            user_id    = uid_rt[0].get('plain_text', '') if uid_rt else ''
            email      = (props.get('Email', {}).get('email') or '').strip().lower()
            if name and capacity and is_active:
                editors[name] = {
                    'page_id':            page['id'],
                    'active':             active,
                    'capacity':           capacity,
                    'discord_channel_id': channel_id,
                    'discord_user_id':    user_id,
                    'email':              email,
                }
    return editors


def fetch_creator_discord_channel(client_name):
    """Returns Discord Channel ID string for client_name from Creator Assignments DB."""
    return fetch_creator_discord_info(client_name)[0]


def fetch_creator_discord_info(client_name):
    """Returns (channel_id_str, user_id_str) for client_name from Creator Assignments DB.

    Drive-origin folders name the creator by whatever the Drive folder is
    called ('Jackie'); the CC dashboard sends the creator's full profile name
    ('Jackie Zhang'). Both need to resolve to the same row, so an exact match
    is tried first (this is what Drive-origin lookups always hit), and only if
    that fails do we fall back to first-name matching — and only when exactly
    one row shares that first name, so we don't guess between two different
    people who happen to share a first name (see the documented 'Chris'
    ambiguity: renaming or matching loosely there would misroute a batch)."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{CREATOR_ASSIGNMENTS_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    if not resp.ok:
        return '', ''

    wanted = client_name.strip().lower()
    wanted_first = wanted.split()[0] if wanted else ''
    first_name_matches = []
    for page in resp.json().get('results', []):
        props = page['properties']
        name_rt = props.get('Creator/Folder', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else ''
        name_norm = name.strip().lower()
        ch_rt  = props.get('Discord Channel ID', {}).get('rich_text', [])
        uid_rt = props.get('Discord User ID',    {}).get('rich_text', [])
        ch_id  = ch_rt[0].get('plain_text', '')  if ch_rt  else ''
        u_id   = uid_rt[0].get('plain_text', '') if uid_rt else ''
        if name_norm == wanted:
            return ch_id, u_id
        if name_norm and name_norm.split()[0] == wanted_first:
            first_name_matches.append((ch_id, u_id))

    if len(first_name_matches) == 1:
        logger.info(f"fetch_creator_discord_info: no exact match for {client_name!r}, "
                    f"using unambiguous first-name match")
        return first_name_matches[0]
    if len(first_name_matches) > 1:
        logger.warning(f"fetch_creator_discord_info: {client_name!r} has {len(first_name_matches)} "
                        f"ambiguous first-name matches in Creator Assignments — not guessing")
    return '', ''


def fetch_creator_by_channel_id(channel_id):
    """Returns client_name from Creator Assignments where Discord Channel ID matches."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{CREATOR_ASSIGNMENTS_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    target = str(channel_id)
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            ch_rt = props.get('Discord Channel ID', {}).get('rich_text', [])
            ch = ch_rt[0].get('plain_text', '').strip() if ch_rt else ''
            if ch == target:
                name_rt = props.get('Creator/Folder', {}).get('title', [])
                return name_rt[0].get('plain_text', '') if name_rt else ''
    return ''

def fetch_active_queue_for_creator(client_name):
    """Returns list of Active Queue row dicts for client_name."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Creator', 'rich_text': {'equals': client_name}}, 'page_size': 100}
    logger.info(f"fetch_active_queue_for_creator: querying Active Queue with Creator=={repr(client_name)}")
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    logger.info(f"fetch_active_queue_for_creator: HTTP {resp.status_code}, results={len(resp.json().get('results', [])) if resp.ok else 'ERROR'}")
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            title_rt = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            editor_sel = props.get('Editor', {}).get('select') or {}
            editor_name = editor_sel.get('name', '')
            status_sel = props.get('Status', {}).get('select') or {}
            status = status_sel.get('name', '')
            drive_link = (props.get('Drive Link', {}).get('url') or '')
            notes_rt = props.get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            m2 = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id = m2.group(1) if m2 else ''
            delivered_date = (props.get('Delivered', {}).get('date') or {}).get('start', '')
            videos_completed = props.get('Videos Completed', {}).get('number') or 0
            rows.append({
                'folder_name':      folder_name,
                'editor_name':      editor_name,
                'status':           status,
                'video_count':      video_count,
                'folder_id':        folder_id,
                'delivered_date':   delivered_date,
                'videos_completed': videos_completed,
            })
    raw_count = sum(1 for r in rows if r['status'] == 'Raw')
    logger.info(f"fetch_active_queue_for_creator({client_name}): {len(rows)} total rows, {raw_count} Raw")
    return rows


def fetch_pending_assignments_for_creator(client_name):
    """Returns unassigned folders from pending_assignments.json for client_name."""
    try:
        if not os.path.exists(PENDING_ASSIGNMENTS_FILE):
            return []
        with open(PENDING_ASSIGNMENTS_FILE) as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"fetch_pending_assignments_for_creator: failed to read file: {e}")
        return []
    rows = []
    for key, entry in data.items():
        if entry.get('client', '').lower() != client_name.lower():
            continue
        if entry.get('status') == 'assigned':
            continue
        folder_name = entry.get('folder_name', '')
        video_count = entry.get('video_count', 0)
        folder_id   = entry.get('folder_id', '')
        rows.append({'folder_name': folder_name, 'video_count': video_count, 'folder_id': folder_id})
    logger.info(f"fetch_pending_assignments_for_creator({client_name}): {len(rows)} unassigned")
    return rows


def _notion_get(token, page_id):
    resp = requests.get(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(token),
        timeout=15,
    )
    return resp.json() if resp.ok else {}


def _notion_patch(token, page_id, properties):
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(token),
        json={'properties': properties},
        timeout=15,
    )
    if not resp.ok:
        logger.error(f"_notion_patch failed for page {page_id}: {resp.status_code} {resp.text}")
    return resp


def update_active_queue_status(token, page_id, status):
    _notion_patch(token, page_id, {'Status': {'select': {'name': status}}})


def _assign_raw_to_editor(token, folder_id, editor):
    """Update all Active Queue rows for folder_id to In Progress + editor. Returns first page_id."""
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Drive Link', 'url': {'contains': folder_id}}}
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    page_id = None
    if resp.ok:
        for page in resp.json().get('results', []):
            pid = page['id']
            _notion_patch(token, pid, {
                'Status': {'select': {'name': 'In Progress'}},
                'Editor': {'select': {'name': editor}},
            })
            if page_id is None:
                page_id = pid
    return page_id


def update_editor_active_videos(token, editor_page_id, delta):
    page    = _notion_get(token, editor_page_id)
    current = page.get('properties', {}).get('Active Videos', {}).get('number') or 0
    _notion_patch(token, editor_page_id, {'Active Videos': {'number': max(0, current + delta)}})


def recalculate_active_videos(token, editor_name):
    """Recompute Active Videos from Active Queue (In Progress + Raw) plus any
    active website-native batches, and sync Editor Profiles.

    Website batches (dashboard_batches.json) have no Drive folder and no
    Notion row, so the Active Queue query below can never see them — Active
    Videos / Status (Available/Busy/Overloaded) silently ignored that load
    entirely until this fix. /editorstats already surfaced the gap for its
    team-wide view (active_website_batches_by_editor(), added 2026-08-14
    after Jewel showed 70/70 Notion-only but was actually carrying 255
    total), but the underlying Active Videos field auto-assign ranking
    reads from was never corrected — found for real 2026-08-15 when a
    website-heavy bulk-assign left Steven showing 'Available' at 11 while
    actually sitting on ~79 videos once his active website batches counted."""
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
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    total = 0
    if resp.ok:
        for page in resp.json().get('results', []):
            notes_rt = page['properties'].get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            total += int(m.group(1)) if m else 0

    # Fetched before the tally, not after: the website rows are matched against
    # this editor's email / discord id, because the name the site uses isn't
    # always the name Notion uses.
    editors = fetch_editors_from_notion()
    total += sum(
        b.get('video_count') or 0
        for b in active_dashboard_batches_for_editor(editor_name, editors)
    )

    if editor_name not in editors:
        logger.warning(f'recalculate_active_videos: editor {editor_name} not found in profiles')
        return total
    info = editors[editor_name]
    capacity = info['capacity']
    ratio = total / capacity if capacity else 0
    status = 'Overloaded' if ratio >= 0.85 else 'Busy' if ratio >= 0.6 else 'Available'

    _notion_patch(token, info['page_id'], {
        'Active Videos': {'number': total},
        'Status': {'select': {'name': status}},
    })
    logger.info(f'recalculate_active_videos: {editor_name} -> {total} (status: {status})')
    return total


def update_editor_delivered(token, editor_page_id, count):
    page    = _notion_get(token, editor_page_id)
    current = page.get('properties', {}).get('Delivered This Week', {}).get('number') or 0
    _notion_patch(token, editor_page_id, {'Delivered This Week': {'number': current + count}})


def create_delivery_history_row(token, folder_name, client_name, editor_name,
                                 confirmed_count, today_str, edited_folder, drive_link):
    count = int(confirmed_count) if confirmed_count is not None else 0
    logger.info(f"create_delivery_history_row: folder={folder_name}, editor={editor_name}, count={count}")
    props = {
        'Folder':            {'title':     [{'text': {'content': folder_name}}]},
        'Client':            {'rich_text': [{'text': {'content': client_name}}]},
        'Editor':            {'select':    {'name': editor_name}},
        'Videos Completed':  {'number':    count},
        DELIVERY_DATE_PROP:  {'date':      {'start': today_str}},
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


def fetch_editor_by_channel_id(channel_id):
    """Returns (editor_name, stats_dict) from Editor Profiles where Discord Channel ID matches."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp   = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    target = str(channel_id)
    if resp.ok:
        for page in resp.json().get('results', []):
            props  = page['properties']
            ch_rt  = props.get('Discord Channel ID', {}).get('rich_text', [])
            ch     = ch_rt[0].get('plain_text', '').strip() if ch_rt else ''
            if ch != target:
                continue
            name_rt = props.get('Editor', {}).get('title', [])
            name    = name_rt[0].get('plain_text', '') if name_rt else ''
            if not name:
                continue
            ec = get_editor_counters(name)
            return name, {
                'active':           props.get('Active Videos',          {}).get('number') or 0,
                'capacity':         props.get('Capacity',               {}).get('number') or 70,
                'week':             props.get('Delivered This Week',    {}).get('number') or 0,
                'month':            props.get('Delivered This Month',   {}).get('number') or 0,
                'total':            props.get('Total Videos Delivered', {}).get('number') or 0,
                'avg':              props.get('Avg Turnaround Days',    {}).get('number') or 0,
                'revisions':        ec['revisions'],
                'missed_deadlines': ec['missed_deadlines'],
                'slow_pickups_4h':  ec['slow_pickups_4h'],
                'slow_pickups_12h': ec['slow_pickups_12h'],
            }
    return '', {}


def fetch_editor_by_user_id(user_id):
    """Returns editor_name from Editor Profiles where Discord User ID matches, or ''."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp   = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    target = str(user_id)
    if resp.ok:
        for page in resp.json().get('results', []):
            props  = page['properties']
            uid_rt = props.get('Discord User ID', {}).get('rich_text', [])
            uid    = uid_rt[0].get('plain_text', '').strip() if uid_rt else ''
            if uid != target:
                continue
            name_rt = props.get('Editor', {}).get('title', [])
            name    = name_rt[0].get('plain_text', '') if name_rt else ''
            if name:
                return name
    return ''


def fetch_unavailable_editors_today():
    """Returns list of editor names who have marked themselves unavailable for today."""
    config = load_config()
    token  = config['notion_token']
    today  = datetime.now(EDT).strftime('%A')
    url    = f'https://api.notion.com/v1/databases/{EDITOR_SCHEDULES_DB}/query'
    body   = {
        'filter': {
            'and': [
                {'property': 'Day',       'select':   {'equals': today}},
                {'property': 'Available', 'checkbox': {'equals': False}},
            ]
        }
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    names = []
    if resp.ok:
        for page in resp.json().get('results', []):
            sel = (page['properties'].get('Editor', {}).get('select') or {})
            name = sel.get('name', '')
            if name and name not in names:
                names.append(name)
    return names


def set_editor_available_today(editor_name, available):
    """Toggle Available checkbox on today's Editor Schedules row(s). Creates one if missing."""
    config   = load_config()
    token    = config['notion_token']
    today    = datetime.now(EDT).strftime('%A')
    headers  = notion_headers(token)

    # Find existing rows for editor + today
    url  = f'https://api.notion.com/v1/databases/{EDITOR_SCHEDULES_DB}/query'
    body = {'filter': {'and': [
        {'property': 'Editor', 'select': {'equals': editor_name}},
        {'property': 'Day',    'select': {'equals': today}},
    ]}}
    resp    = requests.post(url, headers=headers, json=body, timeout=15)
    results = resp.json().get('results', []) if resp.ok else []

    props = {'Available': {'checkbox': available}}

    if results:
        for page in results:
            r = requests.patch(
                f'https://api.notion.com/v1/pages/{page["id"]}',
                headers=headers, json={'properties': props}, timeout=15,
            )
            if not r.ok:
                logger.error(f'set_editor_available_today: PATCH failed for {editor_name}: {r.status_code} {r.text}')
    else:
        # No row yet — create one. Use 00:00-24:00 if marking available, blank if unavailable.
        create_props = {
            'Editor':    {'select':    {'name': editor_name}},
            'Day':       {'select':    {'name': today}},
            'Start EDT': {'rich_text': [{'text': {'content': '00:00' if available else ''}}]},
            'End EDT':   {'rich_text': [{'text': {'content': '24:00' if available else ''}}]},
            'Available': {'checkbox':  available},
        }
        r = requests.post(
            'https://api.notion.com/v1/pages',
            headers=headers,
            json={'parent': {'database_id': EDITOR_SCHEDULES_DB}, 'properties': create_props},
            timeout=15,
        )
        if not r.ok:
            logger.error(f'set_editor_available_today: POST failed for {editor_name}: {r.status_code} {r.text}')


def fetch_active_queue_for_editor(editor_name):
    """Returns Active Queue rows where Editor == editor_name and Status != Delivered."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': editor_name}},
                {'property': 'Status', 'select': {'does_not_equal': 'Delivered'}},
            ]
        }
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props       = page['properties']
            title_rt    = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt  = props.get('Creator', {}).get('rich_text', [])
            client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
            status_sel  = props.get('Status', {}).get('select') or {}
            status      = status_sel.get('name', '')
            notes_rt    = props.get('Notes', {}).get('rich_text', [])
            notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m           = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            drive_link  = props.get('Drive Link', {}).get('url') or ''
            m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id   = m2.group(1) if m2 else ''
            rows.append({
                'folder_name': folder_name,
                'client_name': client_name,
                'status':      status,
                'video_count': video_count,
                'folder_id':   folder_id,
            })
    return rows


def fetch_in_progress_for_editor(editor_name):
    """Returns Active Queue rows where Editor == editor_name and Status is In Progress or Revision."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': editor_name}},
                {'or': [
                    {'property': 'Status', 'select': {'equals': 'In Progress'}},
                    {'property': 'Status', 'select': {'equals': 'Revision'}},
                ]},
            ]
        }
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props       = page['properties']
            title_rt    = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt  = props.get('Creator', {}).get('rich_text', [])
            client_name = (creator_rt[0].get('plain_text', '') if creator_rt else '').strip()
            status_sel  = props.get('Status', {}).get('select') or {}
            status      = status_sel.get('name', '')
            notes_rt    = props.get('Notes', {}).get('rich_text', [])
            notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m           = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            vc_prop     = props.get('Videos Completed', {}).get('number') or 0
            if vc_prop:
                video_count = vc_prop
            drive_link  = (props.get('Drive Link', {}).get('url') or '')
            m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id   = m2.group(1) if m2 else ''
            rows.append({
                'folder_name':          folder_name.strip(),
                'client_name':          client_name,
                'video_count':          video_count,
                'folder_id':            folder_id,
                'notion_queue_page_id': page['id'],
                'is_revision':          status == 'Revision',
            })
    return rows


def fetch_delivery_history_for_editor(editor_name, limit=10):
    """Returns the last `limit` Delivery History rows for editor_name, newest first."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    body   = {
        'filter':    {'property': 'Editor', 'select': {'equals': editor_name}},
        'sorts':     [{'property': DELIVERY_DATE_PROP, 'direction': 'descending'}],
        'page_size': limit,
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props       = page['properties']
            folder_rt   = props.get('Folder', {}).get('title', [])
            folder_name = folder_rt[0].get('plain_text', '') if folder_rt else ''
            client_rt   = props.get('Client', {}).get('rich_text', [])
            client_name = client_rt[0].get('plain_text', '') if client_rt else ''
            videos      = props.get('Videos Completed', {}).get('number') or 0
            date_prop   = (props.get(DELIVERY_DATE_PROP, {}).get('date') or {}).get('start', '')
            drive_link  = props.get('Drive Link', {}).get('url') or ''
            rows.append({
                'folder_name':      folder_name,
                'client_name':      client_name,
                'videos_completed': videos,
                'delivered_date':   date_prop,
                'drive_link':       drive_link,
            })
    return rows


def fetch_delivery_history_for_creator(client_name, limit=10):
    """Returns the last `limit` Delivery History rows for client_name, newest first."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    body   = {
        'filter':    {'property': 'Client', 'rich_text': {'equals': client_name}},
        'sorts':     [{'property': DELIVERY_DATE_PROP, 'direction': 'descending'}],
        'page_size': limit,
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props       = page['properties']
            folder_rt   = props.get('Folder', {}).get('title', [])
            folder_name = folder_rt[0].get('plain_text', '') if folder_rt else ''
            editor_sel  = props.get('Editor', {}).get('select') or {}
            editor_name = editor_sel.get('name', '')
            date_prop   = (props.get(DELIVERY_DATE_PROP, {}).get('date') or {}).get('start', '')
            drive_link  = props.get('Drive Link', {}).get('url') or ''
            rows.append({
                'folder_name':    folder_name,
                'editor_name':    editor_name,
                'delivered_date': date_prop,
                'drive_link':     drive_link,
            })
    return rows


def fetch_recent_delivered_for_editor(editor_name, limit=10):
    """Active Queue rows where Editor==editor_name and Status==Delivered, newest first.
    Queries Active Queue (not Delivery History) so the result carries notion_page_id —
    Active Queue rows aren't deleted on delivery, so that page_id doubles as the stable
    key for delivery_meta.json (turnaround/overdue) and for /info's dossier lookup."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': editor_name}},
                {'property': 'Status', 'select': {'equals': 'Delivered'}},
            ]
        },
        'sorts':     [{'property': 'Delivered', 'direction': 'descending'}],
        'page_size': limit,
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props          = page['properties']
            title_rt       = props.get('Video', {}).get('title', [])
            folder_name    = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt     = props.get('Creator', {}).get('rich_text', [])
            client_name    = creator_rt[0].get('plain_text', '') if creator_rt else ''
            delivered_date = (props.get('Delivered', {}).get('date') or {}).get('start', '')
            rows.append({
                'notion_page_id': page['id'],
                'folder_name':    folder_name,
                'client_name':    client_name,
                'delivered_date': delivered_date,
            })
    return rows


def fetch_revisions_for_folder(client_name, folder_name):
    """Returns Revision Log rows (notes + date) for this client/folder, newest first."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{REVISION_LOG_DB}/query'
    body   = {
        'filter': {
            'and': [
                {'property': 'Creator',     'rich_text': {'equals': client_name}},
                {'property': 'Folder Name', 'title':     {'equals': folder_name}},
            ]
        },
        'sorts':     [{'property': 'Date', 'direction': 'descending'}],
        'page_size': 20,
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props    = page['properties']
            notes_rt = props.get('Revision Notes', {}).get('rich_text', [])
            notes    = notes_rt[0].get('plain_text', '') if notes_rt else ''
            date     = (props.get('Date', {}).get('date') or {}).get('start', '')
            rows.append({'notes': notes, 'date': date})
    return rows


def resolve_drive_ids_for_dossier(client_name, raw_folder_id, edited_folder_name):
    """Blocking Drive lookup for /info's dossier — call via run_in_executor.
    Returns (client_root_id, edited_subfolder_id), either may be ''."""
    client_root_id      = _client_root_folder_cache.get(client_name, '')
    edited_subfolder_id = ''
    try:
        service = get_drive_service()
        if not client_root_id and client_name:
            client_root_id, _ = _find_edited_folder_top_down(service, client_name)
            client_root_id = client_root_id or ''
        if edited_folder_name and client_name:
            _, _, _, edited_subfolder_id = find_edited_folder_videos(
                raw_folder_id, edited_folder_name, client_name
            )
            edited_subfolder_id = edited_subfolder_id or ''
    except Exception as e:
        logger.error(f'resolve_drive_ids_for_dossier error for {client_name}: {e}')
    return client_root_id, edited_subfolder_id


def fetch_editor_loads_list():
    """Returns sorted list of {name, active, capacity} from Editor Profiles."""
    editors = fetch_editors_from_notion()
    return sorted(
        [{'name': n, 'active': d['active'], 'capacity': d['capacity']} for n, d in editors.items()],
        key=lambda x: x['name'],
    )


def fetch_all_editor_stats():
    """Returns list of active editors with week/month stats, sorted by Delivered This Week desc.
    Reads directly from Editor Profiles to include manual entries and all completions.
    Excludes editors where Capacity is None or 0 (treated as inactive)."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp   = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    editors = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props    = page['properties']
            name_rt  = props.get('Editor', {}).get('title', [])
            name     = name_rt[0].get('plain_text', '') if name_rt else ''
            capacity = props.get('Capacity', {}).get('number')
            if not name or not capacity:
                continue
            week  = props.get('Delivered This Week',  {}).get('number') or 0
            month = props.get('Delivered This Month', {}).get('number') or 0
            ec    = get_editor_counters(name)
            editors.append({
                'name': name, 'week': week, 'month': month, 'capacity': capacity,
                'revisions': ec['revisions'], 'missed_deadlines': ec['missed_deadlines'],
                'slow_pickups_4h': ec['slow_pickups_4h'], 'slow_pickups_12h': ec['slow_pickups_12h'],
            })
    return sorted(editors, key=lambda x: x['week'], reverse=True)


def fetch_all_editor_stats_for_range(start_str, end_str):
    """Returns list of active editors with 'week' = total videos delivered in
    Delivery History within [start_str, end_str) (Notion date strings), sorted desc.
    This is the single source of truth for weekly numbers (leaderboard command +
    auto-post) so bonus-relevant figures always come from a live query instead of
    the cached 'Delivered This Week' counter, which can drift (manual corrections,
    missed resets, etc).
    'week' is the max of that live query and the cached counter — website-native
    deliveries (handle_cc_dashboard_delivered) PATCH the cached counter directly
    and never create a Delivery History row, so the live query alone would
    undercount those; the cached counter alone can't be trusted to reflect only
    the requested range. Taking the max is safe here because start_str/end_str
    is always the still-open current week or a week already fully accounted for
    in Delivery History — never a stale past week the counter has since moved past."""
    config = load_config()
    token  = config['notion_token']

    # Editor list + capacity + month + cached week, from Editor Profiles
    url  = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    editors = {}
    if resp.ok:
        for page in resp.json().get('results', []):
            props    = page['properties']
            name_rt  = props.get('Editor', {}).get('title', [])
            name     = name_rt[0].get('plain_text', '') if name_rt else ''
            capacity = props.get('Capacity', {}).get('number')
            if not name or not capacity:
                continue
            month       = props.get('Delivered This Month', {}).get('number') or 0
            week_cached = props.get('Delivered This Week',  {}).get('number') or 0
            ec          = get_editor_counters(name)
            editors[name] = {
                'name': name, 'week': 0, 'week_cached': week_cached, 'month': month, 'capacity': capacity,
                'revisions': ec['revisions'], 'missed_deadlines': ec['missed_deadlines'],
            }

    # Videos delivered per editor in [start_str, end_str), from Delivery History.
    # Must paginate (notion_query_all) — a single un-paginated query silently caps at
    # 100 rows, which a week with 11+ active editors blows past well before Sunday,
    # truncating whichever rows sort last and undercounting per editor unpredictably.
    dh_results = notion_query_all(token, DELIVERY_HISTORY_DB, _delivery_history_week_filter(start_str, end_str))
    for row in _parse_delivery_history_rows(dh_results):
        name = row['editor_name']
        if name in editors:
            editors[name]['week'] += row['videos_completed']

    for e in editors.values():
        e['week'] = max(e['week'], e.pop('week_cached'))

    return sorted(editors.values(), key=lambda x: x['week'], reverse=True)


def build_weekly_leaderboard_embed(editors, title=None):
    """Builds a Discord embed for the weekly leaderboard."""
    medals = ['🥇', '🥈', '🥉']
    lines  = []
    for i, e in enumerate(editors):
        medal = medals[i] if i < 3 else ''
        prefix = f"{i + 1}. {medal}" if medal else f"{i + 1}."
        lines.append(f"{prefix} {e['name']} — {e['week']} videos")

    embed_title = title or '🏆 Weekly Leaderboard'
    embed = discord.Embed(
        title=embed_title,
        description='\n'.join(lines) if lines else 'No data yet.',
        color=discord.Color.gold(),
    )
    if title is None:
        today       = datetime.now(EDT).date()
        week_start  = today - timedelta(days=(today.weekday() + 1) % 7)  # most recent Sunday
        week_end    = week_start + timedelta(days=6)                     # Saturday
        week_range  = f"Week of {week_start.strftime('%b %-d')} — {week_end.strftime('%b %-d')}"
        embed.set_footer(text=week_range)
    return embed


def build_monthly_leaderboard_embed(editors, year, month):
    """Builds a Discord embed for the monthly leaderboard."""
    medals = ['🥇', '🥈', '🥉']
    lines  = []
    for i, e in enumerate(editors):
        medal  = medals[i] if i < 3 else ''
        prefix = f"{i + 1}. {medal}" if medal else f"{i + 1}."
        pct    = round((e['month'] / e['capacity']) * 100) if e['capacity'] > 0 else 0
        lines.append(f"{prefix} {e['name']} — {e['month']} videos ({pct}% capacity)")

    month_name = datetime(year, month, 1).strftime('%B')
    embed = discord.Embed(
        title=f'🏆 Monthly Leaderboard — {month_name} {year}',
        description='\n'.join(lines) if lines else 'No data yet.',
        color=discord.Color.purple(),
    )
    return embed


def fetch_active_queue_non_delivered():
    """Returns Active Queue rows where Status != Delivered, excluding ignored folders."""
    config  = load_config()
    token   = config['notion_token']
    ignored = _load_ignored_folder_ids()
    body    = {'filter': {'property': 'Status', 'select': {'does_not_equal': 'Delivered'}}}
    pages   = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows    = []
    for page in pages:
        props       = page['properties']
        title_rt    = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt  = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
        status_sel  = props.get('Status', {}).get('select') or {}
        status      = status_sel.get('name', '')
        notes_rt    = props.get('Notes', {}).get('rich_text', [])
        notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m           = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0
        drive_link  = props.get('Drive Link', {}).get('url') or ''
        m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        folder_id   = m2.group(1) if m2 else ''
        if folder_id and folder_id in ignored:
            continue
        rows.append({
            'client_name': client_name,
            'folder_name': folder_name,
            'video_count': video_count,
            'status':      status,
            'folder_id':   folder_id,
        })
    raw_count = sum(1 for r in rows if r['status'] == 'Raw')
    logger.info(f"fetch_active_queue_non_delivered: {len(rows)} total rows, {raw_count} Raw (unassigned)")
    return rows


def fetch_active_queue_in_progress():
    """Returns Active Queue rows where Status is In Progress, oldest first."""
    config = load_config()
    token  = config['notion_token']
    body   = {
        'filter': {'property': 'Status', 'select': {'equals': 'In Progress'}},
        'sorts':  [{'property': 'Submitted', 'direction': 'ascending'}],
    }
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows  = []
    for page in pages:
        props       = page['properties']
        title_rt    = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt  = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
        editor_sel  = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '')
        notes_rt    = props.get('Notes', {}).get('rich_text', [])
        notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m           = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0
        submitted   = (props.get('Submitted', {}).get('date') or {}).get('start', '')
        drive_link  = props.get('Drive Link', {}).get('url') or ''
        m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        folder_id   = m2.group(1) if m2 else ''
        rows.append({
            'folder_name':    folder_name,
            'client_name':    client_name,
            'editor_name':    editor_name,
            'video_count':    video_count,
            'submitted_date': submitted,
            'folder_id':      folder_id,
            'notion_page_id': page['id'],
        })

    # Backfill deadlines for folders assigned before deadline tracking was added
    if rows:
        deadlines = load_deadlines()
        changed   = False
        for r in rows:
            fid = r.get('folder_id')
            if not fid or fid in deadlines:
                continue
            # Derive deadline from submitted_date + 24h
            submitted_str = r.get('submitted_date', '')
            if submitted_str:
                try:
                    dt = datetime.fromisoformat(submitted_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    due_ts = dt.timestamp() + 86400
                except Exception:
                    due_ts = time.time() + 86400
            else:
                due_ts = time.time() + 86400
            deadlines[fid] = {
                'due_ts':       due_ts,
                'indefinite':   False,
                'warned_6h':    due_ts - time.time() <= 6 * 3600,  # don't re-warn old folders
                'editor_name':  r['editor_name'],
                'client_name':  r['client_name'],
                'folder_name':  r['folder_name'],
                'notion_page_id': r['notion_page_id'],
            }
            changed = True
        if changed:
            save_deadlines(deadlines)

    return rows


def fetch_removable_folders(editor_name=None):
    """Returns Active Queue rows where Status is Raw (Pending), In Progress (Active), or Revision.

    If editor_name is given, only rows assigned to that editor are returned
    (Raw/unassigned rows have no Editor and are excluded in that case).
    """
    config = load_config()
    token  = config['notion_token']
    status_filter = {'or': [
        {'property': 'Status', 'select': {'equals': 'Raw'}},
        {'property': 'Status', 'select': {'equals': 'In Progress'}},
        {'property': 'Status', 'select': {'equals': 'Revision'}},
    ]}
    if editor_name:
        body = {'filter': {'and': [
            status_filter,
            {'property': 'Editor', 'select': {'equals': editor_name}},
        ]}}
    else:
        body = {'filter': status_filter}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows  = []
    for page in pages:
        props        = page['properties']
        title_rt     = props.get('Video', {}).get('title', [])
        folder_name  = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt   = props.get('Creator', {}).get('rich_text', [])
        client_name  = creator_rt[0].get('plain_text', '') if creator_rt else ''
        editor_sel   = props.get('Editor', {}).get('select') or {}
        row_editor   = editor_sel.get('name', '')
        status_sel   = props.get('Status', {}).get('select') or {}
        status       = status_sel.get('name', '')
        notes_rt     = props.get('Notes', {}).get('rich_text', [])
        notes        = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m            = re.search(r'Videos:\s*(\d+)', notes)
        video_count  = int(m.group(1)) if m else 0
        # For Revision rows, Videos Completed is the authoritative delivered count
        videos_completed = props.get('Videos Completed', {}).get('number') or video_count
        drive_link   = props.get('Drive Link', {}).get('url') or ''
        m2           = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        folder_id    = m2.group(1) if m2 else ''
        if status == 'Raw':
            display_status = 'Pending'
        elif status == 'Revision':
            display_status = 'Revision'
        else:
            display_status = 'Active'
        rows.append({
            'notion_page_id':   page['id'],
            'folder_id':        folder_id,
            'folder_name':      folder_name,
            'client_name':      client_name,
            'editor_name':      row_editor,
            'video_count':      video_count,
            'videos_completed': videos_completed,
            'status':           display_status,
        })
    return rows


def fetch_delivered_folders_for_creator(client_name):
    """Returns Active Queue rows with Status=Delivered for client_name (for /revision picker)."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {
            'and': [
                {'property': 'Creator', 'rich_text': {'equals': client_name}},
                {'property': 'Status',  'select':    {'equals': 'Delivered'}},
            ]
        },
        'sorts': [{'property': 'Submitted', 'direction': 'descending'}],
        'page_size': 25,
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            title_rt = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            editor_sel = props.get('Editor', {}).get('select') or {}
            editor_name = editor_sel.get('name', '')
            notes_rt = props.get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            vc_prop = props.get('Videos Completed', {}).get('number') or 0
            if vc_prop:
                video_count = vc_prop
            drive_link = props.get('Drive Link', {}).get('url') or ''
            m2 = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id = m2.group(1) if m2 else ''
            rows.append({
                'folder_name':      folder_name,
                'editor_name':      editor_name,
                'video_count':      video_count,
                'folder_id':        folder_id,
                'notion_page_id':   page['id'],
            })
    return rows


def fetch_revision_folders_for_editor(editor_name):
    """Returns Active Queue rows with Status=Revision assigned to editor_name."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select':     {'equals': editor_name}},
                {'property': 'Status', 'select':     {'equals': 'Revision'}},
            ]
        },
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            title_rt = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt = props.get('Creator', {}).get('rich_text', [])
            client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
            notes_rt = props.get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            vc_prop = props.get('Videos Completed', {}).get('number') or 0
            if vc_prop:
                video_count = vc_prop
            drive_link = props.get('Drive Link', {}).get('url') or ''
            m2         = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id  = m2.group(1) if m2 else ''
            rows.append({
                'folder_name': folder_name,
                'client_name': client_name,
                'video_count': video_count,
                'notion_page_id': page['id'],
                'folder_id':   folder_id,
            })
    return rows


def fetch_revision_folders_for_creator(client_name):
    """Returns Active Queue rows with Status=Revision for client_name."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {
            'and': [
                {'property': 'Creator', 'rich_text': {'equals': client_name}},
                {'property': 'Status',  'select':    {'equals': 'Revision'}},
            ]
        },
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            title_rt = props.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            editor_sel = props.get('Editor', {}).get('select') or {}
            editor_name = editor_sel.get('name', '')
            drive_link = props.get('Drive Link', {}).get('url') or ''
            m2         = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id  = m2.group(1) if m2 else ''
            rows.append({'folder_name': folder_name, 'editor_name': editor_name, 'folder_id': folder_id})
    return rows


def fetch_all_revision_folders():
    """Returns all Active Queue rows currently in Revision status."""
    config = load_config()
    token = config['notion_token']
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Revision'}}}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows = []
    for page in pages:
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
        editor_sel = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '')
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0
        vc_prop = props.get('Videos Completed', {}).get('number') or 0
        if vc_prop:
            video_count = vc_prop
        drive_link = props.get('Drive Link', {}).get('url') or ''
        m2 = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        folder_id = m2.group(1) if m2 else ''
        rows.append({
            'folder_name':    folder_name,
            'client_name':    client_name,
            'editor_name':    editor_name,
            'video_count':    video_count,
            'folder_id':      folder_id,
            'notion_page_id': page['id'],
        })
    return rows


def _delivery_history_date_filter(today_str, tomorrow_str, editor_name=None):
    """Build a Notion filter body for Delivery History scoped to [today, tomorrow)."""
    date_clauses = [
        {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after': today_str}},
        {'property': DELIVERY_DATE_PROP, 'date': {'before': tomorrow_str}},
    ]
    if editor_name:
        date_clauses.insert(0, {'property': 'Editor', 'select': {'equals': editor_name}})
    return {'filter': {'and': date_clauses}, 'sorts': [{'property': DELIVERY_DATE_PROP, 'direction': 'descending'}]}


def _delivery_history_week_filter(monday_str, tomorrow_str, editor_name=None):
    """Build a Notion filter body for Delivery History scoped to [monday, tomorrow)."""
    date_clauses = [
        {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after': monday_str}},
        {'property': DELIVERY_DATE_PROP, 'date': {'before': tomorrow_str}},
    ]
    if editor_name:
        date_clauses.insert(0, {'property': 'Editor', 'select': {'equals': editor_name}})
    return {'filter': {'and': date_clauses}, 'sorts': [{'property': DELIVERY_DATE_PROP, 'direction': 'descending'}]}


def _parse_delivery_history_rows(results, include_editor=True, include_drive=False):
    """Extract row dicts from Delivery History Notion results."""
    rows = []
    for page in results:
        props       = page['properties']
        folder_rt   = props.get('Folder', {}).get('title', [])
        folder_name = folder_rt[0].get('plain_text', '') if folder_rt else ''
        client_rt   = props.get('Client', {}).get('rich_text', [])
        client_name = client_rt[0].get('plain_text', '') if client_rt else ''
        videos      = props.get('Videos Completed', {}).get('number') or 0
        row = {'folder_name': folder_name, 'client_name': client_name, 'videos_completed': videos}
        if include_editor:
            editor_sel       = props.get('Editor', {}).get('select') or {}
            row['editor_name'] = editor_sel.get('name', '')
        if include_drive:
            row['drive_link'] = props.get('Drive Link', {}).get('url') or ''
        rows.append(row)
    return rows


def fetch_delivered_today():
    """Returns Delivery History rows where Delivered Date == today (EDT)."""
    config    = load_config()
    token     = config['notion_token']
    now_edt      = datetime.now(EDT)
    today_str    = now_edt.strftime('%Y-%m-%d')
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    logger.info(f"fetch_delivered_today: querying Delivery History for date={today_str} (EDT)")
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    resp = requests.post(url, headers=notion_headers(token),
                         json=_delivery_history_date_filter(today_str, tomorrow_str), timeout=15)
    rows = _parse_delivery_history_rows(resp.json().get('results', []), include_drive=True) if resp.ok else []
    total_videos   = sum(r['videos_completed'] for r in rows)
    unique_folders = len(set(r['folder_name'] for r in rows))
    logger.info(
        f"fetch_delivered_today: date={today_str} → {len(rows)} rows, "
        f"{unique_folders} unique folders, {total_videos} total videos"
    )
    return rows


def fetch_delivered_today_for_editor(editor_name):
    """Returns Delivery History rows where Editor == editor_name AND Delivered Date == today (EDT)."""
    config    = load_config()
    token     = config['notion_token']
    now_edt      = datetime.now(EDT)
    today_str    = now_edt.strftime('%Y-%m-%d')
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    logger.info(f"fetch_delivered_today_for_editor: editor={editor_name}, date={today_str} (EDT)")
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    resp = requests.post(url, headers=notion_headers(token),
                         json=_delivery_history_date_filter(today_str, tomorrow_str, editor_name), timeout=15)
    rows = _parse_delivery_history_rows(resp.json().get('results', []), include_editor=False) if resp.ok else []
    total = sum(r['videos_completed'] for r in rows)
    logger.info(
        f"fetch_delivered_today_for_editor: {editor_name} → {len(rows)} folders, "
        f"{total} videos today ({today_str})"
    )
    return rows


def fetch_delivered_this_week_for_editor(editor_name):
    """Returns Delivery History rows where Editor == editor_name AND Delivered Date >= Sunday (EDT)."""
    config    = load_config()
    token     = config['notion_token']
    now_edt      = datetime.now(EDT)
    today_str    = now_edt.strftime('%Y-%m-%d')
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    week_start_str = (now_edt - timedelta(days=(now_edt.weekday() + 1) % 7)).strftime('%Y-%m-%d')  # most recent Sunday
    logger.info(f"fetch_delivered_this_week_for_editor: editor={editor_name}, week={week_start_str}..{today_str} (EDT)")
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    resp = requests.post(url, headers=notion_headers(token),
                         json=_delivery_history_week_filter(week_start_str, tomorrow_str, editor_name), timeout=15)
    rows = _parse_delivery_history_rows(resp.json().get('results', []), include_editor=False) if resp.ok else []
    total = sum(r['videos_completed'] for r in rows)
    logger.info(
        f"fetch_delivered_this_week_for_editor: {editor_name} → {len(rows)} folders, "
        f"{total} videos this week (since {week_start_str})"
    )
    return rows


def fetch_delivered_this_month_for_editor(editor_name):
    """Returns Delivery History rows where Editor == editor_name AND Delivered Date >= the 1st (EDT).

    Paginated: a busy editor's month is well past Notion's 100-row page (jill
    2026-08-22 — 1,251 on the counter vs 1,357 in her own log, "total monthly
    vid miscount"). The Editor Profiles counter only moves when a delivery goes
    through the bot, so anything finalized by hand, during an outage, or via
    /fixcounter drift leaves it low; the rows are the record.
    """
    config    = load_config()
    token     = config['notion_token']
    now_edt      = datetime.now(EDT)
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    month_start_str = now_edt.replace(day=1).strftime('%Y-%m-%d')
    logger.info(f"fetch_delivered_this_month_for_editor: editor={editor_name}, month={month_start_str}..{tomorrow_str} (EDT)")
    results = notion_query_all(
        token, DELIVERY_HISTORY_DB,
        _delivery_history_week_filter(month_start_str, tomorrow_str, editor_name),
    )
    rows  = _parse_delivery_history_rows(results, include_editor=False)
    total = sum(r['videos_completed'] for r in rows)
    logger.info(
        f"fetch_delivered_this_month_for_editor: {editor_name} → {len(rows)} folders, "
        f"{total} videos this month (since {month_start_str})"
    )
    return rows


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(message):
    config = load_config()
    url = f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage"
    try:
        requests.post(url, json={'chat_id': config['chat_id'], 'text': message}, timeout=10)
    except Exception as e:
        logger.error(f'Telegram error: {e}')


def send_telegram_html(message):
    config = load_config()
    url = f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage"
    try:
        requests.post(url, json={'chat_id': config['chat_id'], 'text': message, 'parse_mode': 'HTML'}, timeout=10)
    except Exception as e:
        logger.error(f'Telegram error: {e}')


# ── Discord ops channel (assignment / completion notifications for Vex) ────────

# Retry budget for an ops post. Deliberately small: this is a blocking call made
# from inside the event loop, so the ceiling on total sleep matters more than
# squeezing out the last retry.
OPS_POST_ATTEMPTS = 3
OPS_POST_BACKOFF  = 0.5
OPS_POST_MAX_WAIT = 2.0


def send_discord_ops_channel(message=None, embed=None):
    """Post to the ops channel. Returns True only when Discord took it.

    Almost every caller ignores that — this is a mirror for Vex and one lost
    line doesn't matter. It matters for `_dashboard_message_failed`, where the
    ops post IS the last copy of a message that reached nobody, so it can't be
    assumed sent. The status code was never checked before either: a 403 on
    the ops channel read exactly like a success."""
    config = load_config()
    channel_id = config.get('ops_channel_id')
    token = config.get('discord_bot_token')
    if not channel_id or not token:
        logger.error('ops_channel_id or discord_bot_token missing in config')
        return False
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages'
    payload = {}
    if message:
        payload['content'] = message
    if embed:
        payload['embeds'] = [embed]
    headers = {'Authorization': f'Bot {token}', 'Content-Type': 'application/json'}
    for attempt in range(OPS_POST_ATTEMPTS):
        last = attempt == OPS_POST_ATTEMPTS - 1
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
        except Exception as e:
            logger.error(f'Discord ops channel error: {e}')
            if last:
                return False
            time.sleep(OPS_POST_BACKOFF)
            continue

        if res.status_code < 300:
            return True

        # 429 carries the exact wait in the body. Not honouring it was why a
        # boot burst (the provision report + escalations landing together) lost
        # an ops post to a 0.3s rate limit (2026-08-20). Bounded on purpose:
        # this runs inside the event loop, and blocking it for seconds is how
        # slash commands start failing with "Unknown interaction".
        if res.status_code == 429 and not last:
            try:
                wait = float(res.json().get('retry_after', OPS_POST_BACKOFF))
            except Exception:
                wait = OPS_POST_BACKOFF
            if wait <= OPS_POST_MAX_WAIT:
                logger.info(f'Discord ops channel rate limited — retrying in {wait}s')
                time.sleep(wait)
                continue
            logger.error(
                f'Discord ops channel rate limited for {wait}s — too long to hold '
                f'the loop, giving up on this post')
            return False

        # 5xx is Discord having a moment; everything else (403, 404, a bad
        # payload) will fail again just as hard, so it stays terminal.
        if 500 <= res.status_code < 600 and not last:
            logger.warning(
                f'Discord ops channel {res.status_code} — retrying in {OPS_POST_BACKOFF}s')
            time.sleep(OPS_POST_BACKOFF)
            continue

        logger.error(
            f'Discord ops channel rejected the post ({res.status_code}): '
            f'{res.text[:200]}')
        return False
    return False


# ── Creator Collective dashboard bridge ────────────────────────────────────────

PENDING_DASHBOARD_PUSHES_FILE = os.path.join(BASE_DIR, 'pending_dashboard_pushes.json')
_PENDING_DASHBOARD_PUSHES_LOCK = FileLock(PENDING_DASHBOARD_PUSHES_FILE + '.lock')


def _load_pending_dashboard_pushes():
    try:
        with open(PENDING_DASHBOARD_PUSHES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _push_identity(kind, payload):
    """What makes two parked pushes 'the same push', per kind.

    Drive-keyed pushes collapse on the folder — a later state supersedes an
    earlier one for the same folder. Offer responses do NOT: they carry no
    folder_id, so keying them on one would make every parked answer look
    identical and quietly drop all but the last. Two editors passing on two
    different batches are two separate facts."""
    p = payload or {}
    if kind == 'offer':
        return p.get('offer_id') or p.get('ticket_id')
    return p.get('folder_id')


def _queue_dashboard_push(kind, payload):
    """Park a push the dashboard couldn't take, so it isn't lost to one blip.
    One entry per (kind, identity) — a later state supersedes an earlier one."""
    with _PENDING_DASHBOARD_PUSHES_LOCK:
        ident = _push_identity(kind, payload)
        items = [
            i for i in _load_pending_dashboard_pushes()
            if not (i.get('kind') == kind
                    and _push_identity(kind, i.get('payload', {})) == ident)
        ]
        items.append({'kind': kind, 'payload': payload})
        with open(PENDING_DASHBOARD_PUSHES_FILE, 'w') as f:
            json.dump(items[-50:], f, indent=2)


def _dashboard_post(kind, payload):
    """One POST attempt at the dashboard. Returns True when it's settled (sent,
    or rejected in a way retrying can't fix), False when it should be retried."""
    settled, _body = _dashboard_post_result(kind, payload)
    return settled


# Which config key holds the endpoint for each outbound kind. A missing key
# makes that push inert rather than misrouted — see the `not url` guard below.
_DASHBOARD_POST_URL_KEYS = {
    'assign': 'dashboard_url',
    'status': 'dashboard_status_url',
    'offer':  'dashboard_offer_url',
}


def _dashboard_post_result(kind, payload):
    """Same POST, but hands back what the site said: (settled, body).

    The body matters for the assignments picker. The site answers a push it
    won't act on with 200 + {"skipped": "..."} — an archived batch, or one
    already delivered — and the picker used to report "✅ Assigned" regardless,
    so Discord claimed an assignment the dashboard had refused."""
    config = load_config()
    secret = config.get('dashboard_secret')
    url = config.get(_DASHBOARD_POST_URL_KEYS.get(kind, 'dashboard_status_url'))
    if not url or not secret:
        return True, None  # bridge off — nothing to retry
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=10,
        )
    except Exception as e:
        logger.warning(f'Dashboard bridge error ({kind}): {e}')
        return False, None

    if resp.status_code == 200:
        try:
            body = resp.json()
        except Exception:
            body = None
        return True, (body if isinstance(body, dict) else None)
    # 400 (bad/missing field — our bug), 422 (no matching student profile), and
    # 404 (assignment/ticket never mirrored) are data problems — retrying
    # forever won't fix them, a human has to.
    if resp.status_code in (400, 404, 422):
        logger.error(f'Dashboard bridge rejected {kind}: {resp.status_code} {resp.text[:200]}')
        return True, None
    if resp.status_code == 401:
        logger.error(
            f'Dashboard bridge 401 ({kind}): dashboard_secret does not match the '
            f"site's EDITING_BRIDGE_SECRET — every push will keep failing until "
            f'this is fixed, not just this one. {resp.text[:200]}'
        )
        return False, None
    logger.warning(f'Dashboard bridge {resp.status_code} ({kind}): {resp.text[:300]}')
    return False, None


def flush_dashboard_pushes():
    """Retry everything an earlier failure parked. Called from the poll loop."""
    with _PENDING_DASHBOARD_PUSHES_LOCK:
        items = _load_pending_dashboard_pushes()
        if not items:
            return
        still = [i for i in items if not _dashboard_post(i.get('kind', 'assign'), i.get('payload', {}))]
        with open(PENDING_DASHBOARD_PUSHES_FILE, 'w') as f:
            json.dump(still, f, indent=2)
    if still:
        logger.info(f'{len(still)} dashboard push(es) still pending')


def post_dashboard_assignment(payload):
    """Best-effort mirror of an assignment into the Creator Collective
    dashboard (creates/updates an editing ticket there). Unconfigured or
    unreachable dashboards must never block the Discord flow — but a failure is
    now parked and retried instead of dropped."""
    settled, body = _dashboard_post_result('assign', payload)
    if not settled:
        _queue_dashboard_push('assign', payload)
    return body or {}


def post_dashboard_offer_response(payload):
    """Send an editor's accept/pass on an assignment offer back to the site.

    Parked and retried like the others, because the answer is the only record
    that the editor replied at all — dropping it leaves the offer looking
    unanswered and the site expires it out from under them."""
    settled, body = _dashboard_post_result('offer', payload)
    if not settled:
        _queue_dashboard_push('offer', payload)
    return body or {}


def post_dashboard_status(folder_id, status, video_count=None, edited_folder_link='',
                          editor_name='', note=''):
    """Tell the dashboard a batch moved: delivered / revisions.
    Without this the dashboard ticket sits at `assigned` forever after Vex
    approves an editor's /complete.

    The endpoint also accepts `approved`, but that status means the *student*
    accepted the cuts on their end — it's terminal there and closes the ticket,
    removing their revision path. That approval only ever flows site -> bot
    (see handle_cc_dashboard_approve), never bot -> site: nothing here
    represents the creator's own sign-off, so `approved` must never be sent
    from this function. Do not add it just because a batch got a VA/Team
    sign-off internally — that's still `delivered` from the dashboard's
    point of view."""
    if not folder_id:
        return
    payload = {
        'folder_id':          folder_id,
        'status':             status,
        'edited_folder_link': edited_folder_link or '',
        'editor_name':        editor_name or '',
        'note':               note or '',
    }
    if video_count is not None:
        payload['video_count'] = video_count
    if not _dashboard_post('status', payload):
        _queue_dashboard_push('status', payload)


# The dashboard's own assignments come back to us through the command feed. If
# we then pushed them straight back out, the dashboard would log a redundant
# reassign on every one. The flag rides on the queue item so it survives a
# restart mid-queue.
def _norm_editor_name(s):
    """Leading/trailing and doubled-up spaces are the usual drift between a
    dashboard profile name and the Notion editor name."""
    return ' '.join((s or '').split()).casefold()


def resolve_editor_name(name, editors):
    """Map a dashboard editor name onto a Notion editor key. Exact wins, then
    whitespace/case, then punctuation-insensitive — each only when it lands on
    exactly one editor, so two similar names never silently pick one."""
    raw = (name or '').strip()
    if not raw:
        return None
    if raw in editors:
        return raw
    target = _norm_editor_name(raw)
    hits = [k for k in editors if _norm_editor_name(k) == target]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return None
    squash = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').casefold())
    t = squash(raw)
    hits = [k for k in editors if squash(k) == t]
    return hits[0] if len(hits) == 1 else None


def resolve_editor_key(cmd, editors):
    """Map a dashboard command onto a Notion editor key. The Discord user id
    wins outright when the dashboard sends one — names genuinely collide, ids
    don't — and the name matcher is only the fallback for editors whose Notion
    row has no Discord User ID filled in yet."""
    uid = str(cmd.get('editor_discord_id') or '').strip()
    if uid:
        hits = [k for k, v in editors.items()
                if str(v.get('discord_user_id') or '').strip() == uid]
        if len(hits) == 1:
            return hits[0]
    return resolve_editor_name(cmd.get('editor_name'), editors)


def fetch_dashboard_commands():
    """GET pending assignment commands made in the CC dashboard UI.
    Returns (url, commands). Unconfigured / unreachable → (None, [])."""
    config = load_config()
    url    = config.get('dashboard_commands_url')
    secret = config.get('dashboard_secret')
    if not url or not secret:
        return None, []
    try:
        resp = requests.get(url, headers={'Authorization': f'Bearer {secret}'}, timeout=10)
        if resp.status_code != 200:
            logger.warning(f'Dashboard commands {resp.status_code}: {resp.text[:200]}')
            return None, []
        return url, resp.json().get('commands', [])
    except Exception as e:
        logger.warning(f'Dashboard commands error: {e}')
        return None, []


def ack_dashboard_commands(url, ids):
    config = load_config()
    secret = config.get('dashboard_secret')
    try:
        requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
            json={'ack': ids},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f'Dashboard ack error: {e}')


def report_dashboard_undelivered(command_id, reason):
    """Take back an ack for a command that reached nobody.

    We ack on queueing, not on delivery, so `sent` on the dashboard has never
    meant a person was told — and nothing else it can see knows the
    difference. Only sent once the retries are spent and the ops channel has
    the message; the site flips the row to `undelivered`, notes it on the
    ticket, and emails a creator who was the target.

    Best-effort. The ops post has already gone out by this point, so a
    dashboard blip costs the correction, not the message."""
    config = load_config()
    url    = config.get('dashboard_commands_url')
    secret = config.get('dashboard_secret')
    if not url or not secret or not command_id:
        return
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
            json={'undelivered': [{'id': command_id, 'reason': str(reason)[:300]}]},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                f'Dashboard undelivered report {resp.status_code}: {resp.text[:200]}')
    except Exception as e:
        logger.warning(f'Dashboard undelivered report error: {e}')


# ── Creator channel ↔ dashboard profile linking ───────────────────────────────
# Ported from CollectiveBot (the JS onboarding bot), which owned this and was
# never run with a wide window — so zero of the 96 student profiles had a
# channel id, and every creator ping fell through to Notion-by-name and died.
#
# LINK ONLY. Channel CREATION stays in CollectiveBot for now; this just reports
# the "<first>-edits" channel that already exists back to the dashboard, which
# is the half the editing bridge actually needs.


def _name_tokens(s):
    """Lowercase alphanumeric tokens: "Jason Shen" -> ['jason','shen']."""
    s = unicodedata.normalize('NFKD', str(s or '')).lower()
    return [t for t in re.sub(r'[^a-z0-9]+', ' ', s).split() if t]


def _matches_student(channel_name, full_name):
    """Deliberately loose, same rule as the JS bot: existing channels are
    first-name-only ("chris-edits"), so a first-name hit counts. When the
    channel carries extra tokens, the last name must be among them — that's
    what keeps two students called Chris apart."""
    tokens = _name_tokens(full_name)
    if not tokens:
        return False
    first, last = tokens[0], tokens[-1]
    t = _name_tokens(channel_name)
    if 'edits' not in t or first not in t:
        return False
    extras = [x for x in t if x not in (first, 'edits')]
    return not extras or len(tokens) == 1 or last in extras


def fetch_provision_pending(days=0):
    """Students the dashboard wants channels linked for. [] when unconfigured."""
    config = load_config()
    url    = config.get('dashboard_provision_url')
    # Falls back to the bridge secret this bot already holds — the provision
    # route accepts either, so config only needs the url.
    secret = config.get('dashboard_provision_secret') or config.get('dashboard_secret')
    if not url or not secret:
        return []
    try:
        resp = requests.get(
            f'{url}?days={days}' if days else url,
            headers={'Authorization': f'Bearer {secret}'}, timeout=15)
        data = resp.json()
        return data.get('pending') or [] if data.get('ok') else []
    except Exception as e:
        logger.warning(f'provision fetch failed: {e}')
        return []


def post_provision_links(links):
    """Report {profileId, channelId} rows back. A rejected row means another
    profile already claims that channel — a real mismatch for a human, not
    something to retry."""
    config = load_config()
    url    = config.get('dashboard_provision_url')
    secret = config.get('dashboard_provision_secret') or config.get('dashboard_secret')
    if not url or not secret or not links:
        return
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}',
                     'Content-Type': 'application/json'},
            json={'links': links}, timeout=20)
        data = resp.json()
        if not data.get('ok'):
            logger.error(f'provision link write-back rejected: {resp.status_code}')
            return
        for f in data.get('failed') or []:
            logger.error(f"provision link rejected for {f.get('profileId')}: {f.get('error')}")
        if data.get('linked'):
            logger.info(f"provision: linked {data['linked']} channel(s) to profiles")
    except Exception as e:
        logger.error(f'provision link write-back failed: {e}')


# Ported from CollectiveBot. Naming follows what the server already does per
# section: "👥-first" in 1-1 Support, "first-edits" in In-House Editor. First
# name only, like the hand-made channels.
#
# 1-1 Support is NOT created here. Almost everyone already has one, its
# categories are at Discord's 50-channel cap, and an edits channel is what the
# editing system actually needs — creating support channels only produced three
# more and then a wall of "category is full". Existing ones are still read for
# the connections view; we just don't make new ones.
STUDENT_SECTIONS = [
    {'needle': 'in-house editor',  'label': 'In-House Editor',
     'make_name': lambda first: f'{first}-edits', 'link': True},
]
# Matched, never created — so the dashboard can show whether a creator has one.
SUPPORT_SECTION_NEEDLE = '1-1 support'
CATEGORY_CHANNEL_CAP = 50  # discord's hard limit per category


async def _deleted_channel_names(guild):
    """Channel names someone deliberately deleted, from the audit log. Without
    this, re-creating them every pass fights the person who removed them."""
    names = []
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.channel_delete,
                                            limit=300):
            name = getattr(entry.target, 'name', None) or getattr(
                getattr(entry, 'before', None), 'name', None)
            if name:
                names.append(name)
    except Exception as e:
        logger.warning(f'provision: audit log unavailable ({e}) — '
                       'deleted channels may be re-created')
    return names


async def provision_create_pass(days=0):
    """Create the two private channels an invited student needs, in whichever
    guild actually holds those categories. Skips anyone who already has one, and
    anyone whose channel was deleted on purpose.

    Needs Manage Channels. Without it every create fails and the pass degrades
    to the link-only behaviour it had before."""
    loop    = asyncio.get_event_loop()
    pending = await loop.run_in_executor(None, fetch_provision_pending, days)
    if not pending:
        return []

    links = []
    for guild in bot.guilds:
        cats_by_section = {
            s['needle']: [c for c in guild.channels
                          if isinstance(c, discord.CategoryChannel)
                          and s['needle'] in c.name.lower()]
            for s in STUDENT_SECTIONS
        }
        if not any(cats_by_section.values()):
            continue  # not the guild these sections live in
        tombstones = await _deleted_channel_names(guild)

        for section in STUDENT_SECTIONS:
            cats = cats_by_section[section['needle']]
            if not cats:
                continue
            cat_ids  = {c.id for c in cats}
            siblings = [c for c in guild.channels
                        if isinstance(c, discord.TextChannel) and c.category_id in cat_ids]
            # Counted live so the 50-cap stays right while this pass creates.
            child_count = {c.id: len([x for x in guild.channels if x.category_id == c.id])
                           for c in cats}

            for student in pending:
                name = student.get('fullName') or ''
                if not name:
                    continue
                already = [c for c in siblings if _matches_student(c.name, name)]
                if already:
                    # One hit links; two same-first-name channels are not
                    # something to pick between.
                    if section['link'] and len(already) == 1 and student.get('id'):
                        links.append({'profileId': student['id'],
                                      'channelId': str(already[0].id)})
                    continue
                if any(_matches_student(t, name) for t in tombstones):
                    continue  # deleted on purpose — leave it deleted

                room = [c for c in cats if child_count.get(c.id, 0) < CATEGORY_CHANNEL_CAP]
                if not room:
                    logger.error(f"provision: every '{section['label']}' category is full")
                    break
                parent = room[-1]
                first  = (_name_tokens(name) or [''])[0]
                try:
                    # Synced to the parent, so the section's staff perms carry.
                    # The student is unlocked when they actually join.
                    channel = await guild.create_text_channel(
                        section['make_name'](first),
                        category=parent,
                        reason=f'Auto-created for invited student {name}',
                    )
                except discord.Forbidden:
                    logger.error('provision: missing Manage Channels — cannot create')
                    return links
                except Exception as e:
                    logger.error(f'provision: create failed for {name}: {e}')
                    continue
                siblings.append(channel)
                child_count[parent.id] = child_count.get(parent.id, 0) + 1
                if section['link'] and student.get('id'):
                    links.append({'profileId': student['id'], 'channelId': str(channel.id)})
                logger.info(f"provision: created #{channel.name} in {section['label']}")

    if links:
        await loop.run_in_executor(None, post_provision_links, links)
    return links


def _support_channel_for(full_name):
    """Their 1-1 support channel ("👥-chris"), or None. Same first-name rule as
    the edits matcher, minus the 'edits' token requirement, and still refusing
    to pick when two channels could be theirs."""
    tokens = _name_tokens(full_name)
    if not tokens:
        return None
    first, last = tokens[0], tokens[-1]
    hits = []
    for g in bot.guilds:
        for c in g.channels:
            if not isinstance(c, discord.TextChannel):
                continue
            cat = (getattr(c.category, 'name', '') or '').lower()
            if SUPPORT_SECTION_NEEDLE not in cat:
                continue
            t = _name_tokens(c.name)
            if first not in t:
                continue
            extras = [x for x in t if x != first]
            if not extras or len(tokens) == 1 or last in extras:
                hits.append(c)
    return hits[0] if len(hits) == 1 else None


async def provision_link_pass(days=0):
    """Match every pending student to their <first>-edits channel across the
    guilds this bot is in, and report the ids. Ambiguity is skipped, never
    guessed: two candidate channels for one student is a human's problem."""
    loop    = asyncio.get_event_loop()
    pending = await loop.run_in_executor(None, fetch_provision_pending, days)
    if not pending:
        return
    channels = [c for g in bot.guilds for c in g.channels
                if isinstance(c, discord.TextChannel)]
    edits_channels = [c for c in channels if 'edits' in _name_tokens(c.name)]

    links = []
    ambiguous, unmatched, claimed = [], [], set()
    for student in pending:
        name = student.get('fullName') or ''
        hits = [c for c in channels if _matches_student(c.name, name)]
        if len(hits) == 1:
            ch = hits[0]
            claimed.add(ch.id)
            row = {'profileId': student.get('id'), 'channelId': str(ch.id)}
            # Their 1-1 support channel, matched but never created. Reported so
            # the dashboard can show at a glance who's actually wired up.
            support = _support_channel_for(name)
            if support:
                row['supportChannelId'] = str(support.id)
            # The channel id says WHERE to post; the user id says WHO they are.
            # Only the second lets us DM them or resolve them by account, so
            # take it while we're already holding the channel.
            uid = await _creator_user_id(ch)
            if uid:
                row['discordUserId'] = uid
            links.append(row)
        elif len(hits) > 1:
            ambiguous.append(f"{name} → {', '.join('#' + c.name for c in hits[:4])}")
        else:
            unmatched.append(name or '(no name)')

    if links:
        await loop.run_in_executor(None, post_provision_links, links)

    # Channels nobody claimed. Usually the answer to "why didn't X link" —
    # the channel is named something the matcher doesn't recognise, or belongs
    # to someone who isn't a student on the dashboard.
    orphans = [f'#{c.name}' for c in edits_channels if c.id not in claimed]
    logger.info(
        f'provision: {len(links)} linked, {len(ambiguous)} ambiguous, '
        f'{len(unmatched)} unmatched, {len(orphans)} unclaimed edits channels')
    return {
        'linked': len(links),
        'with_user_id': sum(1 for r in links if r.get('discordUserId')),
        'ambiguous': ambiguous,
        'unmatched': unmatched,
        'orphans': orphans,
    }


async def _creator_user_id(channel):
    """The creator's Discord user id, read off their own channel's permission
    overwrites. A creator channel grants exactly one member — them. Two or more
    means staff were added individually, and guessing which of them is the
    creator is the same kind of guess that broke name matching, so we don't.

    Read over REST rather than channel.overwrites: resolving overwrite targets
    to Member objects needs the privileged Members intent, which this bot
    doesn't have and which can't be switched on from code alone. The raw
    payload carries the ids either way (type 1 = member, 0 = role)."""
    try:
        data = await bot.http.get_channel(channel.id)
    except Exception as e:
        logger.warning(f'provision: cannot read overwrites for #{channel.name}: {e}')
        return None
    me = str(bot.user.id) if bot.user else ''
    ids = [str(o.get('id')) for o in (data.get('permission_overwrites') or [])
           if int(o.get('type', 0)) == 1 and str(o.get('id')) != me]
    return ids[0] if len(ids) == 1 else None


def _chunk_lines(lines, limit=1500):
    """Discord caps a message at 2000 chars — a 90-name report needs splitting."""
    out, buf = [], ''
    for line in lines:
        if len(buf) + len(line) + 1 > limit:
            out.append(buf)
            buf = ''
        buf += line + '\n'
    if buf:
        out.append(buf)
    return out


async def post_provision_report(stats):
    """Why each student did or didn't link, in the ops channel — so nobody has
    to read bot logs to find out who's missing a channel."""
    if not stats:
        return
    head = (f"🔗 **Channel linking** — {stats['linked']} linked "
            f"({stats['with_user_id']} with a Discord id)")
    send_discord_ops_channel(head)
    for label, names in (('Matched nothing', stats['unmatched']),
                         ('Matched more than one (skipped)', stats['ambiguous']),
                         ('Channels claimed by nobody', stats['orphans'])):
        if not names:
            continue
        send_discord_ops_channel(f'**{label}** ({len(names)})')
        for chunk in _chunk_lines([f'• {n}' for n in names]):
            send_discord_ops_channel(chunk)


async def provision_link_loop():
    """One wide pass at startup to backfill the whole roster, then the dashboard
    default window hourly for new students. Only the startup pass reports —
    an hourly post of the same 18 names is noise nobody reads."""
    # Create first, then link: a channel made this pass should be linked in the
    # same pass rather than an hour later.
    if PROVISION_CREATE_CHANNELS_ENABLED:
        try:
            await provision_create_pass(days=3650)
        except Exception as e:
            logger.warning(f'provision_create_pass failed: {e}')
    stats = await provision_link_pass(days=3650)
    try:
        await post_provision_report(stats)
    except Exception as e:
        logger.warning(f'provision report failed: {e}')
    while True:
        await asyncio.sleep(3600)
        if PROVISION_CREATE_CHANNELS_ENABLED:
            try:
                await provision_create_pass()
            except Exception as e:
                logger.warning(f'provision_create_pass error: {e}')
        try:
            await provision_link_pass()
        except Exception as e:
            logger.warning(f'provision_link_loop error: {e}')


def fetch_unassigned_folder_ids_from_notion():
    """Drive folder ids still waiting for an editor, straight out of the Active
    Queue. 'Raw' is the unassigned state — _assign_raw_to_editor flips a row to
    In Progress and stamps the Editor — so a Raw row is a folder nobody has
    taken. The folder id is parsed out of the Drive Link, same as everywhere
    else. Returns [] on any failure, and the caller treats [] as "don't push"
    rather than "nothing is pending"."""
    config = load_config()
    token  = config.get('notion_token')
    if not token:
        return []
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    ids, cursor = set(), None
    try:
        while True:
            body = {
                'filter': {'property': 'Status', 'select': {'equals': 'Raw'}},
                'page_size': 100,
            }
            if cursor:
                body['start_cursor'] = cursor
            resp = requests.post(url, headers=notion_headers(token), json=body, timeout=20)
            if not resp.ok:
                logger.warning(f'reconcile: notion query failed ({resp.status_code})')
                return []
            data = resp.json()
            for page in data.get('results', []):
                link = page['properties'].get('Drive Link', {}).get('url') or ''
                m = re.search(r'/folders/([a-zA-Z0-9_-]+)', link)
                if m:
                    ids.add(m.group(1))
            if not data.get('has_more'):
                break
            cursor = data.get('next_cursor')
    except Exception as e:
        logger.warning(f'reconcile: notion query error: {e}')
        return []
    return sorted(ids)


def fetch_active_queue_snapshot():
    """Every row in the Active Queue, not just the unassigned ones. The
    dashboard mirrors this so the whole editing picture is visible in one
    place — three sources disagreed (notion 51, dashboard 33, assignments
    channel 10) and there was nowhere to look and see why."""
    config = load_config()
    token  = config.get('notion_token')
    if not token:
        return []
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    rows, cursor = [], None
    try:
        while True:
            body = {'page_size': 100}
            if cursor:
                body['start_cursor'] = cursor
            resp = requests.post(url, headers=notion_headers(token), json=body, timeout=25)
            if not resp.ok:
                logger.warning(f'notion-queue: query failed ({resp.status_code})')
                return []
            data = resp.json()
            for page in data.get('results', []):
                props = page.get('properties', {})
                title_rt = props.get('Video', {}).get('title', [])
                link = props.get('Drive Link', {}).get('url') or ''
                m = re.search(r'/folders/([a-zA-Z0-9_-]+)', link)
                notes_rt = props.get('Notes', {}).get('rich_text', [])
                notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
                vc = re.search(r'Videos:\s*(\d+)', notes)
                rows.append({
                    'page_id':          page['id'],
                    'folder_id':        m.group(1) if m else '',
                    'video_name':       title_rt[0].get('plain_text', '') if title_rt else '',
                    'creator':          _notion_plain(props.get('Creator')),
                    'editor':           (props.get('Editor', {}).get('select') or {}).get('name', ''),
                    'status':           (props.get('Status', {}).get('select') or {}).get('name', ''),
                    'drive_link':       link,
                    'video_count':      int(vc.group(1)) if vc else 0,
                    'videos_completed': props.get('Videos Completed', {}).get('number') or 0,
                    'delivered_on':     (props.get('Delivered', {}).get('date') or {}).get('start', ''),
                })
            if not data.get('has_more'):
                break
            cursor = data.get('next_cursor')
    except Exception as e:
        logger.warning(f'notion-queue: query error: {e}')
        return []
    return rows


def _notion_plain(prop):
    """Notion returns Creator as rich_text in some rows and a title in others."""
    if not prop:
        return ''
    for key in ('rich_text', 'title'):
        parts = prop.get(key) or []
        if parts:
            return parts[0].get('plain_text', '')
    return (prop.get('select') or {}).get('name', '')


def post_notion_queue_snapshot():
    """Push the whole Active Queue to the dashboard, with the local ignore list
    folded in — ignored folders are a Discord-only concept, which is exactly why
    they were invisible on that side. Empty snapshot = don't push: an empty
    result is a failed query far more often than an empty queue."""
    config = load_config()
    url    = config.get('dashboard_notion_queue_url')
    secret = config.get('dashboard_secret')
    if not url or not secret:
        return
    rows = fetch_active_queue_snapshot()
    if not rows:
        logger.warning('notion-queue: nothing returned — skipping push')
        return
    # Ignored (dismissed from the assignments channel) and removed (/remove,
    # recoverable via /recover) both live in local files, not Notion. Both read
    # as "ignored" on the dashboard: the point is that the folder is off the
    # board and you can see that it is.
    off_board = set(_load_ignored_folder_ids())
    try:
        off_board |= set(load_removed_folders().keys())
    except Exception:
        pass
    for r in rows:
        r['ignored'] = r['page_id'] in off_board or (
            bool(r['folder_id']) and r['folder_id'] in off_board)
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}',
                     'Content-Type': 'application/json'},
            json={'rows': rows}, timeout=60)
        data = resp.json()
        if not data.get('ok'):
            logger.warning(f"notion-queue rejected ({resp.status_code}): {data.get('error')}")
            return
        logger.info(f"notion-queue: mirrored {data.get('upserted')} row(s), "
                    f"{data.get('marked_missing')} newly missing")
    except Exception as e:
        logger.warning(f'notion-queue push failed: {e}')


def post_pending_reconcile():
    """Tell the dashboard which drive folders are still genuinely waiting for an
    editor, so it can close out the ones that aren't.

    We push on folder DETECTION, so every folder we spot becomes a `submitted`
    ticket over there — but a folder assigned, ignored or handled in Notion
    without post_dashboard_assignment firing left its ticket sitting forever.
    That's how the dashboard got to 34 waiting while this channel showed 10.

    Sends nothing when the pending set is empty: "nothing is pending" and "the
    lookup failed" are indistinguishable on the wire, and the dashboard rightly
    refuses an empty list rather than archiving its whole queue."""
    config = load_config()
    url    = config.get('dashboard_reconcile_url')
    secret = config.get('dashboard_secret')
    if not url or not secret:
        return
    # Two sources, unioned, because each one alone is wrong in a different way.
    # Notion's Active Queue is the real ledger — a row per video, Status 'Raw'
    # until _assign_raw_to_editor flips it to In Progress — but a folder we've
    # only just detected may not have a row yet. The local pending store covers
    # exactly that gap. Archiving needs both to agree that a folder is done.
    folder_ids = set(fetch_unassigned_folder_ids_from_notion())
    notion_count = len(folder_ids)
    folder_ids |= {
        str(item.get('folder_id') or '').strip()
        for item in load_pending_ops_assigns().values()
        if str(item.get('folder_id') or '').strip()
    }
    folder_ids = sorted(folder_ids)
    # Notion returning nothing is far more likely an API hiccup than a genuinely
    # empty queue, and the difference decides whether the dashboard clears its
    # board. Don't push on the local store alone.
    if not folder_ids or notion_count == 0:
        logger.warning('reconcile: no unassigned rows from notion — skipping push')
        return
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}',
                     'Content-Type': 'application/json'},
            json={'pending_folder_ids': folder_ids}, timeout=20)
        data = resp.json()
        if not data.get('ok'):
            logger.warning(f"reconcile rejected ({resp.status_code}): {data.get('error')}")
            return
        if data.get('archived'):
            logger.info(f"reconcile: dashboard archived {data['archived']} stale ticket(s) "
                        f"of {data.get('checked')} checked")
    except Exception as e:
        logger.warning(f'reconcile push failed: {e}')


async def reconcile_loop():
    """A first pass shortly after boot, then hourly. The short delay is just to
    let the gateway settle — sleeping the full hour first meant a restart to fix
    the queue didn't fix the queue for an hour. The dashboard applies its own
    grace window, so a folder detected seconds before a push isn't archived."""
    await asyncio.sleep(120)
    while True:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, post_pending_reconcile)
        except Exception as e:
            logger.warning(f'reconcile_loop error: {e}')
        # Same cadence, same data source — mirror the whole Active Queue so the
        # dashboard can show what the reconcile is reasoning about.
        try:
            await loop.run_in_executor(None, post_notion_queue_snapshot)
        except Exception as e:
            logger.warning(f'notion-queue loop error: {e}')
        await asyncio.sleep(3600)


_dashboard_commands_started = False

async def dashboard_commands_loop():
    """Poll the CC dashboard every 30 s for editor assignments made in its UI
    and feed them through the normal queue → assign_folder path, so a
    dashboard assignment produces the exact same embed + Start button +
    deadline state as a Telegram one. Commands are acked only after they're
    safely in discord_queue.json; unknown editor names are dropped with an
    ops-channel warning instead of poisoning the queue with eternal retries."""
    while True:
        await asyncio.sleep(30)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, flush_dashboard_pushes)
            url, commands = await loop.run_in_executor(None, fetch_dashboard_commands)
            if not commands:
                continue
            editors = await loop.run_in_executor(None, fetch_editors_from_notion)
            if not editors:
                continue  # Notion hiccup — leave commands pending, retry next cycle
            items, acked = [], []
            aq_snapshot = None  # lazily fetched by 'archive' commands, cached across this batch
            for cmd in commands:
                kind = cmd.get('kind') or 'assign'

                # One-time cleanup: these 7 ticket_ids are test batches off a
                # staff account that got swept into the mirror before the site
                # started gating on the creator being role 'student'. The site
                # won't send new commands for them, but drop anything for one
                # of these ids (and scrub any leftover dashboard_batches.json
                # entry) instead of letting it keep inflating an editor's
                # active count.
                ticket_id = _cmd_ticket_id(cmd)
                if ticket_id in TEST_DASHBOARD_TICKET_IDS:
                    data    = load_dashboard_batches()
                    removed = data.pop(ticket_id, None) is not None
                    # Older entries for these ids were filed under the
                    # editor|folder_name fallback key (see _cmd_ticket_id) —
                    # sweep those out too, matched by the ticket_id embedded
                    # in their stored ticket_url.
                    for k, b in list(data.items()):
                        m = _TICKET_URL_ID_RE.search(str(b.get('ticket_url') or ''))
                        if m and m.group(1) == ticket_id:
                            del data[k]
                            removed = True
                    if removed:
                        save_dashboard_batches(data)
                    logger.info(f'dashboard_commands_loop: dropped test ticket {ticket_id} ({kind})')
                    acked.append(cmd.get('id'))
                    continue

                # An assign_request names no editor on purpose — it's a website
                # batch nobody has claimed, going to the assignments channel so
                # Vex picks one there. Everything else must resolve an editor.
                # A generic embed for one person. The dashboard owns the wording
                # and the routing ids; we just deliver it. New pings land here
                # as new payloads, not as new kinds and new handlers.
                if kind == 'message':
                    items.append({
                        'type':               'cc_dashboard_message',
                        # Kept so an undeliverable one can be reported back
                        # against the row we're about to ack.
                        'command_id':         cmd.get('id'),
                        'target':             cmd.get('target', 'creator'),
                        'title':              cmd.get('title', ''),
                        'description':        cmd.get('description', ''),
                        'url':                cmd.get('url', ''),
                        'fields':             cmd.get('fields') or [],
                        'student_name':       cmd.get('student_name', ''),
                        'student_username':   cmd.get('student_username', ''),
                        'creator_channel_id': cmd.get('creator_channel_id', ''),
                        'creator_discord_id': cmd.get('creator_discord_id', ''),
                        'editor_name':        cmd.get('editor_name', ''),
                        'editor_discord_id':  cmd.get('editor_discord_id', ''),
                        # The routing key the dashboard leads with — names
                        # disagree across the two systems, addresses don't.
                        'editor_email':       cmd.get('editor_email', ''),
                    })
                    acked.append(cmd.get('id'))
                    continue

                # `assign_request_update` is the same card, corrected: the site
                # sends it for every push after the first (a changed video count,
                # a renamed batch). It had no branch here at all, so it fell
                # through to the editor gate below, which an update carries no
                # editor for — every correction was warned about in ops and
                # dropped (2026-08-20).
                if kind in ('assign_request', 'assign_request_update'):
                    items.append({
                        'type':         'cc_dashboard_assign_request',
                        'is_update':    kind == 'assign_request_update',
                        'ticket_id':    _cmd_ticket_id(cmd),
                        'client_name':  cmd.get('client_name', ''),
                        'folder_name':  cmd.get('folder_name', ''),
                        'video_count':  cmd.get('video_count', 0),
                        'student_name':     cmd.get('student_name', ''),
                        'student_username': cmd.get('student_username', ''),
                        'creator_channel_id': cmd.get('creator_channel_id', ''),
                        'formats':      cmd.get('formats', ''),
                        'ticket_url':   cmd.get('ticket_url', ''),
                    })
                    acked.append(cmd.get('id'))
                    continue

                # An offer names an editor, but it is NOT an assignment yet —
                # the site is asking whether they'll take it, and nothing moves
                # until they answer. Handled above the editor gate on purpose:
                # the card carries its own routing ids (email, discord id) and
                # is addressed to one person, so a Notion roster miss must not
                # swallow it the way the gate below would.
                if kind == 'assign_offer':
                    items.append({
                        'type':               'cc_dashboard_assign_offer',
                        'command_id':         cmd.get('id'),
                        'offer_id':           cmd.get('offer_id', ''),
                        'ticket_id':          _cmd_ticket_id(cmd),
                        'client_name':        cmd.get('client_name', ''),
                        'folder_name':        cmd.get('folder_name', ''),
                        'video_count':        cmd.get('video_count', 0),
                        'formats':            cmd.get('formats', ''),
                        'reason':             cmd.get('reason', ''),
                        'expires_at':         cmd.get('expires_at', ''),
                        'student_name':       cmd.get('student_name', ''),
                        'student_username':   cmd.get('student_username', ''),
                        'creator_channel_id': cmd.get('creator_channel_id', ''),
                        'editor_name':        cmd.get('editor_name', ''),
                        'editor_discord_id':  cmd.get('editor_discord_id', ''),
                        'editor_email':       cmd.get('editor_email', ''),
                        'ticket_url':         cmd.get('ticket_url', ''),
                    })
                    acked.append(cmd.get('id'))
                    continue

                if kind == 'archive':
                    # The dashboard archived a batch that came from a Drive folder
                    # (external_ref) — Notion never hears about it on its own, so
                    # the folder stays live there and gets re-assigned. Mirror
                    # exactly what /remove does: archive the page and cache it for
                    # /recover. No Discord post — the dashboard already told
                    # whoever needed telling.
                    folder_id = cmd.get('folder_id', '')
                    # Take the unclaimed assign card down FIRST. The Notion
                    # work below can fail and retry; the card should come down
                    # on the first pass regardless, and it's what actually
                    # puts an editor on a dead batch.
                    await retract_pending_assign_cards(
                        ticket_id=_cmd_ticket_id(cmd), folder_id=folder_id)
                    # A website-born batch has no Drive folder and no Notion
                    # row — its only record here is dashboard_batches.json, and
                    # nothing ever removed an entry from it. So a batch staff
                    # archived on the site sat in that editor's /stats forever
                    # under 🌐 Website Batches. Oliver's test batches were still
                    # showing on Josh a fortnight later.
                    if not folder_id:
                        tid  = _cmd_ticket_id(cmd)
                        data = load_dashboard_batches()
                        if data.pop(tid, None) is not None:
                            save_dashboard_batches(data)
                            logger.info(
                                f'dashboard_commands_loop: archive dropped website '
                                f'batch {tid} ({cmd.get("folder_name", "?")})'
                            )
                        acked.append(cmd.get('id'))
                        continue
                    if aq_snapshot is None:
                        aq_snapshot = await loop.run_in_executor(None, fetch_active_queue_snapshot)
                    match = next((r for r in aq_snapshot if folder_id and r['folder_id'] == folder_id), None)
                    if not match:
                        # Not in the live (non-archived) Active Queue view — either
                        # we already archived it on a prior delivery of this same
                        # command, or the page is otherwise gone. Either way the
                        # job is already done; don't retry forever.
                        logger.info(
                            f"dashboard_commands_loop: archive for folder_id={folder_id!r} "
                            f"({cmd.get('client_name', '?')} / {cmd.get('folder_name', '?')}) "
                            f"not found in Active Queue — already archived or gone, acking."
                        )
                        acked.append(cmd.get('id'))
                        continue
                    if match['status'] == 'Raw':
                        display_status = 'Pending'
                    elif match['status'] == 'Revision':
                        display_status = 'Revision'
                    else:
                        display_status = 'Active'
                    row = {
                        'notion_page_id':   match['page_id'],
                        'folder_id':        folder_id,
                        'folder_name':      cmd.get('folder_name') or match['video_name'],
                        'client_name':      cmd.get('client_name') or match['creator'],
                        'editor_name':      cmd.get('editor_name') or match['editor'],
                        'video_count':      match['video_count'],
                        'videos_completed': match['videos_completed'],
                        'status':           display_status,
                    }
                    ok = await loop.run_in_executor(None, archive_active_queue_page, match['page_id'], row)
                    if not ok:
                        logger.warning(
                            f"dashboard_commands_loop: archive PATCH failed for "
                            f"{row['client_name']} / {row['folder_name']} — will retry"
                        )
                        continue
                    acked.append(cmd.get('id'))
                    continue

                # The drive branch below reads the Active Queue to get the
                # creator's Notion name; fetch the snapshot once per batch, the
                # same one the archive branch uses.
                if kind not in ('message', 'assign_request', 'assign_request_update') and cmd.get('folder_id') and aq_snapshot is None:
                    aq_snapshot = await loop.run_in_executor(None, fetch_active_queue_snapshot)

                editor_name = resolve_editor_key(cmd, editors)
                if not editor_name:
                    uid = str(cmd.get('editor_discord_id') or '').strip()
                    send_discord_ops_channel(
                        f"⚠️ Dashboard sent a **{kind}** for "
                        f"**{cmd.get('folder_name', '?')}** to "
                        f"**{(cmd.get('editor_name') or '?').strip()}**"
                        + (f" (<@{uid}>)" if uid else "") +
                        f", but they aren't in the Notion editor list — skipped. "
                        f"Handle it from here."
                    )
                    acked.append(cmd.get('id'))
                    continue

                if kind == 'revision':
                    items.append({
                        'type':           'dashboard_revision',
                        'client_name':    cmd.get('client_name', ''),
                        'folder_name':    cmd.get('folder_name', ''),
                        'folder_id':      cmd.get('folder_id', ''),
                        'video_count':    cmd.get('video_count', 0),
                        'editor_name':    editor_name,
                        'notes':          cmd.get('notes', ''),
                        'notion_page_id': '',
                        'from_dashboard': True,
                    })
                elif kind == 'approve':
                    items.append({
                        'type':           'cc_dashboard_approve',
                        'client_name':    cmd.get('client_name', ''),
                        'folder_name':    cmd.get('folder_name', ''),
                        'folder_id':      cmd.get('folder_id', ''),
                        'editor_name':    editor_name,
                        # Routing keys the dashboard leads with: names disagree
                        # across the two systems, addresses and ids don't.
                        'editor_email':      cmd.get('editor_email', ''),
                        'editor_discord_id': cmd.get('editor_discord_id', ''),
                        'student_name':   cmd.get('student_name', ''),
                    })
                elif kind == 'notify':
                    # A ticket that was born in the dashboard — no Drive folder,
                    # so no Notion row, no deadline entry and no /complete. Just
                    # put it in front of the editor with a link back.
                    items.append({
                        'type':         'cc_dashboard_notify',
                        'ticket_id':    _cmd_ticket_id(cmd),
                        'client_name':  cmd.get('client_name', ''),
                        'folder_name':  cmd.get('folder_name', ''),
                        'video_count':  cmd.get('video_count', 0),
                        'editor_name':  editor_name,
                        # Routing keys the dashboard leads with: names disagree
                        # across the two systems, addresses and ids don't.
                        'editor_email':      cmd.get('editor_email', ''),
                        'editor_discord_id': cmd.get('editor_discord_id', ''),
                        'student_name':     cmd.get('student_name', ''),
                        'student_username': cmd.get('student_username', ''),
                        'formats':      cmd.get('formats', ''),
                        'ticket_url':   cmd.get('ticket_url', ''),
                        'creator_url':  cmd.get('creator_url', ''),
                        'is_reassign':  bool(cmd.get('is_reassign')),
                        # Absent/empty on a fresh (non-reassign) notify; when
                        # present on a reassign, drives the outgoing-editor
                        # ping in _notify_previous_editor — never fired on the
                        # name alone, only when the discord id is known.
                        'previous_editor_name':        cmd.get('previous_editor_name', ''),
                        'previous_editor_discord_id':  cmd.get('previous_editor_discord_id', ''),
                        # Website batches have no Notion creator row, so the
                        # dashboard sends the creator's own edits channel.
                        'creator_channel_id': cmd.get('creator_channel_id', ''),
                        'creator_discord_id': cmd.get('creator_discord_id', ''),
                        # A backfill notify mirrors a batch the editor has
                        # already been holding since before today's fix — it's
                        # not a new assignment, so it renders differently and
                        # skips the creator ping (see handle_cc_dashboard_notify).
                        'backfill':     bool(cmd.get('backfill')),
                        'assigned_at':  cmd.get('assigned_at'),
                    })
                elif kind == 'delivered':
                    # Website-native batch finished on the dashboard — no Notion
                    # row to flip Delivered on, so this is the only path that
                    # gets these video counts into Editor Profiles / /stats.
                    items.append({
                        'type':         'cc_dashboard_delivered',
                        'ticket_id':    _cmd_ticket_id(cmd),
                        'client_name':  cmd.get('client_name', ''),
                        'folder_name':  cmd.get('folder_name', ''),
                        'video_count':  cmd.get('video_count', 0),
                        'editor_name':  editor_name,
                        'student_name': cmd.get('student_name', ''),
                    })
                elif kind == 'reopen':
                    # A delivered website batch went back for changes. Flip it
                    # back to active so it reads correctly in /stats and so the
                    # next 'delivered' credits instead of being treated as a
                    # retried duplicate of the first round.
                    items.append({
                        'type':         'cc_dashboard_reopen',
                        'ticket_id':    _cmd_ticket_id(cmd),
                        'client_name':  cmd.get('client_name', ''),
                        'folder_name':  cmd.get('folder_name', ''),
                        'editor_name':  editor_name,
                        'student_name': cmd.get('student_name', ''),
                        'ticket_url':   cmd.get('ticket_url', ''),
                    })
                else:
                    items.append({
                        # 'client_name' is the Notion Creator property everywhere
                        # else in this codebase, and it is load-bearing: assign_folder
                        # walks DRIVE_ROOT -> <client_name> -> Raw Footage to build the
                        # Drive links, and handle_creator_notify looks the creator's
                        # channel up by it. It has to be Notion's own value.
                        #
                        # Nothing the dashboard can send is that value. Its brand
                        # ("Phrasly") duplicates folder_name and drops the person; its
                        # student_name is the full legal name ("Joshua Jalapa",
                        # "Jonathan Gedam") while Notion — and therefore the Drive
                        # folder — uses the short one ("Joshua", "Jonny"). Either way
                        # the walk finds nothing and the editor gets an embed with no
                        # links to start from. 19 folders went out like that on
                        # 2026-08-16 before this was spotted.
                        #
                        # So take it from the Active Queue row when we have a folder
                        # id, and only fall back to what was sent. Snapshot is fetched
                        # once per batch and shared with the archive branch above.
                        'client_name': _creator_for_folder(
                            cmd.get('folder_id', ''), aq_snapshot
                        ) or cmd.get('student_name') or cmd.get('client_name', ''),
                        'folder_name': cmd.get('folder_name', ''),
                        'video_count': cmd.get('video_count', 0),
                        'folder_id':   cmd.get('folder_id', ''),
                        'editor_name': editor_name,
                        'is_reassign': bool(cmd.get('is_reassign')),
                        # The outgoing editor. The `notify` branch above has
                        # carried these for weeks and pings them; this branch
                        # dropped both on the floor, so a DRIVE folder moved
                        # from the dashboard told the new editor and left the
                        # old one to notice their folder had gone. Same fields,
                        # same handler, so the two provenances finally agree.
                        'previous_editor_name':       cmd.get('previous_editor_name', ''),
                        'previous_editor_discord_id': cmd.get('previous_editor_discord_id', ''),
                        # Came FROM the dashboard — don't bounce it back out.
                        'from_dashboard': True,
                    })
                acked.append(cmd.get('id'))
            if items:
                with QUEUE_LOCK:
                    existing = []
                    if os.path.exists(QUEUE_FILE):
                        with open(QUEUE_FILE) as f:
                            existing = json.load(f)
                    with open(QUEUE_FILE, 'w') as f:
                        json.dump(existing + items, f, indent=2)
            acked = [a for a in acked if a]
            if acked:
                await loop.run_in_executor(None, ack_dashboard_commands, url, acked)
        except Exception as e:
            logger.warning(f'dashboard_commands_loop error: {e}')


# ── Telegram to notion_bridge bot (for callbacks notion_bridge.py handles) ─────

def send_notion_bridge_telegram(message, keyboard=None):
    config = load_config()
    url = f"https://api.telegram.org/bot{config['notion_bridge_token']}/sendMessage"
    payload = {
        'chat_id':    config['notion_bridge_chat_id'],
        'text':       message,
        'parse_mode': 'HTML',
    }
    if keyboard:
        payload['reply_markup'] = json.dumps(keyboard)
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        logger.error(f'Telegram (bridge) error: {e}')
        return {}


# ── Ignored folder helpers ─────────────────────────────────────────────────────

def _load_ignored_folder_ids():
    try:
        with open(IGNORED_FOLDERS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _add_ignored_folder_id(folder_id: str):
    ids = list(_load_ignored_folder_ids())
    if folder_id not in ids:
        ids.append(folder_id)
    with open(IGNORED_FOLDERS_FILE, 'w') as f:
        json.dump(ids, f, indent=2)


# ── Removed folders cache ───────────────────────────────────────────────────────

def load_removed_folders():
    if os.path.exists(REMOVED_FOLDERS_FILE):
        with _REMOVED_FOLDERS_LOCK:
            with open(REMOVED_FOLDERS_FILE) as f:
                return json.load(f)
    return {}


def save_removed_folders(data):
    with _REMOVED_FOLDERS_LOCK:
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


def _creator_for_folder(folder_id, snapshot):
    """Notion's Creator value for a drive folder, or '' if we can't tell.

    This is the name the Drive client folder is called, so it's what
    assign_folder needs to resolve Raw Footage links, and what
    fetch_creator_discord_info needs to find the creator's channel. The
    dashboard cannot supply it — it knows people by their full names and
    Notion knows them by short ones.
    """
    if not folder_id or not snapshot:
        return ''
    row = next((r for r in snapshot if r.get('folder_id') == folder_id), None)
    return (row or {}).get('creator') or ''


def archive_active_queue_page(page_id, row):
    """Archives a Notion Active Queue page and caches it for /recover — the exact
    same steps as /remove (RemoveFolderSelect._on_select's Pending/Active branch),
    factored out so the dashboard's archive command can drive it too. `row` needs
    client_name/folder_name/folder_id/status (display status: Pending/Active/Revision).
    Returns True once the page is archived (including if it already was)."""
    config = load_config()
    token  = config['notion_token']
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=notion_headers(token),
        json={'archived': True},
        timeout=15,
    )
    if not resp.ok:
        return False
    cache_removed_folder(page_id, row, row['status'])
    pop_deadline_entry(row.get('folder_id', ''), page_id)
    return True


# ── Dashboard-native batches (no Drive folder, no Notion row) ──────────────────
# /stats is 100% Notion-driven, and website-only batches never get an Active
# Queue row (see handle_cc_dashboard_notify) — so this file is the only record
# of what's currently active or delivered for them, purely to surface it in
# /stats. It doesn't feed deadlines, /complete, or anything else Notion-backed.

_LIVE_BATCHES_CACHE = {'at': 0.0, 'data': None}
LIVE_BATCHES_TTL = 60


def fetch_live_dashboard_batches():
    """Website batches straight from the dashboard, or None if it can't say.

    This file was our own copy of something the site already knows, and it
    failed the way copies do: the box died on 2026-08-19 and took 45 in-flight
    batches out of /stats with it. The site owns them; we read them.

    Cached for a minute — /stats, /editorstats and recalculate_active_videos
    can each ask several times in one command, and none of them needs a fresher
    answer than that. None (not {}) on any failure, so callers can tell "the
    site says there are none" from "the site didn't answer" and fall back to
    the file instead of reporting everyone as idle."""
    now = time.time()
    if _LIVE_BATCHES_CACHE['data'] is not None and now - _LIVE_BATCHES_CACHE['at'] < LIVE_BATCHES_TTL:
        return _LIVE_BATCHES_CACHE['data']
    config = load_config()
    # Derived from the commands url when it isn't spelled out, so nobody has to
    # rewrite a config (or a PaaS secret) to turn this on — same host, same
    # bridge, same secret.
    url = config.get('dashboard_batches_url')
    if not url and config.get('dashboard_commands_url'):
        url = config['dashboard_commands_url'].rsplit('/', 1)[0] + '/editing-batches'
    secret = config.get('dashboard_secret')
    if not url or not secret:
        return None
    try:
        resp = requests.get(
            url, headers={'Authorization': f'Bearer {secret}'}, timeout=10)
        if resp.status_code != 200:
            logger.warning(f'live batches: dashboard returned {resp.status_code}')
            return None
        batches = resp.json().get('batches')
        if not isinstance(batches, dict):
            logger.warning('live batches: unexpected payload shape')
            return None
    except Exception as e:
        logger.warning(f'live batches: {e}')
        return None
    _LIVE_BATCHES_CACHE['at'] = now
    _LIVE_BATCHES_CACHE['data'] = batches
    return batches


def load_local_dashboard_batches():
    """Our own file, without the site's live view mixed in.

    The WRITERS use this. 'Have I already credited this delivery' is a fact
    about us, and only the file knows it — the live feed says `delivered`
    because the batch IS delivered, which is not the same statement. Reading
    the merged view to decide that made a first delivery look like a retry and
    dropped the credit (Aki's Motion batch, 2026-08-19)."""
    with _DASHBOARD_BATCHES_LOCK:
        if os.path.exists(DASHBOARD_BATCHES_FILE):
            with open(DASHBOARD_BATCHES_FILE) as f:
                return json.load(f)
    return {}


def load_dashboard_batches():
    """The site's live answer when it has one, our own file when it doesn't.

    Delivery bookkeeping still writes to the file (mark_dashboard_batch_delivered
    dedupes credits, which is genuinely bot-side state), so a locally-recorded
    'delivered' is merged over the live row rather than being overwritten by it.
    """
    with _DASHBOARD_BATCHES_LOCK:
        local = {}
        if os.path.exists(DASHBOARD_BATCHES_FILE):
            with open(DASHBOARD_BATCHES_FILE) as f:
                local = json.load(f)
    live = fetch_live_dashboard_batches()
    if live is None:
        return local
    merged = dict(live)
    for key, row in local.items():
        if key in merged and row.get('status') == 'delivered':
            # We already counted this delivery; the site may still show the
            # batch as active for a moment. Ours wins, or the next `delivered`
            # command would credit it twice.
            merged[key] = {**merged[key], **row}
        elif key not in merged:
            # Absent from the live set means the site considers it finished:
            # approved, archived, or cancelled. Every row in this file got here
            # from a website `notify`, so the site is authoritative for all of
            # them — keeping one resurrected work nobody owes (Whitney's
            # archived Composio green screen sat in /stats after Storm had
            # finished the real one, 2026-08-19).
            #
            # The delivered ones stay regardless: that flag is our own ledger of
            # what we've already credited, and dropping it lets a resent
            # `delivered` count twice.
            if row.get('status') == 'delivered':
                merged[key] = row
    return merged


def save_dashboard_batches(data):
    with _DASHBOARD_BATCHES_LOCK:
        with open(DASHBOARD_BATCHES_FILE, 'w') as f:
            json.dump(data, f, indent=2)


def _dashboard_batch_key(item):
    # ticket_id is the dashboard's stable id; fall back to editor+folder for
    # any command that predates ticket_id being wired through everywhere.
    ticket_id = str(item.get('ticket_id') or '').strip()
    if ticket_id:
        return ticket_id
    return f"{item.get('editor_name', '')}|{item.get('folder_name', '')}"


def _parse_dashboard_assigned_at(raw):
    """A backfill notify carries the batch's real assignment time so /stats
    can show how long it's actually been sitting, not how long ago the
    backfill ran. Falls back to now for a normal (non-backfill) notify, or if
    the ISO string is missing/unparseable."""
    if raw:
        try:
            return datetime.fromisoformat(str(raw).replace('Z', '+00:00')).timestamp()
        except ValueError:
            pass
    return time.time()


def upsert_active_dashboard_batch(item):
    data = load_local_dashboard_batches()
    data[_dashboard_batch_key(item)] = {
        'editor_name':       item.get('editor_name', ''),
        'editor_discord_id': item.get('editor_discord_id', ''),
        'client_name':       item.get('client_name', ''),
        'student_name':      item.get('student_name', ''),
        'student_username':  item.get('student_username', ''),
        'folder_name':       item.get('folder_name', ''),
        'video_count':       item.get('video_count', 0),
        'formats':           item.get('formats', ''),
        'ticket_url':        item.get('ticket_url', ''),
        'status':            'active',
        'assigned_at':       _parse_dashboard_assigned_at(item.get('assigned_at')),
        'delivered_at':      None,
    }
    save_dashboard_batches(data)


# How long after a delivery a repeat 'delivered' for the same ticket still
# counts as the bot retrying the same command rather than a genuine second
# round. Retries are seconds apart; the shortest real revision turnaround we've
# seen is a little over an hour.
DELIVERED_RETRY_WINDOW = 45 * 60


def mark_dashboard_batch_delivered(item):
    """Flags the matching batch delivered. Returns (batch, already_delivered).

    A ticket we have no row for is RECORDED from the payload and credited —
    the row we write is itself what a retry dedupes against. Refusing instead
    was how real deliveries went uncredited (see the note in the body).
    batch is None only if that write fails.

    already_delivered is True when the batch was already in 'delivered' status
    — an ack that never reached the site, so it resends the same command and
    we'd otherwise double-credit Editor Profiles for the same round. There is
    currently no signal that reopens a website batch to 'active' after a
    revision (the 'revision' command doesn't carry ticket_id and never touches
    this file), so today every ticket_id legitimately delivers at most once;
    a revision-then-redeliver flow needs the site to send that reopen signal
    before this can safely credit a second round for the same ticket_id."""
    data  = load_local_dashboard_batches()
    key   = _dashboard_batch_key(item)
    batch = data.get(key)
    if not batch:
        # Nothing on file. Dropping the credit here was the safe-looking choice
        # and it silently cost editors real work: Naomi's 8 cuts on Kai Gangi's
        # asmi batch, approved before the last ordered cut, then credited to
        # nobody twice — once when the delivery push never fired, and again
        # when it was sent by hand and landed on a ticket the live feed no
        # longer lists (approved batches aren't in it).
        #
        # The dedupe worry is answered by writing the row instead of refusing:
        # a retry of the same command then finds it in 'delivered' and skips,
        # which is exactly the guarantee the file gave before.
        data[key] = {
            'editor_name':       item.get('editor_name', ''),
            'editor_discord_id': item.get('editor_discord_id', ''),
            'client_name':       item.get('client_name', ''),
            'student_name':      item.get('student_name', ''),
            'student_username':  item.get('student_username', ''),
            'folder_name':       item.get('folder_name', ''),
            'video_count':       item.get('video_count', 0),
            'formats':           item.get('formats', ''),
            'ticket_url':        item.get('ticket_url', ''),
            'status':            'delivered',
            'assigned_at':       None,
            'delivered_at':      time.time(),
            # Says where this row came from, since it never saw an assignment.
            'recovered':         True,
        }
        save_dashboard_batches(data)
        logger.info(
            f"cc_dashboard_delivered: no batch on file for "
            f"{item.get('ticket_id')!r} — recorded from the delivered payload "
            f"and crediting it"
        )
        return data[key], False
    # A repeat 'delivered' on a row that already reads delivered is a bot retry
    # — unless enough time has passed that it can't be one. When the reopen
    # signal goes missing (the editor isn't in the Notion list, the ticket lost
    # its editor_id, the site never enqueued it) this row stays 'delivered'
    # through the whole next round, and round two was then swallowed as a retry
    # and credited to nobody (2026-08-20). Retries live in the seconds-to-minutes
    # range; a second delivery hours later is real work.
    delivered_at = batch.get('delivered_at') or 0
    stale        = delivered_at and (time.time() - delivered_at) > DELIVERED_RETRY_WINDOW
    already_delivered = batch.get('status') == 'delivered' and not stale
    if batch.get('status') == 'delivered' and stale:
        logger.info(
            f"cc_dashboard_delivered: ticket {item.get('ticket_id')!r} was still "
            f"marked delivered from "
            f"{round((time.time() - delivered_at) / 3600, 1)}h ago — the reopen "
            f"for this round never arrived, crediting it as a new round"
        )
    batch['status']       = 'delivered'
    batch['delivered_at'] = time.time()
    if item.get('video_count'):
        batch['video_count'] = item['video_count']
    save_dashboard_batches(data)
    return batch, already_delivered


def reopen_dashboard_batch(item):
    """Flips a delivered batch back to active — the counterpart to the dedupe
    check in mark_dashboard_batch_delivered, so the next 'delivered' for this
    ticket_id credits instead of being logged as a retry. No-ops (returns
    None) if the ticket isn't currently in 'delivered' status: a reopen for a
    ticket that's still active, or one we never tracked, has nothing to flip."""
    data  = load_local_dashboard_batches()
    key   = _dashboard_batch_key(item)
    batch = data.get(key)
    if not batch or batch.get('status') != 'delivered':
        return None
    batch['status']       = 'active'
    batch['delivered_at'] = None
    save_dashboard_batches(data)
    return batch


def _batch_belongs_to(b, editor_name, editors=None):
    """Is this dashboard batch this editor's?

    The row carries the name the WEBSITE knows the editor by; `editor_name` is
    the key NOTION knows them by, and for four of sixteen those are different
    strings — Jermaine is Josh here, ronruzzelv is Ron, Ysabel is Ysa, Zyon
    Kahili is Zyon. Comparing the two directly made their website batches
    invisible in /stats while the assignment ping arrived perfectly well
    (founder, 2026-08-20: "josh did get a message... but why not in /stats").

    So identity, not spelling: the editor's Notion row supplies their email and
    Discord id, and either one matching is proof. The name comparison stays as
    the fallback for rows written before the feed carried an email.
    """
    if b.get('editor_name') == editor_name:
        return True
    row = (editors or {}).get(editor_name) or {}
    email = (row.get('email') or '').strip().lower()
    if email and (b.get('editor_email') or '').strip().lower() == email:
        return True
    uid = str(row.get('discord_user_id') or '').strip()
    if uid and str(b.get('editor_discord_id') or '').strip() == uid:
        return True
    return False


def active_dashboard_batches_for_editor(editor_name, editors=None):
    data = load_dashboard_batches()
    return [
        b for b in data.values()
        if b.get('status') == 'active' and _batch_belongs_to(b, editor_name, editors)
    ]


def active_dashboard_batches_for_creator(client_name):
    """Website-native batches belonging to one creator, for their own /stats.

    The creator branch of /stats is 100% Notion-driven, and a website batch has
    no Active Queue row — so a creator who submitted on the dashboard saw
    "Active Folders (0)" while their editor was mid-way through the work. 37
    live batches across 10+ creators were invisible this way on 2026-08-16,
    including two separate 84-video orders.

    Matched on the creator's name as the dashboard sent it (`student_name`),
    falling back to `client_name` for entries written before the two were
    split apart. Names are the only handle here — dashboard_batches.json has
    no creator id — so this deliberately stays an exact, case-insensitive
    match: a loose one would show a creator somebody else's batch, which is
    far worse than showing them nothing.
    """
    wanted = (client_name or '').strip().lower()
    if not wanted:
        return []
    out = []
    for b in load_dashboard_batches().values():
        if b.get('status') != 'active':
            continue
        who = (b.get('student_name') or b.get('client_name') or '').strip().lower()
        if who == wanted:
            out.append(b)
    return out


def active_website_batches_by_editor():
    """All active (not yet delivered) website-native batches, grouped by
    editor. /stats already surfaces these per-editor via
    active_dashboard_batches_for_editor(); /editorstats' team-wide view had
    no equivalent, so an editor's real load (and the % shown in Editor Load)
    silently excluded every website-only ticket — found 2026-08-14 when Jewel
    showed 70/70 (Notion-only) but was actually carrying 185 more videos'
    worth of website batches on top, 255 total. Local file read
    (dashboard_batches.json) — no network, safe to call directly."""
    data = load_dashboard_batches()
    grouped = {}
    for b in data.values():
        if b.get('status') != 'active':
            continue
        grouped.setdefault(b.get('editor_name') or 'Unassigned', []).append(b)
    return grouped


def _as_epoch(value):
    """Seconds, from either shape this field arrives in.

    We wrote delivered_at with time.time() — a float. Since the batches moved
    to the live dashboard feed they can also arrive as the site's ISO string,
    and comparing those to a float is a TypeError that took /stats down in
    every channel holding a delivered website batch (Steven's, 2026-08-20).
    Anything unparseable reads as 0, i.e. 'long ago', which is the honest
    answer for a timestamp we can't understand."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def dashboard_delivered_videos_for_editor(editor_name, since_ts=None, editors=None):
    data  = load_dashboard_batches()
    total = 0
    for b in data.values():
        if b.get('status') != 'delivered' or not _batch_belongs_to(b, editor_name, editors):
            continue
        if since_ts is not None and _as_epoch(b.get('delivered_at')) < since_ts:
            continue
        total += b.get('video_count') or 0
    return total


def load_pending_ops_assigns():
    with PENDING_OPS_ASSIGNS_LOCK:
        if os.path.exists(PENDING_OPS_ASSIGNS_FILE):
            with open(PENDING_OPS_ASSIGNS_FILE) as f:
                return json.load(f)
    return {}


def save_pending_ops_assign(msg_id, item):
    with PENDING_OPS_ASSIGNS_LOCK:
        data = {}
        if os.path.exists(PENDING_OPS_ASSIGNS_FILE):
            with open(PENDING_OPS_ASSIGNS_FILE) as f:
                data = json.load(f)
        data[str(msg_id)] = item
        with open(PENDING_OPS_ASSIGNS_FILE, 'w') as f:
            json.dump(data, f, indent=2)


def remove_pending_ops_assign(msg_id):
    with PENDING_OPS_ASSIGNS_LOCK:
        if not os.path.exists(PENDING_OPS_ASSIGNS_FILE):
            return
        with open(PENDING_OPS_ASSIGNS_FILE) as f:
            data = json.load(f)
        data.pop(str(msg_id), None)
        with open(PENDING_OPS_ASSIGNS_FILE, 'w') as f:
            json.dump(data, f, indent=2)


async def retract_pending_assign_cards(ticket_id='', folder_id=''):
    """Pull the unclaimed assign card for a batch the dashboard just archived.

    The assign card sits in #assignments with a live editor dropdown and
    nothing ever retracted it. So a batch cancelled on the site left its picker
    sitting there looking exactly like work waiting for an editor — and picking
    one put them on a batch that was already pulled. Kai Gangi cancelled his
    Invo batch on 2026-08-14 at 22:51 and it was assigned to Naomi off that
    stale card six hours later; Henry's empty monid twin went the same way on
    08-18.

    The site refuses those assignments now (it answers the click with
    skipped:"archived" and we swap the embed in cc_assign_request) — but only
    if somebody clicks. Until then the channel still reads as live work. This
    takes the card down when the archive arrives instead of waiting for a
    wasted click.

    Matches on ticket_id (website batches) or folder_id (drive folders); an
    entry carrying neither is left alone rather than guessed at. Best-effort
    throughout — a card we can't edit is logged, never raised, because the
    archive itself must still go through.
    """
    ticket_id = str(ticket_id or '').strip()
    folder_id = str(folder_id or '').strip()
    if not ticket_id and not folder_id:
        return
    for msg_id, item in list(load_pending_ops_assigns().items()):
        item_ticket = str(item.get('ticket_id') or '').strip()
        item_folder = str(item.get('folder_id') or '').strip()
        matched = (ticket_id and item_ticket == ticket_id) or \
                  (folder_id and item_folder == folder_id)
        if not matched:
            continue
        try:
            cid = int(item.get('channel_id') or ASSIGNMENTS_CHANNEL_ID or 0)
            ch  = bot.get_channel(cid) or await bot.fetch_channel(cid)
            msg = await ch.fetch_message(int(msg_id))
            gone = discord.Embed(
                title='⚠️ Pulled from the queue',
                description='This batch was cancelled on the dashboard — nothing to assign.',
                color=discord.Color.dark_grey())
            gone.add_field(name='Creator', value=creator_label(item), inline=True)
            gone.add_field(name='Batch', value=item.get('folder_name') or '—', inline=True)
            if item.get('ticket_url'):
                gone.add_field(name='Where', value=f"[Open in the dashboard]({item['ticket_url']})", inline=False)
            await msg.edit(embed=gone, view=None)
        except discord.NotFound:
            # Already deleted — the pending entry below is all that's left.
            pass
        except Exception as e:
            # Leave the entry in place so a retry of this archive can try again.
            logger.warning(f'archive: could not retract assign card {msg_id}: {e}')
            continue
        remove_pending_ops_assign(msg_id)
        logger.info(
            f'archive: retracted assign card {msg_id} '
            f'(ticket={ticket_id or "-"} folder={folder_id or "-"})'
        )


def fetch_pending_website_batches():
    """Website-native batches (ticket_id, no folder_id) still sitting unclaimed
    in the #assignments channel. pending_ops_assigns.json entries go stale the
    same way Drive-folder ones do — assigned or delivered on the site directly
    without the Discord dropdown ever being clicked leaves an orphan entry
    here — so cross-check dashboard_batches.json (updated on every assign/
    deliver) and drop anything that's no longer actually pending. Returns a
    list of {msg_id, channel_id, ticket_id, folder_name, client_name,
    student_name, video_count, ...} sorted oldest first."""
    pending = load_pending_ops_assigns()
    batches = load_dashboard_batches()
    out = []
    for msg_id, item in pending.items():
        ticket_id = item.get('ticket_id')
        if not ticket_id:
            continue  # Drive-folder entry, not a website batch
        if ticket_id in batches:
            continue  # already assigned/delivered on the site — stale orphan
        out.append({**item, 'msg_id': msg_id})
    # Discord snowflake embeds the creation timestamp — no separate 'timestamp'
    # field is saved for these entries, so derive age from the message ID.
    out.sort(key=lambda x: int(x['msg_id']))
    return out


def fetch_dashboard_assignable_batches(editor_discord_id='', editor_name=''):
    """Live website-native batches (no Drive folder, no Notion row) the
    dashboard will let /reassign hand to a different editor. Optionally
    scoped to one editor — id first, name only as fallback, same
    disambiguation rule the inbound bridge already uses (several creators/
    editors share a first name). Returns [] and logs on any failure —
    a dashboard outage must never take /reassign down, Notion rows still
    work without this."""
    config = load_config()
    secret = config.get('dashboard_secret')
    url = config.get('dashboard_assignable_url')
    if not url or not secret:
        return []
    params = {}
    if editor_discord_id:
        params['editor_discord_id'] = editor_discord_id
    elif editor_name:
        params['editor'] = editor_name
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'Bearer {secret}'},
            params=params,
            timeout=10,
        )
    except Exception as e:
        logger.warning(f'fetch_dashboard_assignable_batches: request failed: {e}')
        return []
    if not resp.ok:
        logger.warning(f'fetch_dashboard_assignable_batches: {resp.status_code} {resp.text[:200]}')
        return []
    try:
        data = resp.json()
    except Exception as e:
        logger.warning(f'fetch_dashboard_assignable_batches: bad JSON: {e}')
        return []

    out = []
    for b in data.get('batches', []):
        out.append({
            'ticket_id':         b.get('ticket_id', ''),
            'client_name':       b.get('creator_name') or b.get('client_name') or '',
            'folder_name':       f"{WEBSITE_BATCH_PREFIX}{b.get('label') or b.get('client_name') or 'Untitled'}",
            'editor_name':       b.get('editor_name', ''),
            'editor_discord_id': b.get('editor_discord_id', ''),
            'video_count':       b.get('video_count') or 0,
            'folder_id':         '',
            'notion_page_id':    '',
            'is_revision':       False,
            'source':            'website',
        })
    return out


def post_dashboard_ticket_reassign(ticket_id, editor_name, editor_discord_id):
    """POST a website-batch reassign to the dashboard's editing-assign
    endpoint (ticket_id takes precedence over folder_id there) — runs the
    same path the dashboard's own UI uses: writes the assignment, logs the
    event, queues the outbox ping, notifies. Unlike the best-effort Drive-side
    pushes (_dashboard_post/_queue_dashboard_push), this feeds straight back
    into the /reassign UI, so it's one synchronous call with the result
    surfaced to whoever ran the command rather than parked and retried.
    Returns (ok, error_message)."""
    config = load_config()
    secret = config.get('dashboard_secret')
    url = config.get('dashboard_url')
    if not url or not secret:
        return False, 'Dashboard bridge not configured (dashboard_url/dashboard_secret missing).'
    payload = {
        'ticket_id':         ticket_id,
        'editor_name':       editor_name,
        'editor_discord_id': editor_discord_id or '',
        'is_reassign':       True,
    }
    try:
        resp = requests.post(
            url,
            headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=15,
        )
    except Exception as e:
        logger.error(f'post_dashboard_ticket_reassign: request failed: {e}')
        return False, 'Could not reach the dashboard. Try again.'
    if resp.ok:
        return True, ''
    logger.error(f'post_dashboard_ticket_reassign: {resp.status_code} {resp.text[:200]}')
    return False, f'Dashboard rejected the reassign ({resp.status_code}).'


def fetch_stale_website_assign_messages():
    """The mirror image of fetch_pending_website_batches(): entries whose
    ticket_id already shows assigned/delivered in dashboard_batches.json but
    whose #assignments dropdown message was never cleaned up (the site-side
    assign happened without the Discord button being clicked). Used to tidy
    the channel so it doesn't show phantom open assignments."""
    pending = load_pending_ops_assigns()
    batches = load_dashboard_batches()
    out = []
    for msg_id, item in pending.items():
        ticket_id = item.get('ticket_id')
        if not ticket_id or ticket_id not in batches:
            continue
        out.append({**item, 'msg_id': msg_id, 'resolved': batches[ticket_id]})
    return out


# ── Deadline helpers ───────────────────────────────────────────────────────────

def load_deadlines():
    if os.path.exists(DEADLINES_FILE):
        try:
            with open(DEADLINES_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_deadlines(data):
    with _DEADLINES_LOCK:
        with open(DEADLINES_FILE, 'w') as f:
            json.dump(data, f, indent=2)


def update_deadline_editor(folder_id, notion_page_id, new_editor):
    """Repoints a deadlines.json entry's editor_name to new_editor on reassign.
    Keyed by folder_id when available; if folder_id is missing/blank (e.g. Drive Link
    didn't parse), falls back to matching by notion_page_id so the deadline checker
    doesn't keep pinging the old editor for a folder it can no longer find by ID."""
    deadlines = load_deadlines()
    key = folder_id if (folder_id and folder_id in deadlines) else None
    if key is None and notion_page_id:
        for fid, d in deadlines.items():
            if d.get('notion_page_id') == notion_page_id:
                key = fid
                break
    if key is None:
        key = folder_id or notion_page_id
        if not key:
            return
    entry = deadlines.get(key, {})
    entry['editor_name'] = new_editor
    due_ts = entry.get('due_ts')
    if not entry.get('indefinite') and due_ts and (due_ts - time.time()) > 6 * 3600:
        entry['warned_6h'] = False
    deadlines[key] = entry
    save_deadlines(deadlines)


def pop_deadline_entry(folder_id, notion_page_id):
    """Removes a deadlines.json entry on folder removal/archival, same key-fallback lookup as
    update_deadline_editor(), so archived folders stop showing up as perpetually-overdue."""
    deadlines = load_deadlines()
    key = folder_id if (folder_id and folder_id in deadlines) else None
    if key is None and notion_page_id:
        for fid, d in deadlines.items():
            if d.get('notion_page_id') == notion_page_id:
                key = fid
                break
    if key is not None and key in deadlines:
        del deadlines[key]
        save_deadlines(deadlines)


def load_delivery_meta():
    """delivery_meta.json: Active Queue notion_page_id -> {assigned_at, turnaround_hours, was_overdue}.
    Survives the Delivered status because Active Queue rows aren't deleted on delivery, so /info can
    always look up turnaround/overdue info by the same page_id whether a folder is in progress or done."""
    if os.path.exists(DELIVERY_META_FILE):
        try:
            with open(DELIVERY_META_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_delivery_meta_entry(notion_page_id, data):
    if not notion_page_id:
        return
    with _DELIVERY_META_LOCK:
        meta = {}
        if os.path.exists(DELIVERY_META_FILE):
            try:
                with open(DELIVERY_META_FILE) as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        meta[notion_page_id] = data
        with open(DELIVERY_META_FILE, 'w') as f:
            json.dump(meta, f, indent=2)


def load_editor_counters():
    if os.path.exists(EDITOR_COUNTERS_FILE):
        try:
            with open(EDITOR_COUNTERS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_editor_counters(data):
    with _EDITOR_COUNTERS_LOCK:
        with open(EDITOR_COUNTERS_FILE, 'w') as f:
            json.dump(data, f, indent=2)


def increment_editor_counter(editor_name, field):
    """Increment a per-editor counter ('revisions', 'missed_deadlines',
    'slow_pickups_4h', 'slow_pickups_12h')."""
    if not editor_name:
        return
    counters = load_editor_counters()
    if editor_name not in counters:
        counters[editor_name] = {'revisions': 0, 'missed_deadlines': 0}
    counters[editor_name][field] = counters[editor_name].get(field, 0) + 1
    save_editor_counters(counters)
    logger.info(f'editor_counter: {editor_name} {field} → {counters[editor_name][field]}')


def get_editor_counters(editor_name):
    """Returns {revisions, missed_deadlines, slow_pickups_4h, slow_pickups_12h}
    for an editor, defaulting to 0."""
    data = load_editor_counters().get(editor_name, {})
    return {
        'revisions':        data.get('revisions', 0),
        'missed_deadlines': data.get('missed_deadlines', 0),
        'slow_pickups_4h':  data.get('slow_pickups_4h', 0),
        'slow_pickups_12h': data.get('slow_pickups_12h', 0),
    }


def get_project_number(folder_id):
    """Returns '#N' for the given folder_id, or '' if not found."""
    if not folder_id:
        return ''
    try:
        with _PROJECT_NUMBERS_LOCK:
            if not os.path.exists(PROJECT_NUMBERS_FILE):
                return ''
            with open(PROJECT_NUMBERS_FILE) as f:
                data = json.load(f)
        n = data.get(folder_id)
        return f'#{n}' if n else ''
    except Exception:
        return ''


def folder_link(folder_name, folder_id='', drive_link=''):
    """Wraps a folder name in a Drive-hyperlink markdown for embed display, falling back
    to the plain name when neither a folder_id nor a drive_link is available."""
    url = drive_link or (f'https://drive.google.com/drive/folders/{folder_id}' if folder_id else '')
    return f'[{folder_name}]({url})' if url else folder_name


def add_lines_fields(embed, name, lines, max_field=1000, embed_budget=5400):
    """Adds bullet lines to an embed as one or more fields — continuation fields
    get a zero-width name — so long lists span multiple fields instead of being
    cut off at Discord's 1024-char per-field cap. Lines are never chopped
    mid-markdown-link (a truncated link renders as broken text). Falls back to
    an '… +N more' tail only when the 6000-char total-embed cap is near
    (embed_budget leaves headroom for fields added after this one)."""
    if not lines:
        embed.add_field(name=name, value='None', inline=False)
        return
    first = True

    def flush(value_lines):
        nonlocal first
        embed.add_field(name=name if first else '\u200b', value='\n'.join(value_lines), inline=False)
        first = False

    def has_room(value_lines):
        header = len(name) if first else 1
        return (len(embed) + header + len('\n'.join(value_lines)) <= embed_budget
                and len(embed.fields) < 24)

    cur, used = [], 0
    for i, line in enumerate(lines):
        if len(line) > max_field:
            line = line[:max_field - 1] + '…'
        if cur and used + len(line) + 1 > max_field:
            if not has_room(cur):
                flush([f'*… +{len(cur) + len(lines) - i} more*'])
                return
            flush(cur)
            cur, used = [], 0
        cur.append(line)
        used += len(line) + 1
    if has_room(cur):
        flush(cur)
    else:
        flush([f'*… +{len(cur)} more*'])


AUTO_DELETE_SECS = 120  # /stats, /editorstats, /leaderboard self-clean after this — keeps channels from filling up with stale snapshots


async def _auto_delete_later(message, delay=AUTO_DELETE_SECS):
    """Deletes a sent message after `delay` seconds. Silently no-ops if it's
    already gone or the interaction token expired (ephemeral webhook messages
    only stay deletable for ~15 min, well past our 2-minute window)."""
    if message is None:
        return
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass


def format_deadline(folder_id):
    """Returns a human-readable deadline string for display in /stats."""
    if not folder_id:
        return None
    d = load_deadlines().get(folder_id)
    if not d:
        return None
    if d.get('pending_start'):
        assigned_at = d.get('assigned_at')
        if assigned_at:
            h = int((time.time() - assigned_at) // 3600)
            return f'⏸️ Not started ({h}h since assigned)'
        return '⏸️ Not started'
    if d.get('indefinite') or not d.get('due_ts'):
        return '♾️ Indefinite'
    remaining = d['due_ts'] - time.time()
    if remaining <= 0:
        return '⛔ Overdue'
    h = int(remaining // 3600)
    m = int((remaining % 3600) // 60)
    label = f'{h}h {m}m left'
    return f'⚠️ {label}' if h < 6 else f'⏰ {label}'


# ── ▶️ Start (pickup) state ─────────────────────────────────────────────────────
# New assignments enter a "pending start" state: no due_ts until the editor
# presses ▶️ Start, at which point due_ts = started_at + 24h. Entries without
# the pending_start flag (assigned before this feature) behave exactly as before.

PICKUP_NAG_1_SECS  = 4 * 3600    # gentle nudge in editor channel
PICKUP_NAG_2_SECS  = 8 * 3600    # stronger nudge
PICKUP_OPS_SECS    = 12 * 3600   # ops channel ping, then every 12h after
START_DEADLINE_SECS = 86400      # editing window once started


def reset_start_state(entry):
    """Puts a deadlines entry into the pending-start state (fresh assignment or
    reassignment). Preserves 'indefinite' — an explicitly no-deadline folder stays
    that way after Start, but pickup tracking still applies."""
    entry['pending_start']          = True
    entry['started_at']             = None
    entry['due_ts']                 = None
    entry['pickup_nag_level']       = 0
    entry['last_pickup_ops_ts']     = 0
    entry['slow_pickup_4h_logged']  = False
    entry['slow_pickup_12h_logged'] = False
    entry['footage_flagged']        = False
    entry['warned_6h']              = False
    entry['escalated_12h']          = False
    entry['missed_deadline_logged'] = False
    entry.pop('last_vex_escalation_ts', None)
    return entry


def mark_folder_started(folder_id, backfill=False):
    """Stamps started_at and starts the 24h clock for a pending-start folder.
    With backfill=True (editor ran /complete without ever starting), started_at
    is set to assigned_at so pickup/edit stats stay honest and there is no
    incentive to skip the Start button. Returns the updated entry, or None if
    the folder wasn't in the pending-start state (idempotent)."""
    deadlines = load_deadlines()
    entry = deadlines.get(folder_id)
    if not entry or not entry.get('pending_start'):
        return None
    now = time.time()
    started_at = (entry.get('assigned_at') or now) if backfill else now
    entry['pending_start'] = False
    entry['started_at']    = started_at
    entry['due_ts']        = None if entry.get('indefinite') else started_at + START_DEADLINE_SECS
    entry['warned_6h']     = False
    deadlines[folder_id]   = entry
    save_deadlines(deadlines)
    logger.info(f'mark_folder_started: {folder_id} started_at={started_at} backfill={backfill}')
    return entry


def assignment_jump_link(folder_name, folder_id='', drive_link=''):
    """Wraps a folder name in a link to its assignment message (which carries the
    Drive links, deadline, and Start button). Falls back to the Drive link when no
    assignment message is on record. Editor/Team-scoped views only — jump links
    into a private editor channel don't work for people who can't see it."""
    if folder_id:
        rec = load_assignment_messages().get(folder_id, {})
        msg_id, ch_id = rec.get('message_id'), rec.get('channel_id')
        if msg_id and ch_id:
            try:
                guild_id = int(load_config()['discord_guild_id'])
                return f'[{folder_name}](https://discord.com/channels/{guild_id}/{ch_id}/{msg_id})'
            except Exception:
                pass
    return folder_link(folder_name, folder_id, drive_link)


def _editor_on_shift_now(editor_name):
    """True if the editor is currently inside a scheduled shift block, per
    schedule_cache.json. Editors with no schedule data for today are treated as
    always available (wall-clock nagging). Used to hold pickup nags so they don't
    fire mid-sleep; any parse failure errs toward True (nag rather than never nag)."""
    try:
        with open(os.path.join(BASE_DIR, 'schedule_cache.json')) as f:
            cache = json.load(f)
        sched = cache.get('editors', {}).get(editor_name)
        if not sched:
            return True
        offset    = _parse_utc_offset(sched.get('timezone', ''))
        local_now = datetime.now(timezone.utc) + timedelta(hours=offset)
        blocks    = (sched.get(local_now.strftime('%A')) or '').strip()
        if not blocks:
            return True
        now_min = local_now.hour * 60 + local_now.minute
        for block in blocks.split('|'):
            if '-' not in block:
                continue
            start_s, end_s = block.strip().split('-', 1)
            sh, sm = map(int, start_s.strip().split(':'))
            eh, em = map(int, end_s.strip().split(':'))
            start_min, end_min = sh * 60 + sm, eh * 60 + em
            if start_min <= end_min:
                if start_min <= now_min <= end_min:
                    return True
            elif now_min >= start_min or now_min <= end_min:  # overnight block
                return True
        return False
    except Exception as e:
        logger.warning(f'_editor_on_shift_now: {editor_name}: {e}')
        return True


def average_pickup_hours(editor_name):
    """Average pickup time (assigned → started) across delivery_meta.json entries
    for this editor, or None when no data exists yet."""
    vals = [
        m['pickup_hours'] for m in load_delivery_meta().values()
        if m.get('editor_name') == editor_name and m.get('pickup_hours') is not None
    ]
    return round(sum(vals) / len(vals), 1) if vals else None


# ── Drive helpers ───────────────────────────────────────────────────────────────

def _drive_escape(name):
    return name.replace('\\', '\\\\').replace("'", "\\'")


def _find_child_folder_id(service, name, parent_id=None):
    """Matches by stripped name — an exact Drive-side `name=` filter silently
    misses folders with stray leading/trailing whitespace (e.g. 'Chris Lam '
    vs 'Chris Lam'), which caused a real missing-Drive-Links bug (Chris Lam,
    2026-07-31) and a missing-Edited-folder bug (Zi, 2026-06-17). Lists all
    folders under parent_id (or drive-wide if parent_id is None) and compares
    stripped names client-side instead of relying on an exact server match."""
    q = "mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = service.files().list(
        q=q, fields='files(id,name)', pageSize=1000,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    target = name.strip()
    for f in resp.get('files', []):
        if f['name'].strip() == target:
            return f['id']
    return None


def get_drive_service():
    logger.info(f"Loading Drive credentials from: {TOKEN_FILE}")
    creds = Credentials.from_authorized_user_file(
        TOKEN_FILE, [
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/drive.activity.readonly',
        ]
    )
    logger.info(f"Creds expired: {creds.expired}, valid: {creds.valid}")
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            if 'invalid_grant' in str(e).lower():
                logger.error(
                    'Google Drive token refresh failed (invalid_grant) — '
                    'token has been revoked or expired. Re-authenticate by running: '
                    'python register_watch.py (in the bot directory)'
                )
            raise
    return build('drive', 'v3', credentials=creds)


def get_folder_drive_meta(folder_id):
    """Returns (createdTime, name) from the Drive folder's own metadata for
    dashboard mirroring — createdTime is ISO 8601 UTC (e.g.
    '2026-07-01T12:34:56.789Z'), name is the folder's real Drive name (which
    can carry a take number the Notion title dropped, e.g. 'Quizlet 17' vs
    Notion's 'Quizlet'). Either or both come back None if the lookup fails —
    callers must omit the corresponding payload key rather than fake a value."""
    if not folder_id:
        return None, None
    try:
        service = get_drive_service()
        meta = service.files().get(
            fileId=folder_id, fields='createdTime,name', supportsAllDrives=True
        ).execute()
        return meta.get('createdTime'), meta.get('name')
    except Exception as e:
        logger.warning(f'get_folder_drive_meta({folder_id}) failed: {e}')
        return None, None


def _count_videos_recursive(service, folder_id):
    """Recursively count video files under folder_id."""
    total = 0
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(name, mimeType, id)',
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        for f in resp.get('files', []):
            if f['mimeType'] == 'application/vnd.google-apps.folder':
                total += _count_videos_recursive(service, f['id'])
            elif (os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS
                  or f.get('mimeType', '').startswith('video/')):
                total += 1
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return total


def count_all_edited_videos(any_folder_id):
    """Walk up from any_folder_id to find Edited/ sibling, then count all video files in it."""
    try:
        service = get_drive_service()
        current_id = any_folder_id
        for _ in range(3):
            meta = service.files().get(fileId=current_id, fields='parents', supportsAllDrives=True).execute()
            parents = meta.get('parents', [])
            if not parents:
                break
            parent_id = parents[0]
            edited_id = _find_child_folder_id(service, 'Edited', parent_id)
            if edited_id:
                return _count_videos_recursive(service, edited_id)
            current_id = parent_id
        return 0
    except Exception as e:
        logger.error(f'Drive error counting all edited videos: {e}')
        return 0


def find_client_edited_folder_id(client_name):
    """
    Returns the Drive ID of the client's top-level Edited/ folder, or '' on failure.
    Primary: searches within the cached client root folder for a child named 'Edited'.
    Fallback: walks up from any Active Queue Drive Link until it finds 'Edited' sibling.
    Results are cached in _client_edited_folder_cache keyed by client_name.
    """
    if client_name in _client_edited_folder_cache:
        return _client_edited_folder_cache[client_name]

    try:
        service = get_drive_service()

        # Primary path: use already-resolved client root folder from cache
        client_root_id = _client_root_folder_cache.get(client_name)
        if client_root_id:
            edited_id = _find_child_folder_id(service, 'Edited', client_root_id)
            if edited_id:
                _client_edited_folder_cache[client_name] = edited_id
                return edited_id

        # Fallback: find any Drive folder_id for this client from Notion, then walk up
        config = load_config()
        token  = config['notion_token']
        resp   = requests.post(
            f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query',
            headers=notion_headers(token),
            json={'filter': {'property': 'Creator', 'rich_text': {'equals': client_name}}, 'page_size': 10},
            timeout=15,
        )
        any_folder_id = ''
        if resp.ok:
            for page in resp.json().get('results', []):
                props      = page['properties']
                drive_link = props.get('Drive Link', {}).get('url') or ''
                m          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
                if m:
                    any_folder_id = m.group(1)
                    break

        if not any_folder_id:
            _client_edited_folder_cache[client_name] = ''
            return ''

        current_id = any_folder_id
        for _ in range(4):
            meta    = service.files().get(fileId=current_id, fields='parents', supportsAllDrives=True).execute()
            parents = meta.get('parents', [])
            if not parents:
                break
            parent_id = parents[0]
            edited_id = _find_child_folder_id(service, 'Edited', parent_id)
            if edited_id:
                _client_edited_folder_cache[client_name] = edited_id
                return edited_id
            current_id = parent_id

        _client_edited_folder_cache[client_name] = ''
        return ''

    except Exception as e:
        logger.error(f'Drive error finding Edited folder for {client_name}: {e}')
        return ''


def get_client_root_folder_id(notes_folder_id, client_name=None):
    """
    Find the client root folder. If client_name is given, uses the top-down approach
    (root_folder_name → client_name) which works on Shared Drives.
    Falls back to walking up two levels from notes_folder_id via parents.
    Returns client root folder ID string, or None on failure.
    """
    try:
        service = get_drive_service()
        if client_name:
            client_root_id, _ = _find_edited_folder_top_down(service, client_name)
            if client_root_id:
                return client_root_id
        # Fallback: walk up via parents
        meta1 = service.files().get(fileId=notes_folder_id, fields='parents', supportsAllDrives=True).execute()
        parents1 = meta1.get('parents', [])
        if not parents1:
            return None
        raw_footage_id = parents1[0]
        meta2 = service.files().get(fileId=raw_footage_id, fields='parents', supportsAllDrives=True).execute()
        parents2 = meta2.get('parents', [])
        if not parents2:
            return None
        return parents2[0]
    except Exception as e:
        logger.error(f'Drive API error resolving client root folder: {e}')
        return None


def _find_edited_folder_top_down(service, client_name):
    """
    Top-down search: root_folder_name → client_name → Edited/.
    Returns (client_root_id, edited_folder_id) or (None, None) on failure.
    Populates _client_root_folder_cache[client_name] on success.
    """
    try:
        cfg = load_config()
        root_name = cfg.get('root_folder_name', '')
        if not root_name:
            return None, None
        root_id = _find_child_folder_id(service, root_name)
        if not root_id:
            logger.warning(f"_find_edited_folder_top_down: root folder '{root_name}' not found")
            return None, None

        client_root_id = _find_child_folder_id(service, client_name, root_id)
        if not client_root_id:
            logger.warning(f"_find_edited_folder_top_down: client folder '{client_name}' not found under root")
            return None, None
        _client_root_folder_cache[client_name] = client_root_id

        edited_id = _find_child_folder_id(service, 'Edited', client_root_id)
        if not edited_id:
            logger.warning(f"_find_edited_folder_top_down: 'Edited' folder not found under client '{client_name}'")
            return client_root_id, None
        return client_root_id, edited_id
    except Exception as e:
        logger.error(f"_find_edited_folder_top_down error for client '{client_name}': {e}")
        return None, None


EDITED_VIDEO_SCAN_MAX_DEPTH = 5


def _find_videos_recursive(service, folder_id, depth=0):
    """Lists every video file under folder_id, descending into subfolders to
    EDITED_VIDEO_SCAN_MAX_DEPTH. Editors' review submissions are frequently
    organized into subfolders (by format, by date, by batch) rather than a
    flat drop — a direct-children-only listing silently undercounts those,
    which used to surface as false count-mismatch/not-found review flags."""
    all_files = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id, name, mimeType)',
            pageToken=page_token,
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        all_files.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break

    # Match by extension OR mimeType — editors sometimes upload videos with
    # extension-less names (e.g. Chris's '1', '2'), which Drive still types video/mp4.
    video_names = [
        f['name'] for f in all_files
        if os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS
        or f.get('mimeType', '').startswith('video/')
    ]

    if depth < EDITED_VIDEO_SCAN_MAX_DEPTH:
        for f in all_files:
            if f.get('mimeType') == 'application/vnd.google-apps.folder':
                video_names.extend(_find_videos_recursive(service, f['id'], depth + 1))

    return video_names


def find_edited_folder_videos(raw_folder_id, edited_folder_name, client_name=None):
    """
    Find edited_folder_name inside the client's Edited/ folder and count video files.
    Primary: top-down search (root → client → Edited/).
    Fallback: walk up from raw_folder_id via parents.
    Returns (count, filenames, fuzzy_note, subfolder_id) or (None, [], None, None) if not found.
    fuzzy_note is a warning string when a fuzzy match was used, else None.
    subfolder_id is the Drive ID of the matched subfolder inside Edited/.
    """
    logger.info(f"=== FIND EDITED DEBUG === client={client_name} searching_for='{edited_folder_name}'")
    try:
        service = get_drive_service()
        label = client_name or raw_folder_id
        logger.info(
            f"find_edited_folder_videos: client='{label}', "
            f"raw_folder_id='{raw_folder_id}', target='{edited_folder_name}'"
        )

        # Primary: top-down search so we don't depend on files.get(parents) which fails on Shared Drives
        edited_folder_id = None
        if client_name:
            _, edited_folder_id = _find_edited_folder_top_down(service, client_name)
            if edited_folder_id:
                logger.info(f"  [top-down] Found Edited/ folder id='{edited_folder_id}'")

        # Fallback: walk up via parents
        if not edited_folder_id:
            logger.info(f"  [fallback] Walking up from raw_folder_id='{raw_folder_id}'")
            current_id = raw_folder_id
            for depth in range(3):
                meta = service.files().get(fileId=current_id, fields='parents', supportsAllDrives=True).execute()
                parents = meta.get('parents', [])
                logger.info(f"  [depth={depth}] current_id='{current_id}' parents={parents}")
                if not parents:
                    break
                parent_id = parents[0]
                edited_folder_id = _find_child_folder_id(service, 'Edited', parent_id)
                if edited_folder_id:
                    logger.info(f"  [depth={depth}] Found Edited/ via walk-up: '{edited_folder_id}'")
                    break
                current_id = parent_id

        if not edited_folder_id:
            logger.info(f"find_edited_folder_videos: Edited/ not found for client='{label}'")
            return None, [], None, None

        # List subfolders of Edited/ and find the matching one
        resp2 = service.files().list(
            q=f"'{edited_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields='files(id, name)',
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        subfolders = resp2.get('files', [])
        subfolder_names = [f['name'] for f in subfolders]
        logger.info(f"Drive Edited/ subfolders found: {subfolder_names}")
        logger.info(f"  Searching for: '{edited_folder_name}'")

        matched = None
        fuzzy_note = None

        # Step 1: exact case-insensitive match
        for f in subfolders:
            if f['name'].lower() == edited_folder_name.lower():
                matched = f
                logger.info(f"Match result: FOUND (exact) '{f['name']}'")
                break

        # Step 2: strip whitespace then compare
        if not matched:
            for f in subfolders:
                if f['name'].strip().lower() == edited_folder_name.strip().lower():
                    matched = f
                    fuzzy_note = f"⚠️ Fuzzy matched (stripped spaces): editor typed '{edited_folder_name}' → found '{f['name']}'"
                    logger.info(f"Match result: FOUND (stripped) '{f['name']}'")
                    break

        # Step 3: substring containment check
        if not matched:
            inp = edited_folder_name.strip().lower()
            for f in subfolders:
                drive = f['name'].strip().lower()
                if drive in inp or inp in drive:
                    matched = f
                    fuzzy_note = f"⚠️ Fuzzy matched: editor typed '{edited_folder_name}' → found '{f['name']}'"
                    logger.info(f"Match result: FOUND (fuzzy) '{f['name']}'")
                    break

        # Step 4: not found at top level — some clients group submissions under a
        # parent folder (e.g. 'Phrasly ' containing 'Phrasly vid 11'), so look one
        # level deeper inside each top-level group folder before giving up.
        if not matched:
            inp = edited_folder_name.strip().lower()
            for group in subfolders:
                try:
                    resp_nested = service.files().list(
                        q=f"'{group['id']}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
                        fields='files(id, name)',
                        pageSize=100,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    ).execute()
                except Exception:
                    continue
                for nf in resp_nested.get('files', []):
                    drive = nf['name'].strip().lower()
                    if drive == inp:
                        matched = nf
                        logger.info(f"Match result: FOUND (nested under '{group['name']}') '{nf['name']}'")
                        break
                    if drive in inp or inp in drive:
                        matched = nf
                        fuzzy_note = (
                            f"⚠️ Fuzzy matched (nested under '{group['name']}'): "
                            f"editor typed '{edited_folder_name}' → found '{nf['name']}'"
                        )
                        logger.info(f"Match result: FOUND (nested fuzzy under '{group['name']}') '{nf['name']}'")
                        break
                if matched:
                    break

        if not matched:
            logger.info(f"Match result: NOT FOUND. Available folder names: {subfolder_names}")
            return None, [], None, None

        target_id = matched['id']
        logger.info(f"  Matched subfolder: '{matched['name']}' id='{target_id}'")

        video_names = _find_videos_recursive(service, target_id)
        logger.info(f"  Found {len(video_names)} video(s) in matched subfolder (recursive)")
        return len(video_names), video_names, fuzzy_note, target_id

    except Exception as e:
        logger.error(f'Drive error finding edited folder: {e}', exc_info=True)
        return None, [], None, None


def build_drive_links_field(client_root_id=None, raw_folder_id=None, edited_subfolder_id=None):
    """
    Builds an embed-ready 'Drive Links' string with clickable Client/Raw Footage/Edited
    folder links. Omits any link whose ID wasn't resolved — e.g. if no Edited subfolder
    was found, only Client Folder and Raw Footage Folder are included.
    Returns '' if nothing is available.
    """
    parts = []
    if client_root_id:
        parts.append(f"📂 [Client Folder](https://drive.google.com/drive/folders/{client_root_id})")
    if raw_folder_id:
        parts.append(f"📁 [Raw Footage Folder](https://drive.google.com/drive/folders/{raw_folder_id})")
    if edited_subfolder_id:
        parts.append(f"🎬 [Edited Folder](https://drive.google.com/drive/folders/{edited_subfolder_id})")
    return '\n'.join(parts)


# ── Pending reviews (shared with notion_bridge.py via file) ────────────────────

def save_pending_review(review_id, data):
    try:
        with PENDING_REVIEW_LOCK:
            reviews = {}
            if os.path.exists(PENDING_REVIEWS_FILE):
                with open(PENDING_REVIEWS_FILE) as f:
                    reviews = json.load(f)
            reviews[review_id] = data
            with open(PENDING_REVIEWS_FILE, 'w') as f:
                json.dump(reviews, f, indent=2)
    except Exception as e:
        logger.error(f'Failed to save pending review: {e}')


def load_pending_reviews():
    try:
        if os.path.exists(PENDING_REVIEWS_FILE):
            with PENDING_REVIEW_LOCK:
                with open(PENDING_REVIEWS_FILE) as f:
                    return json.load(f)
    except Exception as e:
        logger.error(f'Failed to load pending reviews: {e}')
    return {}


# ── Assignment message persistence ────────────────────────────────────────────

def save_assignment_message(folder_id, data):
    try:
        with ASSIGNMENT_MESSAGES_LOCK:
            messages = {}
            if os.path.exists(ASSIGNMENT_MESSAGES_FILE):
                with open(ASSIGNMENT_MESSAGES_FILE) as f:
                    messages = json.load(f)
            messages[folder_id] = data
            with open(ASSIGNMENT_MESSAGES_FILE, 'w') as f:
                json.dump(messages, f, indent=2)
    except Exception as e:
        logger.error(f'Failed to save assignment message: {e}')


def load_assignment_messages():
    try:
        if os.path.exists(ASSIGNMENT_MESSAGES_FILE):
            with ASSIGNMENT_MESSAGES_LOCK:
                with open(ASSIGNMENT_MESSAGES_FILE) as f:
                    return json.load(f)
    except Exception as e:
        logger.error(f'Failed to load assignment messages: {e}')
    return {}


# ── Finalize delivery ──────────────────────────────────────────────────────────

async def finalize_delivery(msg_id, confirmed_count, a, edited_folder, edited_subfolder_id=None):
    editor_name = a.get('editor_name', 'Unknown')
    logger.info(f"finalize_delivery() called for {editor_name}, count={confirmed_count}, folder={a.get('folder_name')}")
    config = load_config()
    token = config['notion_token']
    notion_page_id = a.get('notion_queue_page_id') or a.get('notion_page_id')
    editor_page_id = a.get('editor_page_id')
    now_edt   = datetime.now(EDT)
    today_str = now_edt.strftime('%Y-%m-%d')

    drive_link = ''
    turnaround_days = 0
    if notion_page_id:
        page       = _notion_get(token, notion_page_id)
        page_props = page.get('properties', {})
        submitted_prop  = page_props.get('Submitted', {}).get('date') or {}
        submitted_start = submitted_prop.get('start')
        if submitted_start:
            try:
                # Submitted may be a bare date (legacy rows) or a full timestamp —
                # fromisoformat handles both, unlike a strict '%Y-%m-%d' strptime.
                submitted_date  = datetime.fromisoformat(submitted_start.replace('Z', '+00:00')).date()
                turnaround_days = (now_edt.date() - submitted_date).days
            except Exception:
                pass
        drive_link = page_props.get('Drive Link', {}).get('url') or ''

        patch_props = {
            'Status':             {'select':    {'name': 'Delivered'}},
            'Videos Completed':   {'number':    confirmed_count},
            'Edited Folder Name': {'rich_text': [{'text': {'content': edited_folder}}]},
            'Delivered':          {'date': {'start': today_str}},
        }
        _notion_patch(token, notion_page_id, patch_props)

        # Move the dashboard ticket in step.
        if a.get('folder_id'):
            edited_link = (
                f'https://drive.google.com/drive/folders/{edited_subfolder_id}'
                if edited_subfolder_id else ''
            )
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: post_dashboard_status(
                    a.get('folder_id', ''), 'delivered',
                    video_count=confirmed_count,
                    edited_folder_link=edited_link,
                    editor_name=editor_name,
                    note=f'edited folder: {edited_folder}' if edited_folder else '',
                )
            )

    # Capture turnaround/overdue before clearing the deadline entry — this is the only
    # point where assigned_at/due_ts are still available.
    folder_id_for_dl = a.get('folder_id', '')
    if folder_id_for_dl:
        deadlines  = load_deadlines()
        dl_entry   = deadlines.get(folder_id_for_dl, {})
        assigned_at = dl_entry.get('assigned_at')
        due_ts      = dl_entry.get('due_ts')
        # Auto-start backfill: delivered without ever pressing ▶️ Start — treat
        # started_at as assigned_at so stats count the full window (no incentive
        # to skip the button) and pickup/edit numbers are never null.
        started_at = dl_entry.get('started_at')
        if dl_entry and not started_at:
            started_at = assigned_at
        turnaround_hours = round((time.time() - assigned_at) / 3600, 1) if assigned_at else None
        pickup_hours = round((started_at - assigned_at) / 3600, 1) if (started_at and assigned_at) else None
        edit_hours   = round((time.time() - started_at) / 3600, 1) if started_at else None
        was_overdue = (
            (time.time() > due_ts) if (due_ts and not dl_entry.get('indefinite')) else None
        )
        if notion_page_id and (turnaround_hours is not None or was_overdue is not None):
            save_delivery_meta_entry(notion_page_id, {
                'assigned_at':      assigned_at,
                'started_at':       started_at,
                'turnaround_hours': turnaround_hours,
                'pickup_hours':     pickup_hours,
                'edit_hours':       edit_hours,
                'was_overdue':      was_overdue,
                'editor_name':      editor_name,
                'recorded_at':      time.time(),
            })
        deadlines.pop(folder_id_for_dl, None)
        save_deadlines(deadlines)

    if editor_page_id:
        if a.get('is_revision'):
            logger.info(f"finalize_delivery: revision re-delivery for {editor_name} — skipping stat increment")
            recalculate_active_videos(token, editor_name)
        else:
            page  = _notion_get(token, editor_page_id)
            if not page:
                logger.error(f"finalize_delivery: _notion_get returned empty for editor_page_id={editor_page_id} ({editor_name})")
            props = page.get('properties', {})
            week  = props.get('Delivered This Week',    {}).get('number') or 0
            month = props.get('Delivered This Month',   {}).get('number') or 0
            total = props.get('Total Videos Delivered', {}).get('number') or 0
            new_week  = week  + confirmed_count
            new_month = month + confirmed_count
            new_total = total + confirmed_count
            logger.info(f"Before update — {editor_name} This Week: {week}, This Month: {month}")
            patch_resp = _notion_patch(token, editor_page_id, {
                'Delivered This Week':    {'number': new_week},
                'Delivered This Month':   {'number': new_month},
                'Total Videos Delivered': {'number': new_total},
            })
            if patch_resp.ok:
                logger.info(f"After update — {editor_name} This Week: {new_week}, This Month: {new_month}")
            else:
                logger.error(f"finalize_delivery: Editor Profiles PATCH failed for {editor_name}: {patch_resp.status_code} {patch_resp.text}")
            recalculate_active_videos(token, editor_name)
    else:
        logger.warning(f"finalize_delivery: no editor_page_id for {editor_name}, skipping Editor Profiles update")

    create_delivery_history_row(
        token,
        a['folder_name'],
        a['client_name'],
        a['editor_name'],
        confirmed_count,
        today_str,
        edited_folder,
        drive_link,
    )

    # Build the edited folder Drive link (used in both the completion embed and creator notify)
    edited_folder_id_for_link = edited_subfolder_id or ''
    if not edited_folder_id_for_link:
        _link_loop = asyncio.get_event_loop()
        edited_folder_id_for_link = await _link_loop.run_in_executor(
            None, find_client_edited_folder_id, a['client_name']
        )
    edited_folder_drive_link = (
        f'https://drive.google.com/drive/folders/{edited_folder_id_for_link}'
        if edited_folder_id_for_link else None
    )

    # Resolve the original assignment message — prefer explicit msg_id, fall back to file lookup
    edit_msg_id = msg_id
    if not edit_msg_id:
        folder_id = a.get('folder_id', '')
        if folder_id:
            edit_msg_id = load_assignment_messages().get(folder_id, {}).get('message_id')

    ch_id = a.get('channel_id')
    if edit_msg_id and ch_id:
        try:
            ch    = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            orig  = await ch.fetch_message(edit_msg_id)
            embed = discord.Embed(title='✅ Completed', color=discord.Color.green())
            embed.add_field(name='Client',    value=a['client_name'],   inline=False)
            embed.add_field(name='Folder',    value=a['folder_name'],   inline=False)
            embed.add_field(name='Videos',    value=str(confirmed_count), inline=False)
            embed.add_field(name='Delivered', value=today_str,          inline=False)
            links = []
            client_root_id_for_link = _client_root_folder_cache.get(a['client_name'])
            if client_root_id_for_link:
                links.append(f"[Client Folder](https://drive.google.com/drive/folders/{client_root_id_for_link})")
            if drive_link:
                links.append(f'[Raw Footage Folder]({drive_link})')
            if edited_folder_drive_link:
                links.append(f'[Edited Folder]({edited_folder_drive_link})')
            if links:
                embed.add_field(name='Drive', value=' · '.join(links), inline=False)
            await orig.edit(embed=embed, view=None)
        except Exception as e:
            logger.error(f'Failed to edit assignment message: {e}', exc_info=True)

    send_discord_ops_channel(embed={
        'title': '🎬 Delivery',
        'color': 0x2ecc71,
        'fields': [
            {'name': 'Editor',    'value': a['editor_name'],        'inline': True},
            {'name': 'Client',    'value': a['client_name'],        'inline': True},
            {'name': 'Folder',    'value': a['folder_name'],        'inline': True},
            {'name': 'Videos',    'value': str(confirmed_count),    'inline': True},
            {'name': 'Delivered', 'value': to_ist(now_edt),        'inline': True},
        ],
        'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    })

    # Client root folder link — fall back to here when the Edited folder isn't found
    client_root_id_for_link = _client_root_folder_cache.get(a['client_name'])
    client_root_drive_link = (
        f'https://drive.google.com/drive/folders/{client_root_id_for_link}'
        if client_root_id_for_link else None
    )

    try:
        payload = {
            'type':                     'creator_complete_notify',
            'client_name':              a['client_name'],
            'folder_name':              a['folder_name'],
            'editor_name':              a['editor_name'],
            'confirmed_count':          confirmed_count,
            'edited_folder':            edited_folder,
            'edited_folder_id':         edited_subfolder_id or '',
            'edited_folder_drive_link': edited_folder_drive_link,
            'raw_footage_drive_link':   drive_link or None,
            'client_folder_drive_link': client_root_drive_link,
        }
        logger.info(f"creator_complete_notify payload: {payload}")
        _enqueue_item(payload)
    except Exception as e:
        logger.error(f'Failed to enqueue completion notify: {e}', exc_info=True)

    logger.info(f"Finalized: {a['folder_name']} — {confirmed_count} videos by {a['editor_name']}")


# ── Discord finalize handler (called from queue when notion_bridge finalizes) ───

async def handle_discord_finalize(item):
    msg_id          = item.get('discord_message_id')
    ch_id           = item.get('discord_channel_id')
    confirmed_count = item.get('confirmed_count')
    raw_folder_link = item.get('drive_link')
    edited_folder_link = item.get('edited_folder_drive_link')
    if not (msg_id and ch_id and confirmed_count is not None):
        return
    try:
        ch   = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
        orig = await ch.fetch_message(msg_id)
        a    = pending_assignments.get(msg_id)
        if a:
            embed = assignment_embed(
                a['client_name'], a['folder_name'], a['video_count'],
                f'✅ Completed: {confirmed_count} videos delivered',
                raw_folder_link=raw_folder_link, edited_folder_link=edited_folder_link,
            )
            await orig.edit(embed=embed, view=None)
            a['status'] = 'delivered'
        else:
            fallback_embed = discord.Embed(title='✅ Completed', description=f'{confirmed_count} videos delivered', color=discord.Color.green())
            await orig.edit(content=None, embed=fallback_embed, view=None)
    except Exception as e:
        logger.error(f'Failed to edit finalized Discord message: {e}')


# ── Queue write helper (discord_bot → its own queue) ──────────────────────────

def _enqueue_item(item):
    with QUEUE_LOCK:
        queue = []
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f:
                queue = json.load(f)
        queue.append(item)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(queue, f, indent=2)


# ── Announce / note to editors ────────────────────────────────────────────────

async def handle_announce(item):
    """Send a short note from Vex to one or all editor Discord channels."""
    message = item.get('message', '').strip()
    targets = item.get('targets', [])  # empty list = all editors

    if not message:
        return

    editors = await asyncio.get_event_loop().run_in_executor(None, fetch_editors_from_notion)
    send_to = {k: v for k, v in editors.items() if not targets or k in targets}

    embed = discord.Embed(title='📢 Note from Vex', description=message, color=discord.Color.blurple())
    for editor_name, info in send_to.items():
        ch_id = info.get('discord_channel_id', '')
        if not ch_id:
            logger.warning(f'handle_announce: no Discord channel for {editor_name}')
            continue
        try:
            ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
            await ch.send(embed=embed)
            logger.info(f'Announce sent to {editor_name}')
        except Exception as e:
            logger.error(f'handle_announce: failed to send to {editor_name}: {e}')


# ── Creator channel notification ──────────────────────────────────────────────

async def handle_creator_detected(item):
    """Pings the creator's Discord channel when a new folder is first detected."""
    client_name = item.get('client_name', '')
    folder_name = item.get('folder_name', '')
    video_count = item.get('video_count', 0)
    folder_id   = item.get('folder_id', '')
    pnum        = get_project_number(folder_id)

    loop = asyncio.get_event_loop()
    channel_id_str, user_id_str = await loop.run_in_executor(None, fetch_creator_discord_info, client_name)
    if not channel_id_str:
        logger.warning(f'handle_creator_detected: no Discord channel for creator {client_name}')
        return
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        logger.error(f'handle_creator_detected: bad channel ID for {client_name}: {channel_id_str}')
        return

    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f'handle_creator_detected: cannot reach channel {channel_id}: {e}')
            return

    mention = f'<@{user_id_str}>' if user_id_str else None
    embed = discord.Embed(title='📥 New Footage Received', color=discord.Color.blurple())
    embed.add_field(name='Folder', value=folder_name, inline=False)
    embed.add_field(name='Videos Detected', value=f'{video_count} *(count may change while files finish uploading)*', inline=False)
    embed.add_field(name='Status', value='⏳ Being reviewed for assignment now', inline=False)
    if pnum:
        embed.add_field(name='Project', value=pnum, inline=False)
    await ch.send(content=mention, embed=embed)
    logger.info(f'creator_detected sent to {client_name} (channel {channel_id}): {folder_name}')


async def handle_creator_notify(item):
    """Sends assignment notification to the creator's Discord channel."""
    client_name = item.get('client_name', '')
    folder_name = item.get('folder_name', '')
    editor_name = item.get('editor_name', '')
    video_count = item.get('video_count', 0)
    folder_id   = item.get('folder_id', '')
    pnum        = item.get('project_number') or get_project_number(folder_id)

    loop = asyncio.get_event_loop()
    channel_id_str = await loop.run_in_executor(None, fetch_creator_discord_channel, client_name)
    if not channel_id_str:
        logger.warning(f'No Discord channel found for creator: {client_name}')
        return
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        logger.error(f'Bad Discord Channel ID for creator {client_name}: {channel_id_str}')
        return

    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f'Cannot reach creator channel {channel_id}: {e}')
            return

    embed = discord.Embed(title='📁 New Folder Assigned', color=discord.Color.blue())
    embed.add_field(name='Folder', value=folder_name, inline=False)
    embed.add_field(name='Videos', value=str(video_count), inline=False)
    embed.add_field(name='Editor', value=editor_name, inline=False)
    embed.add_field(name='Status', value='In Progress ⏳', inline=False)
    if pnum:
        embed.add_field(name='Project', value=pnum, inline=False)
    await ch.send(embed=embed)
    logger.info(f'Creator notify sent to {client_name} (channel {channel_id}): {folder_name}')


async def handle_creator_complete_notify(item):
    """Sends delivery completion notification to the creator's Discord channel."""
    client_name     = item.get('client_name', '')
    folder_name     = item.get('folder_name', '')
    editor_name     = item.get('editor_name', '')
    confirmed_count = item.get('confirmed_count', 0)
    edited_folder   = item.get('edited_folder', '')

    dedup_key = (client_name, folder_name.strip())
    now_ts = time.time()
    last_sent = _creator_complete_notified.get(dedup_key, 0)
    if now_ts - last_sent < _CREATOR_NOTIFY_DEDUP_TTL:
        logger.warning(f'handle_creator_complete_notify: dedup suppressed duplicate for {client_name}/{folder_name}')
        return
    _creator_complete_notified[dedup_key] = now_ts

    loop = asyncio.get_event_loop()
    channel_id_str, user_id_str = await loop.run_in_executor(None, fetch_creator_discord_info, client_name)
    logger.info(f"Creator channel for {client_name}: {channel_id_str}, user_id: {user_id_str}")
    if not channel_id_str:
        logger.warning(f'No Discord channel found for creator: {client_name}')
        return
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        logger.error(f'Bad Discord Channel ID for creator {client_name}: {channel_id_str}')
        return

    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f'Cannot reach creator channel {channel_id}: {e}')
            return

    mention      = f"<@{user_id_str}>" if user_id_str else None
    edited_folder_drive_link = item.get('edited_folder_drive_link')
    raw_footage_drive_link   = item.get('raw_footage_drive_link')
    client_folder_drive_link = item.get('client_folder_drive_link')
    folder_id_for_pnum = item.get('edited_folder_id', '') or item.get('folder_id', '')
    pnum = item.get('project_number') or get_project_number(folder_id_for_pnum)
    logger.info(f"Sending creator notify, edited_folder_link={edited_folder_drive_link}")

    links = []
    if edited_folder_drive_link:
        links.append(f'📂 [View Edited Folder]({edited_folder_drive_link})')
    else:
        # No Edited folder detected — link the Client and Raw Footage folders instead
        logger.warning("No edited folder link in creator_complete_notify, falling back to client/raw footage links")
        if client_folder_drive_link:
            links.append(f'📂 [Client Folder]({client_folder_drive_link})')
        if raw_footage_drive_link:
            links.append(f'📁 [Raw Footage Folder]({raw_footage_drive_link})')

    embed = discord.Embed(title=f'✅ {folder_name} Completed', color=discord.Color.green())
    embed.add_field(name='Videos Delivered', value=str(confirmed_count), inline=False)
    embed.add_field(name='Editor', value=editor_name, inline=False)
    if pnum:
        embed.add_field(name='Project', value=pnum, inline=False)
    if links:
        embed.add_field(name='Drive Links', value='\n'.join(links), inline=False)
    await ch.send(content=mention, embed=embed)
    logger.info(f'Creator complete notify sent to {client_name} (channel {channel_id}): {folder_name}')


async def handle_reassign_notify(item):
    """Notifies creator + old editor when a folder is reassigned."""
    client_name = item.get('client_name', '')
    folder_name = item.get('folder_name', '')
    old_editor  = item.get('old_editor', '')
    new_editor  = item.get('new_editor', '')
    loop        = asyncio.get_event_loop()

    # ── Notify creator ─────────────────────────────────────────────────────
    if client_name:
        channel_id_str, user_id_str = await loop.run_in_executor(None, fetch_creator_discord_info, client_name)
        if channel_id_str:
            try:
                ch      = bot.get_channel(int(channel_id_str)) or await bot.fetch_channel(int(channel_id_str))
                mention = f'<@{user_id_str}>' if user_id_str else None
                embed = discord.Embed(title=f'🔁 {folder_name} Reassigned', color=discord.Color.orange())
                embed.add_field(name='New Editor', value=new_editor, inline=False)
                embed.add_field(name='Status', value='Your folder is still being worked on!', inline=False)
                await ch.send(content=mention, embed=embed)
                logger.info(f'handle_reassign_notify: creator notified — {client_name}/{folder_name} → {new_editor}')
            except Exception as e:
                logger.error(f'handle_reassign_notify: creator notify failed for {client_name}: {e}')

    # ── Notify old editor ──────────────────────────────────────────────────
    if old_editor and old_editor != new_editor:
        editors_map = await loop.run_in_executor(None, fetch_editors_from_notion)
        old_info    = editors_map.get(old_editor, {})
        ch_id_str   = old_info.get('discord_channel_id', '')
        user_id     = old_info.get('discord_user_id', '')
        if ch_id_str:
            try:
                ch      = bot.get_channel(int(ch_id_str)) or await bot.fetch_channel(int(ch_id_str))
                mention = f'<@{user_id}>' if user_id else None
                embed = discord.Embed(title='📢 Folder Reassigned', color=discord.Color.orange())
                embed.add_field(name='Folder', value=f'{client_name} / {folder_name}', inline=False)
                embed.add_field(name='Reassigned To', value=new_editor, inline=False)
                await ch.send(content=mention, embed=embed)
                logger.info(f'handle_reassign_notify: old editor notified — {old_editor}')
            except Exception as e:
                logger.error(f'handle_reassign_notify: old editor notify failed for {old_editor}: {e}')


# ── In-memory state ────────────────────────────────────────────────────────────

# message_id (int) → assignment dict
pending_assignments: dict[int, dict] = {}

# client_name → Drive ID of their Edited/ folder (populated on first notify)
_client_edited_folder_cache: dict[str, str] = {}

# client_name → Drive ID of the client root folder (two levels above assignment folder)
_client_root_folder_cache: dict[str, str] = {}

# Dedup guard: (client_name, folder_name) → epoch seconds of last creator-complete notify sent.
# Prevents the same folder being notified twice if both Discord /complete and Telegram review fire.
_creator_complete_notified: dict[tuple, float] = {}
_CREATOR_NOTIFY_DEDUP_TTL = 120  # seconds

# client_name → Drive ID of the client's Raw Footage folder
_client_raw_footage_folder_cache: dict[str, str] = {}

# Leaderboard auto-post tracking (reset each startup; loop prevents double-posting)
_leaderboard_last_weekly_post: date | None  = None
_leaderboard_last_monthly_post: tuple | None = None  # (year, month)


# ── Embed builder ──────────────────────────────────────────────────────────────

def assignment_embed(client_name, folder_name, video_count, status=None,
                      raw_folder_link=None, edited_folder_link=None):
    if status is None:
        color, title = discord.Color.blue(), '📁 New Assignment'
    elif 'Completed' in str(status):
        color, title = discord.Color.green(), '📁 Assignment'
    elif 'Declined' in str(status):
        color, title = discord.Color.red(), '📁 Assignment'
    else:
        color, title = discord.Color.yellow(), '📁 Assignment'

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name='Client', value=client_name, inline=False)
    embed.add_field(name='Folder', value=folder_name, inline=False)
    embed.add_field(name='Videos', value=str(video_count), inline=False)
    if status:
        embed.add_field(name='Status', value=str(status), inline=False)
    links = []
    if raw_folder_link:
        links.append(f'[Raw Footage Folder]({raw_folder_link})')
    if edited_folder_link:
        links.append(f'[Edited Folder]({edited_folder_link})')
    if links:
        embed.add_field(name='Drive', value=' · '.join(links), inline=False)
    return embed


# ── Completion modal ───────────────────────────────────────────────────────────

class OpenCompleteModalView(discord.ui.View):
    """Button that opens the CompleteModal — needed when the command had to defer() first."""
    def __init__(self, assignment):
        super().__init__(timeout=120)
        self._assignment = assignment

    @discord.ui.button(label='Enter Details', style=discord.ButtonStyle.primary, emoji='✅')
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CompleteModal(self._assignment))


class CompleteModal(discord.ui.Modal, title='Mark Assignment Complete'):
    confirm_assignment = discord.ui.TextInput(
        label='Confirm: Client / Folder (do not edit)',
        required=False,
    )
    videos_done = discord.ui.TextInput(
        label='Videos Completed',
        placeholder='Enter number',
        min_length=1,
        max_length=6,
    )
    edited_folder = discord.ui.TextInput(
        label='Edited Folder Name',
        placeholder="Folder name in client's Edited/ folder",
        min_length=1,
        max_length=200,
    )

    def __init__(self, assignment: dict):
        super().__init__()
        self._assignment = assignment
        # Surface which assignment this modal is bound to, so an editor who
        # misclicked the wrong client/folder in the dropdown can catch it
        # before typing — the modal gave no such confirmation previously,
        # which let a wrong-client selection silently attach a correct
        # "edited folder" answer to the wrong Notion page (Jill/Henry mixup,
        # 2026-07-23).
        confirm_text = f"{assignment.get('client_name', '')} / {assignment.get('folder_name', '')}"
        self.confirm_assignment.default = confirm_text[:100]
        self.title = f"Complete: {confirm_text}"[:45]

    async def on_submit(self, interaction: discord.Interaction):
        logger.info(
            f"CompleteModal submitted by user={interaction.user} "
            f"videos_raw='{self.videos_done.value}' folder_raw='{self.edited_folder.value}'"
        )
        try:
            videos_done = int(self.videos_done.value.strip())
        except ValueError:
            logger.warning(f"CompleteModal: non-integer videos value '{self.videos_done.value}'")
            await interaction.response.send_message('Please enter a number for Videos Completed.', ephemeral=True)
            return

        edited_folder = self.edited_folder.value.strip()
        a             = self._assignment

        await interaction.response.defer(ephemeral=True)

        client_name    = a['client_name']
        folder_name    = a['folder_name']
        folder_id      = a['folder_id']
        editor_name    = a['editor_name']
        notion_page_id = a.get('notion_queue_page_id')

        # /complete on a never-started folder: backfill started_at = assigned_at
        # right away so pickup nags stop while the review is pending and stats
        # count the full window (skipping ▶️ Start gains nothing).
        if folder_id:
            mark_folder_started(folder_id, backfill=True)

        logger.info(
            f"CompleteModal parsed: editor={editor_name} client={client_name} "
            f"folder={folder_name} folder_id={folder_id} "
            f"videos_done={videos_done} edited_folder='{edited_folder}' "
            f"notion_page_id={notion_page_id}"
        )

        # ── Duplicate-submission guard ─────────────────────────────────────────
        # A folder that already has a pending review, or is already Delivered,
        # must not be completed again — a double /complete double-counts stats
        # and creates a duplicate Delivery History row (hit for real 2026-07-02).
        for rd in load_pending_reviews().values():
            if rd.get('status') != 'pending':
                continue
            if (notion_page_id and rd.get('notion_page_id') == notion_page_id) or \
               (folder_id and rd.get('folder_id') == folder_id):
                await interaction.followup.send(
                    f'⚠️ **{folder_name}** is already submitted and waiting for manager review — '
                    'no need to submit again. Vex will approve it shortly.',
                    ephemeral=True,
                )
                logger.warning(f'CompleteModal: duplicate submit blocked (pending review) — {client_name}/{folder_name} by {editor_name}')
                return

        drive_link = ''
        current_status = ''
        if notion_page_id:
            _cfg = load_config()
            _page = _notion_get(_cfg['notion_token'], notion_page_id)
            drive_link = _page.get('properties', {}).get('Drive Link', {}).get('url') or ''
            current_status = (_page.get('properties', {}).get('Status', {}).get('select') or {}).get('name', '')

        if current_status == 'Delivered':
            await interaction.followup.send(
                f'⚠️ **{folder_name}** is already marked Delivered — no need to submit again. '
                'If something changed, contact Vex.',
                ephemeral=True,
            )
            logger.warning(f'CompleteModal: duplicate submit blocked (already Delivered) — {client_name}/{folder_name} by {editor_name}')
            return

        original_drive_link = drive_link

        folder_name_match = edited_folder.lower() == folder_name.lower()
        folder_name_fuzzy_match = (
            not folder_name_match
            and edited_folder.lower().replace(' ', '') == folder_name.lower().replace(' ', '')
        )

        loop = asyncio.get_event_loop()
        drive_count, drive_files, fuzzy_note, edited_subfolder_id = await loop.run_in_executor(
            None, find_edited_folder_videos, folder_id, edited_folder, client_name
        )

        # Resolve client root folder — find_edited_folder_videos may have already cached it
        client_root_id = _client_root_folder_cache.get(client_name)
        if not client_root_id:
            client_root_id = await loop.run_in_executor(
                None, get_client_root_folder_id, folder_id, client_name
            )
            if client_root_id:
                _client_root_folder_cache[client_name] = client_root_id
            else:
                logger.warning(f'Could not resolve client root folder for client={client_name}')

        flags = []
        if not folder_name_match and not folder_name_fuzzy_match:
            flags.append(
                f"⚠️ Folder name mismatch: editor said '{edited_folder}' but assigned folder was '{folder_name}'"
            )
            # Wrong-folder check: does the typed name match one of the editor's OTHER
            # active assignments? (Kaye once typed 'Personal brand 3' against a mathgpt
            # assignment — one video ended up counted as two deliveries.)
            try:
                others = await loop.run_in_executor(None, fetch_in_progress_for_editor, editor_name)
                typed_norm = edited_folder.lower().replace(' ', '')
                for o in others:
                    if o.get('notion_queue_page_id') == notion_page_id:
                        continue
                    other_norm = o['folder_name'].lower().replace(' ', '')
                    if typed_norm == other_norm or typed_norm in other_norm or other_norm in typed_norm:
                        flags.append(
                            f"🚨 Possible wrong folder: '{edited_folder}' looks like the editor's other assignment "
                            f"'{o['folder_name']}' ({o['client_name']}) — check which folder was actually completed"
                        )
                        break
            except Exception as _wf_e:
                logger.warning(f'wrong-folder check failed: {_wf_e}')
        # fuzzy_note is informational — a fuzzy match still counts as found, so no review flag
        is_revision = a.get('is_revision', False)
        if is_revision:
            # Revisions: Drive folder also holds previously-approved videos, so drive_count
            # will legitimately exceed videos_done. Only flag if Drive has fewer than reported.
            if drive_count is not None and drive_count < videos_done:
                flags.append(
                    f"⚠️ Count mismatch: editor said {videos_done} but Drive Edited folder has {drive_count} videos"
                )
        elif drive_count is not None and drive_count != videos_done:
            flags.append(
                f"⚠️ Count mismatch: editor said {videos_done} but Drive Edited folder has {drive_count} videos"
            )
        if drive_count is None:
            # Only flag when all match attempts (exact, stripped, fuzzy) truly failed
            flags.append(
                f"⚠️ Edited folder '{edited_folder}' not found in client's Edited/ folder on Drive"
            )

        drive_count_str = str(drive_count) if drive_count is not None else 'not found'
        fuzzy_line      = f" ({fuzzy_note})" if fuzzy_note else ''

        tg_links = []
        if client_root_id:
            client_root_link = f'https://drive.google.com/drive/folders/{client_root_id}'
            tg_links.append(f'<a href="{client_root_link}">Client Folder</a>')
        raw_footage_link_tg = original_drive_link or (
            f'https://drive.google.com/drive/folders/{folder_id}' if folder_id else ''
        )
        if raw_footage_link_tg:
            tg_links.append(f'<a href="{raw_footage_link_tg}">Raw Footage</a>')
        if edited_subfolder_id:
            edited_folder_link = f'https://drive.google.com/drive/folders/{edited_subfolder_id}'
            tg_links.append(f'<a href="{edited_folder_link}">Edited Folder</a>')
        drive_link_line = ' · '.join(tg_links)

        pnum_review     = a.get('project_number') or get_project_number(folder_id or '')
        pnum_str        = f" <b>{pnum_review}</b>" if pnum_review else ''
        completion_line = f"✅ <b>{editor_name}</b> completed{pnum_str} <b>{client_name} / {folder_name}</b> — {videos_done} videos"
        detail_line     = f"Edited folder: <b>{edited_folder}</b> · Drive count: <b>{drive_count_str}</b>{fuzzy_line}"

        if flags:
            flags_block = '\n'.join(flags)
            status_line = "⚠️ Needs your review — tap 🔍 Review to confirm or adjust"
            tg_msg = f"{flags_block}\n\n{completion_line}\n{status_line}\n\n{detail_line}"
        else:
            status_line = "Auto-confirmed · counts match · Notion updated"
            tg_msg = f"{completion_line}\n{status_line}\n\n{detail_line}"

        if drive_link_line:
            tg_msg += f"\n{drive_link_line}"

        if flags:
            review_id = str(uuid.uuid4())
            review_data = {
                'review_id':          review_id,
                'discord_message_id': a.get('discord_message_id'),
                'discord_channel_id': a.get('channel_id'),
                'editor_name':        editor_name,
                'client_name':        client_name,
                'folder_name':        folder_name,
                'folder_id':          folder_id,
                'videos_done':        videos_done,
                'drive_count':        drive_count,
                'edited_folder':      edited_folder,
                'editor_page_id':     a.get('editor_page_id'),
                'notion_page_id':     notion_page_id,
                'flags':              flags,
                'created_at':         datetime.now(timezone.utc).isoformat(),
                'status':             'pending',
                'client_root_id':     client_root_id,
                'edited_subfolder_id': edited_subfolder_id,
            }
            save_pending_review(review_id, review_data)
            keyboard = {
                'inline_keyboard': [[
                    {'text': '🔍 Review', 'callback_data': f'review:{review_id}'}
                ]]
            }
            send_notion_bridge_telegram(tg_msg, keyboard)
            # Also post to the dedicated review channel (separate from the completion
            # channel so flagged items don't get lost scrolling through clean deliveries)
            # with a Discord decision view (approve / flag discrepancy back to editor).
            if REVIEW_CHANNEL_ID:
                try:
                    assign_ch = bot.get_channel(REVIEW_CHANNEL_ID) or await bot.fetch_channel(REVIEW_CHANNEL_ID)
                    flag_embed = discord.Embed(
                        title='⚠️ Completion Needs Review',
                        description='\n'.join(flags),
                        color=discord.Color.orange(),
                    )
                    flag_embed.add_field(name='Editor',  value=editor_name,  inline=True)
                    flag_embed.add_field(name='Client',  value=client_name,  inline=True)
                    flag_embed.add_field(name='Folder',  value=folder_name,  inline=True)
                    flag_embed.add_field(name='Videos',  value=str(videos_done), inline=True)
                    flag_embed.add_field(name='Edited Folder', value=edited_folder, inline=True)
                    drive_links = build_drive_links_field(client_root_id, folder_id, edited_subfolder_id)
                    if drive_links:
                        flag_embed.add_field(name='Drive Links', value=drive_links, inline=False)
                    ping = f'<@{VEX_USER_ID}> ' if VEX_USER_ID else ''
                    review_sent = await assign_ch.send(
                        content=f'{ping}Review required for **{editor_name}** · {client_name}/{folder_name}',
                        embed=flag_embed,
                        view=DiscordReviewView(review_data),
                    )
                    # Save the review message's own ID so on_ready can re-register the
                    # Approve/Discrepancy buttons on this exact message after a bot restart —
                    # without this, an old review's buttons silently do nothing post-restart.
                    review_data['review_message_id'] = review_sent.id
                    review_data['review_channel_id']  = REVIEW_CHANNEL_ID
                    save_pending_review(review_id, review_data)
                except Exception as _e:
                    logger.error(f'CompleteModal: failed to post review to review channel: {_e}')
            try:
                await interaction.edit_original_response(content='⚠️ Submitted for manager review.')
            except discord.NotFound:
                await interaction.followup.send('⚠️ Submitted for manager review.', ephemeral=True)
        else:
            send_notion_bridge_telegram(tg_msg)
            await finalize_delivery(a.get('discord_message_id'), videos_done, a, edited_folder, edited_subfolder_id)
            # Notify completion channel of clean delivery
            if COMPLETION_CHANNEL_ID:
                try:
                    assign_ch = bot.get_channel(COMPLETION_CHANNEL_ID) or await bot.fetch_channel(COMPLETION_CHANNEL_ID)
                    done_embed = discord.Embed(
                        title='✅ Delivery Confirmed',
                        color=discord.Color.green(),
                    )
                    done_embed.add_field(name='Editor',  value=editor_name,  inline=True)
                    done_embed.add_field(name='Client',  value=client_name,  inline=True)
                    done_embed.add_field(name='Folder',  value=folder_name,  inline=True)
                    done_embed.add_field(name='Videos',  value=str(videos_done), inline=True)
                    drive_links = build_drive_links_field(client_root_id, folder_id, edited_subfolder_id)
                    if drive_links:
                        done_embed.add_field(name='Drive Links', value=drive_links, inline=False)
                    await assign_ch.send(
                        content=f'**{editor_name}** completed **{client_name}/{folder_name}** — {videos_done} video{"s" if videos_done != 1 else ""}',
                        embed=done_embed,
                    )
                except Exception as _e:
                    logger.error(f'CompleteModal: failed to post completion to completion channel: {_e}')
            try:
                await interaction.edit_original_response(content='✅ Delivery confirmed!')
            except discord.NotFound:
                await interaction.followup.send('✅ Delivery confirmed!', ephemeral=True)

        logger.info(f"Completion submitted: {folder_name} — {videos_done} videos by {editor_name}")


# ── /info — folder dossier (Drive links + status + revisions + turnaround) ─────

async def _build_dossier_embed(notion_page_id):
    """Builds the full /info embed for one Active Queue page: Drive links, status,
    revisions, and turnaround/overdue (live for in-progress, recorded for delivered)."""
    loop  = asyncio.get_event_loop()
    token = load_config()['notion_token']
    page  = await loop.run_in_executor(None, _notion_get, token, notion_page_id)
    if not page:
        return discord.Embed(title='⚠️ Folder not found', description='This Notion page may have been archived.', color=discord.Color.red())

    props          = page.get('properties', {})
    title_rt       = props.get('Video', {}).get('title', [])
    folder_name    = title_rt[0].get('plain_text', '') if title_rt else '(unknown)'
    creator_rt     = props.get('Creator', {}).get('rich_text', [])
    client_name    = creator_rt[0].get('plain_text', '') if creator_rt else ''
    status_sel     = props.get('Status', {}).get('select') or {}
    status         = status_sel.get('name', '')
    editor_sel     = props.get('Editor', {}).get('select') or {}
    editor_name    = editor_sel.get('name', '')
    notes_rt       = props.get('Notes', {}).get('rich_text', [])
    notes          = notes_rt[0].get('plain_text', '') if notes_rt else ''
    videos_done    = props.get('Videos Completed', {}).get('number') or 0
    edited_name_rt = props.get('Edited Folder Name', {}).get('rich_text', [])
    edited_name    = edited_name_rt[0].get('plain_text', '') if edited_name_rt else ''
    drive_link     = props.get('Drive Link', {}).get('url') or ''
    delivered_date = (props.get('Delivered', {}).get('date') or {}).get('start', '')

    m         = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
    folder_id = m.group(1) if m else ''

    client_root_id, edited_subfolder_id = await loop.run_in_executor(
        None, resolve_drive_ids_for_dossier, client_name, folder_id, edited_name
    )
    revisions = await loop.run_in_executor(None, fetch_revisions_for_folder, client_name, folder_name)

    status_emoji = {
        'Raw': '🆕', 'In Progress': '🔧', 'Revision': '🔁', 'Delivered': '✅',
    }.get(status, '❓')
    embed = discord.Embed(title=f'{status_emoji} {folder_name}', color=discord.Color.blurple())
    embed.add_field(name='Client',  value=client_name or '—', inline=True)
    embed.add_field(name='Editor',  value=editor_name or '—', inline=True)
    embed.add_field(name='Status',  value=status or '—',      inline=True)
    embed.add_field(name='Videos',  value=str(videos_done) if videos_done else '—', inline=True)

    dl_entry = load_deadlines().get(folder_id, {})
    meta     = load_delivery_meta().get(notion_page_id, {})
    assigned_at = dl_entry.get('assigned_at') or meta.get('assigned_at')
    if assigned_at:
        assigned_str = datetime.fromtimestamp(assigned_at, tz=EDT).strftime('%b %d, %I:%M %p EDT')
        embed.add_field(name='Assigned', value=assigned_str, inline=True)

    if status == 'Delivered':
        embed.add_field(name='Delivered', value=delivered_date or '—', inline=True)
        if meta.get('turnaround_hours') is not None:
            embed.add_field(name='Turnaround', value=f"{meta['turnaround_hours']}h", inline=True)
        if meta.get('was_overdue') is not None:
            embed.add_field(name='Was Overdue', value=('⚠️ Yes' if meta['was_overdue'] else '✅ No'), inline=True)
    else:
        if assigned_at:
            hours_in = round((time.time() - assigned_at) / 3600, 1)
            embed.add_field(name='In Progress For', value=f'{hours_in}h', inline=True)
        if dl_entry.get('pending_start'):
            waiting_h = round((time.time() - assigned_at) / 3600, 1) if assigned_at else '?'
            embed.add_field(name='Due', value=f'⏸️ Not started yet ({waiting_h}h waiting)', inline=True)
        elif dl_entry.get('indefinite'):
            embed.add_field(name='Due', value='No deadline', inline=True)
        elif dl_entry.get('due_ts'):
            remaining_h = round((dl_entry['due_ts'] - time.time()) / 3600, 1)
            due_str = f'⚠️ Overdue by {abs(remaining_h)}h' if remaining_h < 0 else f'Due in {remaining_h}h'
            embed.add_field(name='Due', value=due_str, inline=True)
            if dl_entry.get('started_at'):
                embed.add_field(
                    name='Started',
                    value=datetime.fromtimestamp(dl_entry['started_at'], tz=EDT).strftime('%b %d, %I:%M %p EDT'),
                    inline=True,
                )

    rev_lines = [f"• {r['date'][:10]}: {r['notes'][:80]}" for r in revisions[:5] if r['notes']]
    embed.add_field(
        name=f'Revisions ({len(revisions)})',
        value=('\n'.join(rev_lines) if rev_lines else ('No notes recorded' if revisions else 'None')),
        inline=False,
    )

    if notes:
        embed.add_field(name='Notes', value=notes[:500], inline=False)

    drive_links = build_drive_links_field(client_root_id, folder_id, edited_subfolder_id)
    if drive_links:
        embed.add_field(name='Drive Links', value=drive_links, inline=False)

    return embed


class InfoFolderSelectView(discord.ui.View):
    """Dropdown of an editor's in-progress + last-10-delivered folders for /info."""
    def __init__(self, rows: list):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=f"{r['client_name']} / {r['folder_name']}"[:100],
                value=r['notion_page_id'][:100],
                description=r['description'][:100],
                emoji=r['emoji'],
            )
            for r in rows[:25]
        ]
        select = discord.ui.Select(placeholder='Pick a folder for Drive links & info…', options=options)

        async def on_select(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            embed = await _build_dossier_embed(select.values[0])
            msg = await interaction.followup.send(embed=embed, ephemeral=True)
            asyncio.create_task(_auto_delete_later(msg))

        select.callback = on_select
        self.add_item(select)


# ── Folder selection for /complete when multiple In Progress folders ────────────

class FolderSelectView(discord.ui.View):
    def __init__(self, rows: list, client_name: str, base_assignment: dict):
        super().__init__(timeout=120)
        rows_by_folder_id = {
            (r['folder_id'] or r['notion_queue_page_id']): r
            for r in rows
        }
        options = [
            discord.SelectOption(
                label=r['folder_name'][:100],
                value=(r['folder_id'] or r['notion_queue_page_id'])[:100],
                description=f"{r['video_count']} videos",
            )
            for r in rows
        ]
        select = discord.ui.Select(
            placeholder=f'Which folder for {client_name}?'[:150],
            options=options,
        )

        async def on_select(interaction: discord.Interaction):
            folder_id = select.values[0]
            row = rows_by_folder_id.get(folder_id)
            if not row:
                await interaction.response.send_message('Folder not found.', ephemeral=True)
                return
            assignment = {**row, **base_assignment}
            await interaction.response.send_modal(CompleteModal(assignment))

        select.callback = on_select
        self.add_item(select)


class ClientSelectView(discord.ui.View):
    def __init__(self, rows: list, base_assignment: dict):
        super().__init__(timeout=120)
        unique_clients = list(dict.fromkeys(r['client_name'] for r in rows))
        options = [
            discord.SelectOption(label=c[:100], value=c[:100])
            for c in unique_clients
        ]
        select = discord.ui.Select(placeholder='Which client?', options=options)

        async def on_select(interaction: discord.Interaction):
            client_name = select.values[0]
            client_rows = [r for r in rows if r['client_name'] == client_name]
            if len(client_rows) == 1:
                assignment = {**client_rows[0], **base_assignment}
                await interaction.response.send_modal(CompleteModal(assignment))
            elif not client_rows:
                await interaction.response.edit_message(
                    content='Could not match that client — your assignments may have changed. Run `/complete` again.',
                    view=None,
                )
            else:
                folder_view = FolderSelectView(client_rows, client_name, base_assignment)
                await interaction.response.edit_message(
                    content=f'Which folder for {client_name}?', view=folder_view
                )

        select.callback = on_select
        self.add_item(select)


class RevisionFolderSelectView(discord.ui.View):
    """Select menu showing delivered folders that a creator can send back for revision."""
    def __init__(self, rows: list, client_name: str):
        super().__init__(timeout=120)
        self._rows_by_id = {r['notion_page_id']: r for r in rows}
        options = [
            discord.SelectOption(
                label=r['folder_name'][:100],
                value=r['notion_page_id'][:100],
                description=f"Editor: {r['editor_name'] or 'unknown'} · {r['video_count']} videos",
            )
            for r in rows
        ]
        select = discord.ui.Select(
            placeholder=f'Which delivered folder needs revision?',
            options=options,
        )

        async def on_select(interaction: discord.Interaction):
            row = self._rows_by_id.get(select.values[0])
            if not row:
                await interaction.response.send_message('Folder not found.', ephemeral=True)
                return
            await interaction.response.send_modal(
                RevisionNotesModal(row, client_name)
            )

        select.callback = on_select
        self.add_item(select)


class RevisionNotesModal(discord.ui.Modal, title='Revision Notes'):
    """Modal for adding revision notes when sending a folder back for changes.
    Works from main guild (Vex/Team) and creator guild.
    """
    notes_input = discord.ui.TextInput(
        label='Describe the issue and what to fix',
        style=discord.TextStyle.paragraph,
        placeholder='e.g. "Intro music is too loud, cut the last 3 seconds of clip 2…"',
        min_length=10,
        max_length=1000,
    )

    def __init__(self, row: dict, client_name: str):
        super().__init__()
        self._row          = row
        self._client_name  = client_name

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        row         = self._row
        folder_name = row['folder_name']
        editor_name = row['editor_name']
        notes_text  = self.notes_input.value
        loop        = asyncio.get_event_loop()

        editors     = await loop.run_in_executor(None, fetch_editors_from_notion)
        editor_info = editors.get(editor_name)
        if not editor_info:
            await interaction.followup.send(
                f'Could not find editor **{editor_name}** in Notion.', ephemeral=True
            )
            return

        await open_revision_assignment(
            client_name=self._client_name,
            folder_name=folder_name,
            folder_id=row.get('folder_id', ''),
            video_count=row['video_count'],
            editor_name=editor_name,
            editor_info=editor_info,
            notion_queue_page_id=row['notion_page_id'],
            notes=notes_text,
        )

        send_discord_ops_channel(embed={
            'title': '🔄 Revision',
            'color': 0xe67e22,
            'fields': [
                {'name': 'Client', 'value': self._client_name, 'inline': True},
                {'name': 'Folder', 'value': folder_name,       'inline': True},
                {'name': 'Editor', 'value': editor_name,       'inline': True},
            ],
            'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        })
        await interaction.followup.send(
            f"✅ **{folder_name}** sent for revision. Notes delivered to **{editor_name}**.",
            ephemeral=True,
        )


class PremiumRevisionSelectView(discord.ui.View):
    """VA picks a 'Review'-status folder to send back for revision with notes."""
    def __init__(self, rows: list, client_name: str, premium_channel_id: str):
        super().__init__(timeout=180)
        self._rows_by_id = {r['notion_page_id']: r for r in rows}
        options = [
            discord.SelectOption(
                label=r['folder_name'][:100],
                value=r['notion_page_id'][:100],
                description=f"Editor: {r['editor_name'] or 'unknown'} · {r['video_count']} videos",
            )
            for r in rows
        ]
        select = discord.ui.Select(
            placeholder='Which folder needs revision?',
            options=options,
        )

        async def on_select(interaction: discord.Interaction):
            row = self._rows_by_id.get(select.values[0])
            if not row:
                await interaction.response.send_message('Folder not found.', ephemeral=True)
                return
            await interaction.response.send_modal(
                RevisionNotesModal(row, client_name, confirm_channel_id=premium_channel_id)
            )

        select.callback = on_select
        self.add_item(select)


class AllApprovedSelectView(discord.ui.View):
    """VA picks a 'Review'-status folder to approve — triggers stat finalization."""
    def __init__(self, rows: list, client_name: str, premium_channel_id: str):
        super().__init__(timeout=180)
        self._rows_by_id     = {r['notion_page_id']: r for r in rows}
        self._client_name    = client_name
        self._premium_ch_id  = premium_channel_id
        options = [
            discord.SelectOption(
                label=r['folder_name'][:100],
                value=r['notion_page_id'][:100],
                description=f"{r['video_count']} videos · Editor: {r['editor_name'] or 'unknown'}",
            )
            for r in rows
        ]
        select = discord.ui.Select(
            placeholder='Which folder is fully approved?',
            options=options,
        )

        async def on_select(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            row = self._rows_by_id.get(select.values[0])
            if not row:
                await interaction.followup.send('Folder not found.', ephemeral=True)
                return
            await _finalize_va_approval(interaction, row, self._client_name, self._premium_ch_id)

        select.callback = on_select
        self.add_item(select)


async def _finalize_va_approval(interaction: discord.Interaction, row: dict,
                                 client_name: str, premium_channel_id: str):
    """Finalizes VA approval: Notion Delivered, editor stats, delivery history, channel notification."""
    folder_name  = row['folder_name']
    editor_name  = row['editor_name']
    video_count  = row['video_count']
    notion_pid   = row['notion_page_id']
    drive_link   = row.get('drive_link', '')
    edited_name  = row.get('edited_folder_name', '')
    loop         = asyncio.get_event_loop()

    config    = load_config()
    token     = config['notion_token']
    today_str = datetime.now(EDT).strftime('%Y-%m-%d')

    # Update Active Queue: Review → Delivered + set Delivered date
    _notion_patch(token, notion_pid, {
        'Status':    {'select': {'name': 'Delivered'}},
        'Delivered': {'date':   {'start': today_str}},
    })

    # Premium batches reach the dashboard only now, once the VA has signed off.
    # The row carries the raw-footage Drive link, and the folder id in it is the
    # dashboard's join key.
    _fid = re.search(r'/folders/([A-Za-z0-9_-]+)', drive_link or '')
    if _fid:
        await loop.run_in_executor(
            None, lambda: post_dashboard_status(
                _fid.group(1), 'delivered',
                video_count=video_count,
                editor_name=editor_name,
                note=f'edited folder: {edited_name}' if edited_name else '',
            )
        )

    # Increment editor stats
    editors     = await loop.run_in_executor(None, fetch_editors_from_notion)
    editor_info = editors.get(editor_name)
    if editor_info:
        editor_page_id = editor_info.get('page_id')
        page  = _notion_get(token, editor_page_id)
        props = page.get('properties', {}) if page else {}
        week  = props.get('Delivered This Week',    {}).get('number') or 0
        month = props.get('Delivered This Month',   {}).get('number') or 0
        total = props.get('Total Videos Delivered', {}).get('number') or 0
        _notion_patch(token, editor_page_id, {
            'Delivered This Week':    {'number': week  + video_count},
            'Delivered This Month':   {'number': month + video_count},
            'Total Videos Delivered': {'number': total + video_count},
        })
        recalculate_active_videos(token, editor_name)
        logger.info(f'_finalize_va_approval: {editor_name} stats +{video_count}')
    else:
        logger.warning(f'_finalize_va_approval: editor {editor_name!r} not found')

    # Create Delivery History row
    create_delivery_history_row(
        token, folder_name, client_name, editor_name,
        video_count, today_str, edited_name, drive_link,
    )

    # Send approval confirmation to premium channel
    try:
        pch = bot.get_channel(int(premium_channel_id)) or await bot.fetch_channel(int(premium_channel_id))
        embed = discord.Embed(title='✅ Approved!', color=discord.Color.green())
        embed.add_field(name='Folder',  value=folder_name,       inline=False)
        embed.add_field(name='Editor',  value=editor_name,       inline=False)
        embed.add_field(name='Videos',  value=str(video_count),  inline=False)
        if drive_link:
            embed.add_field(name='Drive', value=f'[Open Edited Folder]({drive_link})', inline=False)
        await pch.send(embed=embed)
    except Exception as e:
        logger.error(f'_finalize_va_approval: premium channel notify failed: {e}')

    send_discord_ops_channel(embed={
        'title': '✅ VA Approved',
        'color': 0x1abc9c,
        'fields': [
            {'name': 'Client', 'value': client_name,       'inline': True},
            {'name': 'Folder', 'value': folder_name,       'inline': True},
            {'name': 'Editor', 'value': editor_name,       'inline': True},
            {'name': 'Videos', 'value': str(video_count),  'inline': True},
        ],
        'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    })
    await interaction.followup.send(
        f"✅ **{folder_name}** approved! {video_count} videos marked as delivered.",
        ephemeral=True,
    )
    logger.info(f'VA approval finalized: {client_name}/{folder_name} by {editor_name}')


class EditorStatsView(discord.ui.View):
    """View for /editorstats — holds [Show Delivered Today], [Show In Progress], and sort buttons."""

    def __init__(self, embed: discord.Embed, delivered_rows: list, in_progress_rows: list, website_by_editor: dict = None):
        super().__init__(timeout=600)
        self._embed           = embed
        self._delivered       = delivered_rows
        self._in_progress     = in_progress_rows
        self._website_by_editor = website_by_editor or {}
        self._detail_shown    = False
        self._progress_shown  = False
        self.show_in_progress.label = f'⏳ Show In Progress ({len(in_progress_rows)})'

    @discord.ui.button(label='📋 Show Delivered Today', style=discord.ButtonStyle.secondary)
    async def show_delivered(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._detail_shown:
            await interaction.response.defer()
            return
        self._detail_shown = True
        await interaction.response.defer()

        if self._delivered:
            lines = []
            for r in self._delivered:
                name = folder_link(r['folder_name'], drive_link=r.get('drive_link', ''))
                lines.append(
                    f"• {r['client_name']} / {name} — {r['editor_name']} — {r['videos_completed']} videos"
                )
            field_value = '\n'.join(lines)
            if len(field_value) > 1020:
                field_value = field_value[:1020] + '…'
        else:
            field_value = 'Nothing delivered today yet'

        self._embed.add_field(
            name='📋 Delivered Today — Detail',
            value=field_value,
            inline=False,
        )
        button.disabled = True
        button.label    = '📋 Delivered Today (loaded)'
        await interaction.edit_original_response(embed=self._embed, view=self)

    @discord.ui.button(label='⏳ Show In Progress', style=discord.ButtonStyle.secondary)
    async def show_in_progress(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._progress_shown:
            await interaction.response.defer()
            return
        self._progress_shown = True
        await interaction.response.defer()

        if self._in_progress:
            lines = []
            for r in self._in_progress:
                date_str = r['submitted_date'][:10] if r.get('submitted_date') else '?'
                editor   = r['editor_name'] or 'unassigned'
                dl       = format_deadline(r.get('folder_id', ''))
                dl_part  = f' — {dl}' if dl else ''
                name     = folder_link(r['folder_name'], r.get('folder_id', ''))
                lines.append(
                    f"• {r['client_name']} / {name} — {editor} — {r['video_count']} videos — since {date_str}{dl_part}"
                )

            # Split into 1020-char chunks so every folder is shown
            chunks = []
            current = []
            current_len = 0
            for line in lines:
                needed = len(line) + (1 if current else 0)
                if current and current_len + needed > 1020:
                    chunks.append('\n'.join(current))
                    current = [line]
                    current_len = len(line)
                else:
                    current.append(line)
                    current_len += needed
            if current:
                chunks.append('\n'.join(current))

            self._embed.add_field(
                name=f'⏳ In Progress — Detail ({len(lines)} folders)',
                value=chunks[0],
                inline=False,
            )
        else:
            self._embed.add_field(
                name='⏳ In Progress — Detail',
                value='No folders in progress',
                inline=False,
            )
            chunks = []

        button.disabled = True
        button.label    = '⏳ In Progress (loaded)'
        await interaction.edit_original_response(embed=self._embed, view=self)

        # Send overflow chunks as follow-up messages
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @discord.ui.button(label='🔤 Sort by Editor', style=discord.ButtonStyle.primary, row=1)
    async def sort_by_editor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        rows = self._in_progress
        website = self._website_by_editor
        if not rows and not website:
            await interaction.followup.send('No in-progress folders.', ephemeral=True)
            return

        from collections import defaultdict
        grouped: dict = defaultdict(list)
        for r in rows:
            key = r['editor_name'] or 'Unassigned'
            grouped[key].append(r)

        total_folders = len(rows) + sum(len(v) for v in website.values())
        title = f'⏳ In Progress — Sorted by Editor ({total_folders} folders)'
        embeds = [discord.Embed(title=title, color=discord.Color.blurple())]
        total_len = len(title)
        all_editors = sorted(set(grouped.keys()) | set(website.keys()))
        for editor in all_editors:
            folder_rows = grouped.get(editor, [])
            web_batches = website.get(editor, [])
            lines = []
            for r in sorted(folder_rows, key=lambda x: x.get('submitted_date') or ''):
                date_str = (r.get('submitted_date') or '?')[:10]
                dl       = format_deadline(r.get('folder_id', ''))
                dl_part  = f' — {dl}' if dl else ''
                lines.append(
                    f"• {r['client_name']} / {r['folder_name']} — {r['video_count']} vids — since {date_str}{dl_part}"
                )
            for b in web_batches:
                label = creator_label(b)
                lines.append(f"• 🌐 {label} — {b.get('video_count') or 0} vids (website batch)")
            field_val = '\n'.join(lines)
            if len(field_val) > 1020:
                field_val = field_val[:1020] + '…'
            count_part = f'{len(folder_rows)} folders'
            if web_batches:
                count_part += f', {len(web_batches)} website'
            field_name = f'👤 {editor} ({count_part})'

            # Discord caps a whole embed at 6000 chars (title + every field's
            # name+value summed) and 25 fields, independent of the 1024-char
            # per-field cap above — with enough editors/folders that total
            # blows past 6000 and the send 400s. Since this runs inside a view
            # button (not the slash command), that 400 never reaches
            # @tree.error — it's swallowed by discord.py's default view error
            # handler, so the button just silently does nothing. Roll to a new
            # embed before either limit is hit instead of risking that.
            added_len = len(field_name) + len(field_val)
            if len(embeds[-1].fields) >= 25 or total_len + added_len > 5900:
                embeds.append(discord.Embed(title=f'{title} (cont.)', color=discord.Color.blurple()))
                total_len = len(embeds[-1].title)
            embeds[-1].add_field(name=field_name, value=field_val, inline=False)
            total_len += added_len

        for e in embeds:
            await interaction.followup.send(embed=e)

    @discord.ui.button(label='📅 Sort by Date', style=discord.ButtonStyle.primary, row=1)
    async def sort_by_date(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        rows = self._in_progress
        if not rows:
            await interaction.followup.send('No in-progress folders.', ephemeral=True)
            return

        sorted_rows = sorted(rows, key=lambda r: r.get('submitted_date') or '')
        lines = []
        for r in sorted_rows:
            date_str = (r.get('submitted_date') or '?')[:10]
            editor   = r['editor_name'] or 'unassigned'
            dl       = format_deadline(r.get('folder_id', ''))
            dl_part  = f' — {dl}' if dl else ''
            lines.append(
                f"• {r['client_name']} / {r['folder_name']} — {editor} — {r['video_count']} vids — since {date_str}{dl_part}"
            )

        embed = discord.Embed(
            title=f'⏳ In Progress — Sorted by Date ({len(rows)} folders, oldest first)',
            color=discord.Color.blurple(),
        )
        chunks, current, current_len = [], [], 0
        for line in lines:
            needed = len(line) + (1 if current else 0)
            if current and current_len + needed > 1020:
                chunks.append('\n'.join(current))
                current, current_len = [line], len(line)
            else:
                current.append(line)
                current_len += needed
        if current:
            chunks.append('\n'.join(current))

        embed.add_field(name='📋 Folders', value=chunks[0] if chunks else 'None', inline=False)
        await interaction.followup.send(embed=embed)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)


# ── Discord client ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.guilds = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Without this, an exception after defer() leaves the user staring at
    infinite loading → 'interaction failed' with nothing in our logs."""
    cmd = interaction.command.qualified_name if interaction.command else '?'
    logger.error(f'/{cmd} failed for {interaction.user} in channel {interaction.channel_id}: {error}', exc_info=error)
    # Also to the ops channel. "The error has been logged" was technically true
    # and practically useless: the log file lived on a disk that gets wiped
    # every redeploy, and stdout is a short rolling window nobody watches.
    # /stats was dead in Steven's channel for a day for exactly that reason
    # (2026-08-20). Best-effort — a failed ops post must not replace the
    # editor's own error message.
    try:
        root = error.__cause__ or error
        send_discord_ops_channel(
            f'⚠️ **/{cmd}** failed for {interaction.user.mention} '
            f'in <#{interaction.channel_id}>\n```{type(root).__name__}: {str(root)[:400]}```'
        )
    except Exception as e:
        logger.warning(f'on_app_command_error: ops post failed: {e}')
    try:
        msg = ('⚠️ Something went wrong running that command. It has been reported '
               'to the ops channel — ping Vexxe if you need it sooner.')
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_ready():
    logger.info(f'Discord bot ready — logged in as {bot.user} ({bot.user.id})')
    config = load_config()
    main_guild    = discord.Object(id=int(config['discord_guild_id']))
    creator_guild = discord.Object(id=int(config['creator_guild_id']))
    try:
        synced = await tree.sync(guild=main_guild)
        logger.info(f'Synced {len(synced)} slash command(s) to main guild {config["discord_guild_id"]}')
    except Exception as e:
        logger.error(f'Failed to sync slash commands to main guild: {e}')
    try:
        synced_creator = await tree.sync(guild=creator_guild)
        logger.info(f'Synced {len(synced_creator)} slash command(s) to creator guild {config["creator_guild_id"]}')
    except Exception as e:
        logger.error(f'Failed to sync slash commands to creator guild: {e}')
    asyncio.get_event_loop().create_task(process_queue_loop())
    # on_ready refires on gateway reconnects — guard so we never run two
    # dashboard pollers (double polling would double-post assignments).
    global _dashboard_commands_started
    if not _dashboard_commands_started:
        _dashboard_commands_started = True
        asyncio.get_event_loop().create_task(dashboard_commands_loop())
        asyncio.get_event_loop().create_task(provision_link_loop())
        asyncio.get_event_loop().create_task(reconcile_loop())
    if not leaderboard_loop.is_running():
        leaderboard_loop.start()
    if not deadline_checker.is_running():
        deadline_checker.start()
    if not review_recheck_loop.is_running():
        review_recheck_loop.start()

    # Re-register persistent AssignEditorViews so dropdowns survive restarts
    pending_ops = load_pending_ops_assigns()
    if pending_ops:
        editors_map  = fetch_editors_from_notion()
        editor_names = sorted(editors_map.keys())
        for msg_id_str, item in pending_ops.items():
            try:
                # Three card types share this map. An offer is checked FIRST
                # because it also carries a ticket_id, so the old two-arm
                # ternary would have restored it as an editor dropdown — a
                # card asking "will you take this?" coming back after a
                # restart as "pick who gets this".
                if item.get('card_kind') == 'assign_offer':
                    view = AssignOfferView(item)
                elif item.get('ticket_id'):
                    # Website batches use the dashboard picker (no Drive
                    # folder, no Notion row).
                    view = DashboardAssignView(item, editor_names)
                else:
                    view = AssignEditorView(item, editor_names)
                bot.add_view(view, message_id=int(msg_id_str))
            except Exception as _e:
                logger.warning(f'on_ready: could not re-register ops assign view {msg_id_str}: {_e}')
        logger.info(f'on_ready: re-registered {len(pending_ops)} pending ops assign view(s)')

    # Re-register persistent DiscordReviewViews so the Approve button survives restarts —
    # previously these views were only attached in-memory at post time, so any review still
    # pending across a bot restart had a dead Approve button (no error shown to the clicker).
    pending_reviews = load_pending_reviews()
    reregistered = 0
    for rd in pending_reviews.values():
        msg_id = rd.get('review_message_id')
        if not msg_id or rd.get('status') != 'pending':
            continue
        try:
            bot.add_view(DiscordReviewView(rd), message_id=int(msg_id))
            reregistered += 1
        except Exception as _e:
            logger.warning(f"on_ready: could not re-register review view {rd.get('review_id')}: {_e}")
    logger.info(f'on_ready: re-registered {reregistered} pending review view(s)')

    # Re-register ▶️ Start / footage-problem views so the buttons survive restarts.
    # Registered by custom_id (no message_id) so one view per folder also covers the
    # pickup-reminder messages, which carry the same buttons. Entries with neither
    # pending_start nor started_at are pre-Start-feature (due_ts set directly, no
    # migration) — treat them as already-started so their footage button still works.
    start_views = 0
    for fid, d in load_deadlines().items():
        try:
            if d.get('pending_start'):
                bot.add_view(StartAssignmentView(fid))
            else:
                bot.add_view(StartAssignmentView(fid, started=True))
            start_views += 1
        except Exception as _e:
            logger.warning(f'on_ready: could not re-register start view for {fid}: {_e}')
    logger.info(f'on_ready: re-registered {start_views} start/footage view(s)')


@tree.command(name='stats', description='View your video stats', guilds=[GUILD_OBJ, CREATOR_GUILD_OBJ])
async def stats_command(interaction: discord.Interaction):
    # Team members get an ephemeral reply — their view includes the Performance
    # field (missed deadlines, slow pickups, etc.) which editors shouldn't see
    # when /stats is run inside an editor's channel. An ephemeral defer makes
    # every followup in this command ephemeral too.
    is_team = any(r.name == 'Team' for r in getattr(interaction.user, 'roles', []))
    await interaction.response.defer(ephemeral=is_team)

    config     = load_config()
    guild_id   = interaction.guild_id
    channel_id = interaction.channel_id
    loop       = asyncio.get_event_loop()

    # ── Editor server ──────────────────────────────────────────────────────────
    if guild_id == int(config['discord_guild_id']):
        editor_name, editor_data = await loop.run_in_executor(
            None, fetch_editor_by_channel_id, channel_id
        )
        if not editor_name:
            await interaction.followup.send(
                'This channel is not registered as an editor channel.', ephemeral=True
            )
            return

        token = config['notion_token']
        fresh_active, (active_rows, history_rows, today_rows, week_rows, month_rows, revision_rows) = await asyncio.gather(
            loop.run_in_executor(None, recalculate_active_videos, token, editor_name),
            asyncio.gather(
                loop.run_in_executor(None, fetch_active_queue_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivery_history_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivered_today_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivered_this_week_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivered_this_month_for_editor, editor_name),
                loop.run_in_executor(None, fetch_revision_folders_for_editor, editor_name),
            ),
        )
        today_videos = sum(r['videos_completed'] for r in today_rows)
        week_videos  = sum(r['videos_completed'] for r in week_rows)
        month_videos = sum(r['videos_completed'] for r in month_rows)
        # Use the higher of the live Delivery History query and the Editor Profiles counter —
        # old rows have no dates so the live query may undercount for editors with prior deliveries.
        week_videos  = max(week_videos, editor_data.get('week', 0))

        # Website-native deliveries (no Notion row, so no Delivery History query
        # will ever pick them up) — Editor Profiles week/month/total already got
        # these counts via handle_cc_dashboard_delivered, but "today" has no
        # Notion-side signal at all, so it's only ever visible from this file.
        today_start_edt = datetime.now(EDT).replace(hour=0, minute=0, second=0, microsecond=0)
        # `editors` here so website rows match on this editor's email / discord
        # id rather than on the site spelling their name the same way Notion
        # does — it doesn't for four of them.
        stats_editors = await loop.run_in_executor(None, fetch_editors_from_notion)
        today_videos += dashboard_delivered_videos_for_editor(
            editor_name,
            since_ts=today_start_edt.astimezone(timezone.utc).timestamp(),
            editors=stats_editors,
        )
        # Same for the month: Delivery History rows + website-native deliveries
        # is the live figure; the Editor Profiles counter is a running tally
        # that drifts low whenever a delivery skipped the bot. Higher wins,
        # same rule as the week line — the counter still covers any rows that
        # predate the Delivered Date column.
        month_start_edt = today_start_edt.replace(day=1)
        month_videos += dashboard_delivered_videos_for_editor(
            editor_name,
            since_ts=month_start_edt.astimezone(timezone.utc).timestamp(),
            editors=stats_editors,
        )
        month_videos = max(month_videos, editor_data.get('month', 0))

        embed = discord.Embed(
            title=f'📊 Editor Stats — {editor_name}', color=discord.Color.blurple()
        )
        embed.add_field(
            name='⚙️ Current Load',
            value=f"{round((fresh_active / editor_data['capacity']) * 100) if editor_data['capacity'] > 0 else 0}%",
            inline=False,
        )

        if active_rows:
            lines = []
            for r in active_rows:
                dl = format_deadline(r.get('folder_id', ''))
                dl_part = f' — {dl}' if dl else ''
                # Folder names link to the assignment message (Drive links + Start
                # button live there); Drive-link fallback for pre-feature folders.
                name = assignment_jump_link(r['folder_name'], r.get('folder_id', ''))
                lines.append(
                    f"• {r['client_name']} / {name} — {r['status']} — {r['video_count']} videos{dl_part}"
                )
            add_lines_fields(embed, f"📁 Active Folders ({len(active_rows)})", lines)
        else:
            embed.add_field(name='📁 Active Folders (0)', value='None', inline=False)

        dash_active = active_dashboard_batches_for_editor(editor_name, stats_editors)
        if dash_active:
            dash_lines = []
            for b in dash_active:
                who = b.get('student_name') or b.get('client_name') or '—'
                vids = f" — {b['video_count']} videos" if b.get('video_count') else ''
                link = f" — [Open]({b['ticket_url']})" if b.get('ticket_url') else ''
                dash_lines.append(f"• {who} / {b.get('folder_name') or 'Untitled batch'}{vids}{link}")
            add_lines_fields(embed, f'🌐 Website Batches ({len(dash_active)})', dash_lines)

        if revision_rows:
            rev_lines = [
                f"• {r['client_name']} / {folder_link(r['folder_name'], r.get('folder_id', ''))} — {r['video_count']} videos"
                for r in revision_rows
            ]
            add_lines_fields(embed, f'🔄 Revisions ({len(revision_rows)})', rev_lines)
        else:
            embed.add_field(name='🔄 Revisions (0)', value='None', inline=False)

        embed.add_field(
            name='✅ Delivered',
            value=(
                f"• Today: {today_videos} videos\n"
                f"• This week: {week_videos} videos\n"
                f"• This month: {month_videos} videos\n"
                f"• All time: {editor_data['total']} videos"
            ),
            inline=False,
        )

        if 'Team' in [r.name for r in interaction.user.roles]:
            avg_pickup = average_pickup_hours(editor_name)
            pickup_line = f"\n• Avg pickup time: {avg_pickup}h" if avg_pickup is not None else ''
            slow4  = editor_data.get('slow_pickups_4h', 0)
            slow12 = editor_data.get('slow_pickups_12h', 0)
            slow_line = f"\n• Slow pickups: {slow4} over 4h ({slow12} over 12h)" if slow4 or slow12 else ''
            embed.add_field(
                name='📈 Performance (this month)',
                value=(
                    f"• Revisions received: {editor_data.get('revisions', 0)}\n"
                    f"• Missed deadlines: {editor_data.get('missed_deadlines', 0)}"
                    f"{pickup_line}{slow_line}"
                ),
                inline=False,
            )

        valid_history = [r for r in history_rows if (r['videos_completed'] or 0) >= 1]
        if valid_history:
            lines = [
                f"• {r['client_name']} / {folder_link(r['folder_name'], drive_link=r.get('drive_link', ''))} — {r['videos_completed']} videos — {r['delivered_date']}"
                for r in valid_history
            ]
            add_lines_fields(embed, '📋 Completed Folders (last 10)', lines)

        msg = await interaction.followup.send(embed=embed)
        asyncio.create_task(_auto_delete_later(msg))

    # ── Creator server ─────────────────────────────────────────────────────────
    elif guild_id == int(config['creator_guild_id']):
        logger.info(f"/stats creator: channel_id={channel_id}")
        client_name = await loop.run_in_executor(None, fetch_creator_by_channel_id, channel_id)
        logger.info(f"/stats creator: resolved client_name={repr(client_name)!r}")
        if not client_name:
            await interaction.followup.send(
                'This channel is not registered. Contact Vexxe.', ephemeral=True
            )
            return

        queue_rows, pending_rows, revision_rows, delivery_history_rows = await asyncio.gather(
            loop.run_in_executor(None, fetch_active_queue_for_creator, client_name),
            loop.run_in_executor(None, fetch_pending_assignments_for_creator, client_name),
            loop.run_in_executor(None, fetch_revision_folders_for_creator, client_name),
            loop.run_in_executor(None, fetch_delivery_history_for_creator, client_name),
        )
        statuses = [r['status'] for r in queue_rows]
        logger.info(f"/stats creator {client_name}: {len(queue_rows)} rows, statuses={statuses}")

        # Raw = unassigned (no editor yet) → Pending section
        # In Progress = assigned → Active section
        active_rows  = [r for r in queue_rows if r['status'] not in ('Delivered', 'Revision', 'Raw')]
        raw_rows     = [r for r in queue_rows if r['status'] == 'Raw']
        # Merge: live Raw rows + any stale pending_assignments.json entries not yet in Active Queue.
        # Archived pages (e.g. removed via /remove) drop out of the live Notion query entirely, so
        # their folder_id would otherwise look "unmatched" and wrongly resurface as still-pending —
        # cross-check removed_folders.json too, not just the live queue.
        queue_folder_ids   = {r['folder_id'] for r in queue_rows if r.get('folder_id')}
        removed_folder_ids = {r.get('folder_id') for r in load_removed_folders().values() if r.get('folder_id')}
        stale_pending = [
            r for r in pending_rows
            if r.get('folder_id') not in queue_folder_ids and r.get('folder_id') not in removed_folder_ids
        ]
        pending_rows  = raw_rows + stale_pending
        logger.info(f"/stats creator {client_name}: active={len(active_rows)}, pending(unassigned)={len(pending_rows)}, revisions={len(revision_rows)}")

        embed = discord.Embed(title=f'📊 Stats for {client_name}', color=discord.Color.blurple())

        if active_rows:
            lines = [
                f"• {folder_link(r['folder_name'], r.get('folder_id', ''))} — {r['editor_name'] or 'Unassigned'} — {r['status']} — {r['video_count']} videos"
                for r in active_rows
            ]
            add_lines_fields(embed, f'📁 Active Folders ({len(active_rows)})', lines)
        else:
            embed.add_field(name='📁 Active Folders (0)', value='None', inline=False)

        # Batches submitted on the website have no Active Queue row, so every
        # query above is blind to them — the creator saw "Active Folders (0)"
        # while their editor was part-way through the work. Same field the
        # editor branch has had since 2026-08-05, pointed at the creator.
        dash_active = active_dashboard_batches_for_creator(client_name)
        if dash_active:
            dash_lines = []
            for b in dash_active:
                who  = b.get('editor_name') or 'Unassigned'
                vids = f" — {b['video_count']} videos" if b.get('video_count') else ''
                link = f" — [Open]({b['ticket_url']})" if b.get('ticket_url') else ''
                dash_lines.append(
                    f"• {b.get('folder_name') or 'Untitled batch'} — {who}{vids}{link}"
                )
            add_lines_fields(embed, f'🌐 Website Batches ({len(dash_active)})', dash_lines)

        if revision_rows:
            rev_lines = [
                f"• {folder_link(r['folder_name'], r.get('folder_id', ''))} — {r['editor_name'] or 'Unassigned'}"
                for r in revision_rows
            ]
            add_lines_fields(embed, f'🔄 In Revision ({len(revision_rows)})', rev_lines)
        else:
            embed.add_field(name='🔄 In Revision (0)', value='None', inline=False)

        if pending_rows:
            pending_lines = [
                f"• {folder_link(r['folder_name'], r.get('folder_id', ''))} — {r['video_count']} videos — awaiting assignment"
                for r in pending_rows
            ]
            add_lines_fields(embed, f'⏳ Pending ({len(pending_rows)})', pending_lines)
        else:
            embed.add_field(name='⏳ Pending (0)', value='None', inline=False)

        if delivery_history_rows:
            history_lines = [
                f"• {folder_link(r['folder_name'], drive_link=r.get('drive_link', ''))} — {r['editor_name'] or 'Unknown'} — {r['delivered_date'] or 'no date'}"
                for r in delivery_history_rows
            ]
            add_lines_fields(embed, f'📋 Last Delivered Folders ({len(delivery_history_rows)})', history_lines)

        msg = await interaction.followup.send(embed=embed)
        asyncio.create_task(_auto_delete_later(msg))

    else:
        await interaction.followup.send('This server is not configured.', ephemeral=True)


@tree.command(
    name='revision',
    description='Reopen a folder for revision',
    guilds=[GUILD_OBJ, CREATOR_GUILD_OBJ],
)
async def revision_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    config = load_config()
    guild_id = interaction.guild_id
    channel_id = interaction.channel_id
    loop = asyncio.get_event_loop()

    if guild_id == int(config['creator_guild_id']):
        client_name = await loop.run_in_executor(None, fetch_creator_by_channel_id, channel_id)
        if not client_name:
            await interaction.followup.send(
                'This channel is not registered. Contact Vexxe.', ephemeral=True
            )
            return
    elif guild_id == int(config['discord_guild_id']):
        user_role_names = [r.name for r in interaction.user.roles]
        if 'Team' not in user_role_names:
            await interaction.followup.send(
                '🚫 Only Team members can open revisions from this server.', ephemeral=True
            )
            return
        client_name = await loop.run_in_executor(None, fetch_creator_by_channel_id, channel_id)
        if not client_name:
            await interaction.followup.send(
                'Run this command from a registered creator channel, or use the creator server.', ephemeral=True
            )
            return
    else:
        await interaction.followup.send('This server is not configured.', ephemeral=True)
        return

    delivered_rows = await loop.run_in_executor(None, fetch_delivered_folders_for_creator, client_name)
    if not delivered_rows:
        await interaction.followup.send(
            f'No delivered folders found for **{client_name}**.', ephemeral=True
        )
        return

    view = RevisionFolderSelectView(delivered_rows, client_name)
    await interaction.followup.send(
        'Select a delivered folder to send back for revision:', view=view, ephemeral=True
    )


@tree.command(
    name='selftest',
    description='Raise on purpose, to prove errors reach the ops channel',
    guilds=[GUILD_OBJ],
)
@app_commands.describe(after_defer='Fail after deferring, the way a slow command does')
async def selftest_command(interaction: discord.Interaction, after_defer: bool = True):
    # There is no way to check the error plumbing except to break something,
    # and "wait for a real command to fail" is how /stats stayed broken in
    # Steven's channel for a day (2026-08-20). This is the deliberate break.
    #
    # after_defer matters: an exception BEFORE defer() and one after it take
    # different paths out of discord.py, and the after-defer case is the one
    # that used to leave an editor watching an infinite spinner.
    user_role_names = [r.name for r in getattr(interaction.user, 'roles', [])]
    if 'Team' not in user_role_names:
        await interaction.response.send_message(
            '🚫 This command is restricted to Team members only.', ephemeral=True
        )
        return
    logger.info(f'selftest: raising on purpose for {interaction.user}')
    if after_defer:
        await interaction.response.defer(ephemeral=True)
    raise RuntimeError(
        'selftest: this exception is deliberate — if you can read it in the ops '
        'channel, command error reporting works'
    )


COUNTER_FIELDS = ('revisions', 'missed_deadlines', 'slow_pickups_4h', 'slow_pickups_12h')


@tree.command(
    name='fixcounter',
    description="Correct an editor's counter (revisions, missed deadlines, slow pickups)",
    guilds=[GUILD_OBJ],
)
@app_commands.describe(
    editor='Editor whose counter is wrong',
    field='Which counter',
    value='What it should be',
    reason='Why - goes in the log and the ops post',
)
@app_commands.choices(field=[
    app_commands.Choice(name='revisions', value='revisions'),
    app_commands.Choice(name='missed deadlines', value='missed_deadlines'),
    app_commands.Choice(name='slow pickups (4h)', value='slow_pickups_4h'),
    app_commands.Choice(name='slow pickups (12h)', value='slow_pickups_12h'),
])
async def fixcounter_command(
    interaction: discord.Interaction,
    editor: str,
    field: app_commands.Choice[str],
    value: int,
    reason: str = '',
):
    # These counters only ever went UP, from automated paths, and a wrong one
    # had nowhere to be corrected: they live in editor_counters.json on the box
    # (now the Railway volume), which nobody can reach without a shell. A ledger
    # repair bumped Aki's revisions by one for a revision that never happened
    # (2026-08-19) and there was no way to take it back.
    #
    # Absolute, not a delta: "should be 4" is a claim someone can check against
    # the Revision Log; "minus one" is only checkable if you already know the
    # current number.
    user_role_names = [r.name for r in getattr(interaction.user, 'roles', [])]
    if 'Team' not in user_role_names:
        await interaction.response.send_message(
            '🚫 This command is restricted to Team members only.', ephemeral=True
        )
        return
    if value < 0:
        await interaction.response.send_message("Value can't be negative.", ephemeral=True)
        return

    loop = asyncio.get_event_loop()
    editors = await loop.run_in_executor(None, fetch_editors_from_notion)
    key = resolve_editor_name(editor, editors) or editor
    counters = load_editor_counters()
    if key not in counters:
        counters[key] = {'revisions': 0, 'missed_deadlines': 0}
    before = counters[key].get(field.value, 0)
    if before == value:
        await interaction.response.send_message(
            f'{key} already has {field.value} = {value}.', ephemeral=True
        )
        return
    counters[key][field.value] = value
    save_editor_counters(counters)
    logger.info(
        f'fixcounter: {key} {field.value} {before} -> {value} '
        f'by {interaction.user} ({reason or "no reason given"})'
    )

    line = (
        f"🔧 **{key}** · {field.value.replace('_', ' ')} **{before} → {value}**"
        f" — corrected by {interaction.user.mention}"
        + (f'\n{reason}' if reason else '')
    )
    await interaction.response.send_message(line, ephemeral=False)
    # The ops channel too: a counter changed by hand is exactly the kind of edit
    # that should never be discoverable only by the person who made it.
    try:
        send_discord_ops_channel(line)
    except Exception as e:
        logger.warning(f'fixcounter: ops post failed: {e}')


@fixcounter_command.autocomplete('editor')
async def fixcounter_editor_autocomplete(interaction: discord.Interaction, current: str):
    names = sorted(load_editor_counters().keys())
    if not names:
        loop = asyncio.get_event_loop()
        names = sorted((await loop.run_in_executor(None, fetch_editors_from_notion)).keys())
    hit = [n for n in names if current.lower() in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in hit]


@tree.command(
    name='editorstats',
    description='Overall ops overview for CC Video Manager',
    guilds=[GUILD_OBJ],
)
async def editorstats_command(interaction: discord.Interaction):
    # Role check: only Team members may use this command
    user_role_names = [r.name for r in interaction.user.roles]
    if 'Team' not in user_role_names:
        await interaction.response.send_message(
            '🚫 This command is restricted to Team members only.', ephemeral=True
        )
        return

    await interaction.response.defer()
    loop = asyncio.get_event_loop()
    config = load_config()
    token = config['notion_token']

    # Recalculate all editors before displaying so Active Videos is always accurate
    all_editor_names = list(fetch_editors_from_notion().keys())
    await asyncio.gather(*[
        loop.run_in_executor(None, recalculate_active_videos, token, name)
        for name in all_editor_names
    ])

    editor_loads, active_rows, delivered_today, in_progress_rows, all_revisions, unavailable_editors, unassigned_website = await asyncio.gather(
        loop.run_in_executor(None, fetch_editor_loads_list),
        loop.run_in_executor(None, fetch_active_queue_non_delivered),
        loop.run_in_executor(None, fetch_delivered_today),
        loop.run_in_executor(None, fetch_active_queue_in_progress),
        loop.run_in_executor(None, fetch_all_revision_folders),
        loop.run_in_executor(None, fetch_unavailable_editors_today),
        loop.run_in_executor(None, fetch_pending_website_batches),
    )

    unassigned = [r for r in active_rows if r['status'] == 'Raw']

    delivered_folder_count = len(delivered_today)
    delivered_video_total  = sum(r['videos_completed'] for r in delivered_today)

    # Website-native batches have no Notion row, so Notion-only 'active' never
    # saw them — an editor's real load (and this %) silently excluded every
    # website ticket. Fold their video counts in here too.
    website_by_editor = active_website_batches_by_editor()

    embed = discord.Embed(
        title='📊 Overall Operations — CC Video Manager',
        color=discord.Color.blurple(),
    )

    # ── Editor Load ────────────────────────────────────────────────────────────
    if editor_loads:
        load_lines = []
        for e in editor_loads:
            web_batches   = website_by_editor.get(e['name'], [])
            web_videos    = sum(b.get('video_count') or 0 for b in web_batches)
            true_active   = e['active'] + web_videos
            pct           = round((true_active / e['capacity']) * 100) if e['capacity'] > 0 else 0
            web_note      = f" (+{web_videos} 🌐)" if web_videos else ''
            load_lines.append(f"• {e['name']}: {pct}%{web_note}")
        embed.add_field(name='⚙️ Editor Load', value='\n'.join(load_lines), inline=False)
    else:
        embed.add_field(name='⚙️ Editor Load', value='No editors found', inline=False)

    # ── Unavailable Editors ────────────────────────────────────────────────────
    if unavailable_editors:
        embed.add_field(
            name=f'🔴 Unavailable Today ({len(unavailable_editors)})',
            value='\n'.join(f'• {n}' for n in unavailable_editors),
            inline=False,
        )
    else:
        embed.add_field(name='🟢 Unavailable Today (0)', value='All editors available', inline=False)

    # ── Unassigned Folders ─────────────────────────────────────────────────────
    if unassigned:
        ua_lines = [
            f"• {r['client_name']} / {folder_link(r['folder_name'], r.get('folder_id', ''))} — {r['video_count']} videos"
            for r in unassigned
        ]
        field_val = '\n'.join(ua_lines)
        if len(field_val) > 1020:
            field_val = field_val[:1020] + '…'
        embed.add_field(
            name=f'📁 Unassigned Folders: {len(unassigned)}',
            value=field_val,
            inline=False,
        )
    else:
        embed.add_field(name='📁 Unassigned Folders: 0', value='All folders assigned ✓', inline=False)

    # ── Unassigned Website Batches (no Drive folder — see fetch_pending_website_batches) ──
    if unassigned_website:
        wb_lines = []
        for b in unassigned_website:
            label = creator_label(b)
            link  = f"[{label}]({b['ticket_url']})" if b.get('ticket_url') else label
            wb_lines.append(f"• {link} — {b.get('video_count') or 0} videos")
        field_val = '\n'.join(wb_lines)
        if len(field_val) > 1020:
            field_val = field_val[:1020] + '…'
        embed.add_field(
            name=f'🌐 Unassigned Website Batches: {len(unassigned_website)}',
            value=field_val,
            inline=False,
        )
    else:
        embed.add_field(name='🌐 Unassigned Website Batches: 0', value='All website batches assigned ✓', inline=False)

    # ── Active Website Batches (assigned, no Drive folder) ─────────────────────
    if website_by_editor:
        active_website_count = sum(len(v) for v in website_by_editor.values())
        wb2_lines = []
        for editor in sorted(website_by_editor.keys()):
            batches = website_by_editor[editor]
            vids = sum(b.get('video_count') or 0 for b in batches)
            wb2_lines.append(f"• **{editor}**: {len(batches)} batch(es), {vids} videos")
        field_val = '\n'.join(wb2_lines)
        if len(field_val) > 1020:
            field_val = field_val[:1020] + '…'
        embed.add_field(
            name=f'🌐 Active Website Batches: {active_website_count}',
            value=field_val,
            inline=False,
        )

    # ── Revisions ──────────────────────────────────────────────────────────────
    if all_revisions:
        rev_lines = [
            f"• {r['client_name']} / {folder_link(r['folder_name'], r.get('folder_id', ''))} → {r['editor_name'] or 'unassigned'}"
            for r in all_revisions
        ]
        field_val = '\n'.join(rev_lines)
        if len(field_val) > 1020:
            field_val = field_val[:1020] + '…'
        embed.add_field(name=f'🔄 In Revision: {len(all_revisions)}', value=field_val, inline=False)
    else:
        embed.add_field(name='🔄 In Revision: 0', value='None ✓', inline=False)

    # ── Summary counts ─────────────────────────────────────────────────────────
    embed.add_field(
        name='✅ Delivered Today',
        value=f'{delivered_folder_count} folders / {delivered_video_total} videos',
        inline=True,
    )
    embed.add_field(
        name='⏳ In Progress',
        value=f'{len(in_progress_rows)} folders',
        inline=True,
    )

    # ── Editor Performance (revisions + missed deadlines + avg pickup) ─────────
    all_editor_stats = fetch_all_editor_stats()
    def _perf_line(e):
        avg_pickup = average_pickup_hours(e['name'])
        pickup_part = f", {avg_pickup}h avg pickup" if avg_pickup is not None else ''
        slow4, slow12 = e.get('slow_pickups_4h', 0), e.get('slow_pickups_12h', 0)
        slow_part = f", {slow4} slow pickups ({slow12} >12h)" if slow4 or slow12 else ''
        return f"• {e['name']}: {e['revisions']} revisions, {e['missed_deadlines']} missed{pickup_part}{slow_part}"
    perf_lines = [
        _perf_line(e)
        for e in sorted(all_editor_stats, key=lambda x: x['name'])
        if e['revisions'] > 0 or e['missed_deadlines'] > 0
        or e.get('slow_pickups_4h', 0) > 0 or average_pickup_hours(e['name']) is not None
    ]
    if perf_lines:
        embed.add_field(
            name='📈 Editor Performance (this month)',
            value='\n'.join(perf_lines),
            inline=False,
        )

    view = EditorStatsView(embed, delivered_today, in_progress_rows, website_by_editor)
    msg  = await interaction.followup.send(embed=embed, view=view)
    asyncio.create_task(_auto_delete_later(msg))


@tree.command(name='help', description='Show all available commands', guilds=[GUILD_OBJ])
async def help_command(interaction: discord.Interaction):
    is_team = 'Team' in [r.name for r in interaction.user.roles]

    embed = discord.Embed(
        title='CC Video Manager — Commands',
        description='All available slash commands for this server.',
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name='📊 /stats',
        value=(
            'Your personal video stats.\n'
            '**Shows:** Delivered today / this week / this month / all time, '
            'active assignments with deadlines remaining.'
        ),
        inline=False,
    )

    embed.add_field(
        name='✅ /complete',
        value=(
            'Mark an assignment as done.\n'
            '**How:** Run in your editor channel → enter the edited folder name and video count '
            '→ bot verifies against Drive → sends review to Vex on Telegram.'
        ),
        inline=False,
    )

    embed.add_field(
        name='🏆 /leaderboard',
        value=(
            'Editor leaderboard sorted by videos delivered this week.\n'
            'Team members also see the monthly board.'
        ),
        inline=False,
    )

    embed.add_field(
        name='🔄 /revision',
        value=(
            'Reopen a delivered folder for revision.\n'
            '**How:** Run in the creator\'s channel → select the folder → editor gets a revision ping.'
        ),
        inline=False,
    )

    embed.add_field(
        name='✅ /available',
        value='Mark yourself as available today — puts you back in the assignment pool.',
        inline=False,
    )

    embed.add_field(
        name='❌ /unavailable',
        value='Mark yourself as unavailable today — Vex won\'t auto-assign folders to you.',
        inline=False,
    )

    embed.add_field(
        name='📁 /info',
        value=(
            'Drive links + full info for a folder.\n'
            '**How:** Run in your editor channel → pick from your in-progress + last 10 delivered folders. '
            'Shows Client/Raw Footage/Edited Drive links, status, due date, revisions, turnaround.'
        ),
        inline=False,
    )

    if is_team:
        embed.add_field(
            name='─── Team commands ───',
            value='​',
            inline=False,
        )

        embed.add_field(
            name='📋 /editorstats',
            value=(
                'Full ops overview — editor load %, unassigned folders, '
                'delivered today, in-progress count. Expandable detail buttons included.'
            ),
            inline=False,
        )

        embed.add_field(
            name='🔎 /info folder:<name>',
            value=(
                'Search **any** folder by name, anywhere — autocompletes as you type.\n'
                '**Shows:** Drive links, status, editor, due date, revisions, turnaround, was-overdue.'
            ),
            inline=False,
        )

        embed.add_field(
            name='🔁 /reassign',
            value=(
                'Move an in-progress folder to a different editor.\n'
                '**How:** Select folder → select new editor → Notion updates, '
                'deadline transfers, new assignment embed posts.'
            ),
            inline=False,
        )

        embed.add_field(
            name='⏱️ /extend',
            value=(
                "Extend a folder's deadline.\n"
                '**How:** Select folder → enter hours to add (enter `0` for no deadline).'
            ),
            inline=False,
        )

        embed.add_field(
            name='🩺 /health',
            value='Show the last 10 errors and warnings from the bot log.',
            inline=False,
        )

        embed.add_field(
            name='🗑️ /remove',
            value=(
                'Remove a folder from the pending or active queue (cached).\n'
                '**How:** Select folder → archived in Notion, removed from queues.'
            ),
            inline=False,
        )

        embed.add_field(
            name='♻️ /recover',
            value='Restore a folder previously removed via `/remove`.',
            inline=False,
        )

        embed.add_field(
            name='↩️ /unstart',
            value=(
                "Undo a misclicked ▶️ Start — the folder returns to pending-start "
                "(no deadline until Start is pressed again).\n"
                "**How:** Run in the editor's channel → pick the folder."
            ),
            inline=False,
        )

    embed.set_footer(text='Team-only commands are visible to Team role members only.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


class StartFolderSelect(discord.ui.View):
    """Dropdown fallback for /start — the editor's un-started folders."""
    def __init__(self, rows):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=f"{r['client_name']} / {r['folder_name']}"[:100],
                value=r['folder_id'][:100],
                description=f"assigned {r['waiting_h']}h ago"[:100],
            )
            for r in rows[:25]
        ]
        select = discord.ui.Select(placeholder='Pick a folder to start…', options=options)

        async def on_select(interaction: discord.Interaction):
            await _start_folder_clicked(interaction, select.values[0])

        select.callback = on_select
        self.add_item(select)


@tree.command(name='start', description='Start the clock on an assigned folder', guilds=[GUILD_OBJ])
async def start_command(interaction: discord.Interaction):
    loop = asyncio.get_event_loop()
    editor_name, _ = await loop.run_in_executor(None, fetch_editor_by_channel_id, interaction.channel_id)
    if not editor_name:
        await interaction.response.send_message(
            'Run this in your editor channel — it lists your un-started folders.', ephemeral=True)
        return

    now  = time.time()
    rows = [
        {
            'folder_id':   fid,
            'client_name': d.get('client_name', '?'),
            'folder_name': d.get('folder_name', fid),
            'waiting_h':   int((now - d['assigned_at']) // 3600) if d.get('assigned_at') else 0,
        }
        for fid, d in load_deadlines().items()
        if d.get('pending_start') and d.get('editor_name') == editor_name
    ]
    if not rows:
        await interaction.response.send_message(
            '✅ Nothing waiting to be started — all your folders are already running.', ephemeral=True)
        return
    if len(rows) == 1:
        await _start_folder_clicked(interaction, rows[0]['folder_id'])
        return
    await interaction.response.send_message(
        f'⏸️ You have {len(rows)} un-started folder(s):', view=StartFolderSelect(rows), ephemeral=True)


class UnstartFolderSelect(discord.ui.View):
    """Dropdown fallback for /unstart — the channel editor's started folders."""
    def __init__(self, rows):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=f"{r['client_name']} / {r['folder_name']}"[:100],
                value=r['folder_id'][:100],
                description=f"started {r['running_h']}h ago"[:100],
            )
            for r in rows[:25]
        ]
        select = discord.ui.Select(placeholder='Pick a folder to un-start…', options=options)

        async def on_select(interaction: discord.Interaction):
            await _unstart_folder_clicked(interaction, select.values[0])

        select.callback = on_select
        self.add_item(select)


@tree.command(name='unstart', description='Undo a misclicked Start — back to pending-start (Team only)', guilds=[GUILD_OBJ])
async def unstart_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in getattr(interaction.user, 'roles', [])]:
        await interaction.response.send_message('🚫 Team role required.', ephemeral=True)
        return
    loop = asyncio.get_event_loop()
    editor_name, _ = await loop.run_in_executor(None, fetch_editor_by_channel_id, interaction.channel_id)
    if not editor_name:
        await interaction.response.send_message(
            "Run this in the editor's channel whose Start you want to undo.", ephemeral=True)
        return

    now  = time.time()
    rows = [
        {
            'folder_id':   fid,
            'client_name': d.get('client_name', '?'),
            'folder_name': d.get('folder_name', fid),
            'running_h':   int((now - d['started_at']) // 3600) if d.get('started_at') else 0,
        }
        for fid, d in load_deadlines().items()
        if not d.get('pending_start') and d.get('started_at') and d.get('editor_name') == editor_name
    ]
    if not rows:
        await interaction.response.send_message(
            'No started folders here — nothing to undo.', ephemeral=True)
        return
    if len(rows) == 1:
        await _unstart_folder_clicked(interaction, rows[0]['folder_id'])
        return
    await interaction.response.send_message(
        f'{len(rows)} started folder(s) — pick which to undo:', view=UnstartFolderSelect(rows), ephemeral=True)


@tree.command(name='ask', description='Ask the AI ops assistant (Team only)', guilds=[GUILD_OBJ])
@app_commands.describe(question='e.g. "who is available right now?" or "who has lightest load?"')
async def ask_command(interaction: discord.Interaction, question: str):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team role required.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop    = asyncio.get_event_loop()
    config  = load_config()
    editors, profile_schedules = await asyncio.gather(
        loop.run_in_executor(None, fetch_editors_from_notion),
        loop.run_in_executor(None, ai_ops.fetch_schedules_from_profiles, config['notion_token']),
    )

    ctx_str = ai_ops.build_context_from_editors(editors, profile_schedules=profile_schedules)
    answer  = await loop.run_in_executor(
        None, ai_ops.ai_answer_query, ctx_str, question, profile_schedules
    )
    await interaction.followup.send(f'🤖 **AI Ops**\n\n{answer}', ephemeral=True)


WEBSITE_BATCH_PREFIX = '🌐 '  # marks a website-native batch (no Drive folder) in /assign's folder picker


@tree.command(name='assign', description='Assign an unassigned folder to an editor (Team only)', guilds=[GUILD_OBJ])
@app_commands.describe(folder='Folder name (unassigned) — 🌐 prefix = website batch, no Drive folder', editor='Editor to assign')
async def assign_command(interaction: discord.Interaction, folder: str, editor: str):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team role required.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()

    editors = await loop.run_in_executor(None, fetch_editors_from_notion)
    if editor not in editors:
        names = ', '.join(sorted(editors.keys()))
        await interaction.followup.send(f'❌ Editor "{editor}" not found. Available: {names}', ephemeral=True)
        return

    # Website-native batch (no Drive folder, no Notion row) — assigned straight
    # through the dashboard bridge, same path the #assignments dropdown uses.
    if folder.startswith(WEBSITE_BATCH_PREFIX):
        wanted = folder[len(WEBSITE_BATCH_PREFIX):].strip().lower()
        site_item = None
        for b in await loop.run_in_executor(None, fetch_pending_website_batches):
            if (b.get('folder_name') or '').strip().lower() == wanted:
                site_item = b
                break
        if not site_item:
            await interaction.followup.send(
                f'❌ No pending website batch found matching "{wanted}" — it may have already been assigned.',
                ephemeral=True,
            )
            return

        uid = str((editors.get(editor) or {}).get('discord_user_id') or '')
        await loop.run_in_executor(None, post_dashboard_assignment, {
            'ticket_id':         site_item.get('ticket_id', ''),
            'creator_name':      site_item.get('student_name', ''),
            'folder_name':       site_item.get('folder_name', ''),
            'editor_name':       editor,
            'editor_discord_id': uid,
            'video_count':       site_item.get('video_count', 0),
        })
        remove_pending_ops_assign(site_item['msg_id'])

        # Best-effort tidy of the original dropdown message — the assignment
        # already went through above regardless of whether this succeeds.
        try:
            ch = bot.get_channel(int(site_item['channel_id'])) or await bot.fetch_channel(int(site_item['channel_id']))
            msg = await ch.fetch_message(int(site_item['msg_id']))
            done_embed = discord.Embed(title=f'✅ Assigned to {editor}', color=discord.Color.green())
            done_embed.add_field(name='Creator', value=creator_label(site_item), inline=True)
            done_embed.add_field(name='Batch', value=site_item.get('folder_name') or '—', inline=True)
            await msg.edit(embed=done_embed, view=None)
        except Exception as e:
            logger.warning(f'/assign: could not tidy website-batch message {site_item.get("msg_id")}: {e}')

        await interaction.followup.send(
            f'✅ **{creator_label(site_item)} / {site_item["folder_name"]}** (website batch) assigned to **{editor}**.',
            ephemeral=True,
        )
        logger.info(f"/assign: website batch {site_item['folder_name']} → {editor} by {interaction.user}")
        return

    config = load_config()
    token  = config['notion_token']

    # Find the Raw Active Queue row for this folder name
    body  = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
    pages = await loop.run_in_executor(None, notion_query_all, token, ACTIVE_QUEUE_DB, body)

    matched = None
    for page in pages:
        p         = page['properties']
        title_rt  = p.get('Video', {}).get('title', [])
        fname     = title_rt[0].get('plain_text', '') if title_rt else ''
        if fname.strip().lower() == folder.strip().lower():
            creator_rt  = p.get('Creator', {}).get('rich_text', [])
            client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
            notes_rt    = p.get('Notes', {}).get('rich_text', [])
            notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m           = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            drive_link  = p.get('Drive Link', {}).get('url') or ''
            m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id   = m2.group(1) if m2 else ''
            matched = {
                'notion_page_id': page['id'],
                'folder_name':    fname,
                'client_name':    client_name,
                'video_count':    video_count,
                'folder_id':      folder_id,
            }
            break

    if not matched:
        await interaction.followup.send(f'❌ No unassigned folder found matching "{folder}".', ephemeral=True)
        return

    result_pid = await loop.run_in_executor(
        None, _assign_raw_to_editor, token, matched['folder_id'], editor
    )
    notion_page_id = result_pid or matched['notion_page_id']

    await assign_folder(
        matched['client_name'], matched['folder_name'], matched['video_count'],
        matched['folder_id'], editor, notion_page_id,
    )
    await handle_creator_notify({
        'client_name': matched['client_name'],
        'folder_name': matched['folder_name'],
        'editor_name': editor,
        'video_count': matched['video_count'],
        'folder_id':   matched['folder_id'],
    })

    await interaction.followup.send(
        f'✅ **{matched["client_name"]} / {matched["folder_name"]}** assigned to **{editor}**.',
        ephemeral=True,
    )
    logger.info(f"/assign: {matched['client_name']}/{matched['folder_name']} → {editor} by {interaction.user}")


@assign_command.autocomplete('folder')
async def assign_folder_autocomplete(interaction: discord.Interaction, current: str):
    loop  = asyncio.get_event_loop()
    token = load_config()['notion_token']
    url   = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body  = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}, 'page_size': 50}
    resp  = await loop.run_in_executor(
        None, lambda: requests.post(url, headers=notion_headers(token), json=body, timeout=10)
    )
    choices = []
    if resp.ok:
        for page in resp.json().get('results', []):
            p        = page['properties']
            title_rt = p.get('Video', {}).get('title', [])
            fname    = title_rt[0].get('plain_text', '') if title_rt else ''
            if fname and (not current or current.lower() in fname.lower()):
                choices.append(app_commands.Choice(name=fname[:100], value=fname[:100]))

    # Website-native batches (no Drive folder) — same picker, marked with the
    # 🌐 prefix so assign_command knows to route them through the dashboard
    # bridge instead of Notion.
    for b in await loop.run_in_executor(None, fetch_pending_website_batches):
        fname = (b.get('folder_name') or '').strip()
        if not fname or (current and current.lower() not in fname.lower()):
            continue
        label = f'{WEBSITE_BATCH_PREFIX}{fname}'[:100]
        choices.append(app_commands.Choice(name=label, value=label))

    return choices[:25]


@assign_command.autocomplete('editor')
async def assign_editor_autocomplete(interaction: discord.Interaction, current: str):
    loop    = asyncio.get_event_loop()
    editors = await loop.run_in_executor(None, fetch_editors_from_notion)
    return [
        app_commands.Choice(name=name, value=name)
        for name in sorted(editors.keys())
        if not current or current.lower() in name.lower()
    ][:25]


@tree.command(name='refire', description='Re-send assignment embeds for all In Progress folders (use after bot restart)', guilds=[GUILD_OBJ])
async def refire_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team role required.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, fetch_active_queue_in_progress)

    sent = 0
    skipped = 0
    for row in rows:
        editor = row.get('editor_name', '').strip()
        if not editor:
            skipped += 1
            continue
        try:
            await assign_folder(
                row['client_name'], row['folder_name'], row['video_count'],
                row['folder_id'], editor, row['notion_page_id'],
            )
            sent += 1
            await asyncio.sleep(0.5)
        except Exception as _e:
            logger.error(f'refire: failed for {row["folder_name"]} → {editor}: {_e}')
            skipped += 1

    await interaction.followup.send(
        f'🔄 Refired **{sent}** assignment embed(s) to editors.\n'
        f'{"⚠️ " + str(skipped) + " skipped (no editor set)." if skipped else ""}',
        ephemeral=True,
    )
    logger.info(f'/refire by {interaction.user}: {sent} sent, {skipped} skipped')


@tree.command(
    name='info',
    description='Drive links + full info for a folder — your own folders, or search any (Team)',
    guilds=[GUILD_OBJ],
)
@app_commands.describe(folder='Search any folder by name (Team only) — leave blank to see your own recent folders')
async def info_command(interaction: discord.Interaction, folder: str = ''):
    loop = asyncio.get_event_loop()
    is_team = 'Team' in [r.name for r in interaction.user.roles]

    if folder:
        if not is_team:
            await interaction.response.send_message('🚫 Team role required to search any folder.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = await _build_dossier_embed(folder)
        msg = await interaction.followup.send(embed=embed, ephemeral=True)
        asyncio.create_task(_auto_delete_later(msg))
        return

    editor_name, _ = await loop.run_in_executor(None, fetch_editor_by_channel_id, interaction.channel_id)
    if not editor_name:
        msg = ('Run `/info` inside your editor channel to see your own folders, '
               'or use `/info folder:<name>` to search any folder.') if is_team else \
              'Run this command inside your editor channel to see your folders.'
        await interaction.response.send_message(msg, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    in_progress = await loop.run_in_executor(None, fetch_in_progress_for_editor, editor_name)
    delivered   = await loop.run_in_executor(None, fetch_recent_delivered_for_editor, editor_name, 10)

    rows = []
    for r in in_progress:
        rows.append({
            'notion_page_id': r['notion_queue_page_id'],
            'client_name':    r['client_name'],
            'folder_name':    r['folder_name'],
            'description':    '🔁 Revision' if r.get('is_revision') else '🔧 In progress',
            'emoji':          '🔁' if r.get('is_revision') else '🔧',
        })
    for r in delivered:
        rows.append({
            'notion_page_id': r['notion_page_id'],
            'client_name':    r['client_name'],
            'folder_name':    r['folder_name'],
            'description':    f"Delivered {r.get('delivered_date', '')}",
            'emoji':          '✅',
        })

    if not rows:
        await interaction.followup.send('No folders found for you yet.', ephemeral=True)
        return

    msg = await interaction.followup.send(
        content=f"📁 **{editor_name}**'s folders — pick one for Drive links & info:",
        view=InfoFolderSelectView(rows),
        ephemeral=True,
    )
    asyncio.create_task(_auto_delete_later(msg))


@info_command.autocomplete('folder')
async def info_folder_autocomplete(interaction: discord.Interaction, current: str):
    loop  = asyncio.get_event_loop()
    token = load_config()['notion_token']
    url   = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body  = {'page_size': 25}
    if current:
        body['filter'] = {'property': 'Video', 'title': {'contains': current}}
    resp = await loop.run_in_executor(
        None, lambda: requests.post(url, headers=notion_headers(token), json=body, timeout=10)
    )
    choices = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props      = page['properties']
            title_rt   = props.get('Video', {}).get('title', [])
            fname      = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt = props.get('Creator', {}).get('rich_text', [])
            cname      = creator_rt[0].get('plain_text', '') if creator_rt else ''
            if not fname:
                continue
            label = f'{fname} — {cname}' if cname else fname
            choices.append(app_commands.Choice(name=label[:100], value=page['id']))
    return choices[:25]


@tree.command(name='health', description='Show recent bot errors from the log', guilds=[GUILD_OBJ])
async def health_command(interaction: discord.Interaction):
    log_file = os.path.join(BASE_DIR, 'logs', 'discord_bot.log')
    try:
        if not os.path.exists(log_file):
            await interaction.response.send_message('No log file found yet.', ephemeral=True)
            return
        with open(log_file, encoding='utf-8') as f:
            lines = f.readlines()
        error_lines = [l.rstrip() for l in lines if ' | ERROR    |' in l or ' | WARNING  |' in l]
        last_10 = error_lines[-10:] if error_lines else []
        if not last_10:
            body = '✅ No errors or warnings in the log.'
        else:
            body = '\n'.join(last_10)
        # Discord messages cap at 2000 chars
        if len(body) > 1900:
            body = '...(truncated)\n' + body[-1900:]
        await interaction.response.send_message(f'```\n{body}\n```', ephemeral=True)
    except Exception as e:
        logger.error(f'/health command failed: {e}', exc_info=True)
        await interaction.response.send_message(f'Error reading log: {e}', ephemeral=True)


@tree.command(name='leaderboard', description='View the editor leaderboard', guilds=[GUILD_OBJ])
async def leaderboard_command(interaction: discord.Interaction):
    await interaction.response.defer()
    loop = asyncio.get_event_loop()
    # Live Sun-Sat (EDT) range, not the cached 'Delivered This Week' counter directly —
    # matches reset_weekly.py's Sunday reset and stays correct for bonus payouts.
    today_edt      = datetime.now(EDT)
    week_start_str = (today_edt - timedelta(days=(today_edt.weekday() + 1) % 7)).strftime('%Y-%m-%d')  # most recent Sunday
    tomorrow_str   = (today_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    editors = await loop.run_in_executor(
        None, fetch_all_editor_stats_for_range, week_start_str, tomorrow_str)

    member_roles = [r.name for r in interaction.user.roles]
    is_team      = 'Team' in member_roles

    weekly_embed = build_weekly_leaderboard_embed(editors)

    if is_team:
        editors_monthly = sorted(editors, key=lambda x: x['month'], reverse=True)
        now = datetime.now(EDT)
        monthly_embed = build_monthly_leaderboard_embed(editors_monthly, now.year, now.month)
        msg = await interaction.followup.send(embeds=[weekly_embed, monthly_embed])
    else:
        msg = await interaction.followup.send(embed=weekly_embed)
    asyncio.create_task(_auto_delete_later(msg))


@tree.command(name='complete', description='Mark a folder as complete', guilds=[GUILD_OBJ])
async def complete_command(interaction: discord.Interaction):
    # Defer immediately — Notion calls below take >3s and would expire the interaction
    await interaction.response.defer(ephemeral=True)
    loop       = asyncio.get_event_loop()
    channel_id = interaction.channel_id

    # Parallelize the two independent Notion lookups
    (editor_name, _), editors = await asyncio.gather(
        loop.run_in_executor(None, fetch_editor_by_channel_id, channel_id),
        loop.run_in_executor(None, fetch_editors_from_notion),
    )
    if not editor_name:
        await interaction.followup.send(
            'This channel is not registered as an editor channel.', ephemeral=True
        )
        return

    editor_page_id = editors.get(editor_name, {}).get('page_id', '')
    rows = await loop.run_in_executor(None, fetch_in_progress_for_editor, editor_name)

    if not rows:
        await interaction.followup.send('No active assignments found.', ephemeral=True)
        return

    base = {'editor_name': editor_name, 'editor_page_id': editor_page_id, 'channel_id': channel_id}

    if len(rows) == 1:
        # Can't send_modal after defer — use a button that opens it instead
        view = OpenCompleteModalView({**rows[0], **base})
        r    = rows[0]
        await interaction.followup.send(
            f'Completing **{r["client_name"]} / {r["folder_name"]}** — tap below:',
            view=view, ephemeral=True,
        )
        return

    unique_clients = list(dict.fromkeys(r['client_name'] for r in rows))
    if len(unique_clients) == 1:
        client_name = unique_clients[0]
        view = FolderSelectView(rows, client_name, base)
        await interaction.followup.send(
            f'Which folder for {client_name}?', view=view, ephemeral=True
        )
    else:
        view = ClientSelectView(rows, base)
        await interaction.followup.send('Which client?', view=view, ephemeral=True)


# ── Drive link resolver for assignment embed ────────────────────────────────────

def find_assignment_drive_links(client_name, folder_name):
    """
    Top-down search: DRIVE_ROOT_ID → client folder → Raw Footage → assigned subfolder.
    Returns (client_folder_link, raw_footage_subfolder_link).
    Either may be None on failure. Caches client and Raw Footage folder IDs.
    """
    try:
        service = get_drive_service()

        # Step 1: client folder inside root (use cache)
        client_root_id = _client_root_folder_cache.get(client_name)
        if not client_root_id:
            client_root_id = _find_child_folder_id(service, client_name, DRIVE_ROOT_ID)
            if client_root_id:
                _client_root_folder_cache[client_name] = client_root_id
                logger.info(f"find_assignment_drive_links: cached client_root_id={client_root_id} for '{client_name}'")

        if not client_root_id:
            logger.warning(f"find_assignment_drive_links: client folder '{client_name}' not found under root")
            return None, None

        client_folder_link = f'https://drive.google.com/drive/folders/{client_root_id}'

        # Step 2: Raw Footage inside client folder (use cache)
        raw_footage_id = _client_raw_footage_folder_cache.get(client_name)
        if not raw_footage_id:
            raw_footage_id = _find_child_folder_id(service, 'Raw Footage', client_root_id)
            if raw_footage_id:
                _client_raw_footage_folder_cache[client_name] = raw_footage_id

        if not raw_footage_id:
            logger.warning(f"find_assignment_drive_links: 'Raw Footage' not found for '{client_name}'")
            return client_folder_link, None

        # Step 3: assigned subfolder inside Raw Footage
        subfolder_id = _find_child_folder_id(service, folder_name, raw_footage_id)

        if subfolder_id:
            subfolder_link = f'https://drive.google.com/drive/folders/{subfolder_id}'
            logger.info(f"find_assignment_drive_links: found subfolder '{folder_name}' id={subfolder_id}")
            return client_folder_link, subfolder_link

        logger.warning(f"find_assignment_drive_links: subfolder '{folder_name}' not found in Raw Footage")
        return client_folder_link, None

    except Exception as e:
        logger.error(f'Drive error finding assignment links for {client_name}/{folder_name}: {e}')
        return None, None


# ── assign_folder (public API, also called from queue) ─────────────────────────

# ── ▶️ Start button + footage problem flag ─────────────────────────────────────

async def handle_creator_start_notify(client_name, folder_name, editor_name, video_count):
    """Tells the creator's channel that editing has actually begun on their folder.
    Positive events only — pickup nags/escalations are never shown to creators, and
    no delivery time is promised (an /extend or footage problem would turn a promise
    into a visible miss)."""
    try:
        loop = asyncio.get_event_loop()
        channel_id_str, user_id_str = await loop.run_in_executor(None, fetch_creator_discord_info, client_name)
        if not channel_id_str:
            logger.info(f'creator_start_notify: no creator channel for {client_name}')
            return
        ch = bot.get_channel(int(channel_id_str)) or await bot.fetch_channel(int(channel_id_str))
        embed = discord.Embed(title=f'🎬 Editing has started — {folder_name}', color=discord.Color.green())
        embed.add_field(
            name='Status',
            value=f'Your editor has started working on this folder'
                  f'{f" ({video_count} videos)" if video_count else ""}. '
                  f"You'll be notified here when it's delivered.",
            inline=False,
        )
        await ch.send(embed=embed)
        logger.info(f'creator_start_notify sent: {client_name}/{folder_name}')
    except Exception as e:
        logger.error(f'creator_start_notify failed for {client_name}/{folder_name}: {e}')


async def handle_creator_footage_notify(client_name, folder_name, reason, editor_name=None):
    """Tells the creator's channel that a footage problem was found on their folder so
    they can fix/re-upload. Unlike start/delivery notifies this is an action item for
    the creator — editing is paused until the footage is sorted. Best-effort."""
    try:
        loop = asyncio.get_event_loop()
        channel_id_str, user_id_str = await loop.run_in_executor(None, fetch_creator_discord_info, client_name)
        if not channel_id_str:
            logger.info(f'creator_footage_notify: no creator channel for {client_name}')
            return False
        ch = bot.get_channel(int(channel_id_str)) or await bot.fetch_channel(int(channel_id_str))
        embed = discord.Embed(title=f'⚠️ Footage issue — {folder_name}', color=discord.Color.orange())
        embed.add_field(
            name='What we found',
            value=str(reason)[:1000],
            inline=False,
        )
        embed.add_field(
            name='What we need',
            value='Please check the footage and re-upload / clarify the affected clips. '
                  'Editing on this folder is paused until it\'s sorted.',
            inline=False,
        )
        mention = f'<@{user_id_str}> ' if user_id_str else ''
        await ch.send(content=f'{mention}heads up 👇' if mention else None, embed=embed)
        logger.info(f'creator_footage_notify sent: {client_name}/{folder_name}')
        return True
    except Exception as e:
        logger.error(f'creator_footage_notify failed for {client_name}/{folder_name}: {e}')
        return False


class FootageProblemModal(discord.ui.Modal, title='Report a Footage Problem'):
    details = discord.ui.TextInput(
        label="What's wrong with the footage?",
        style=discord.TextStyle.paragraph,
        placeholder='e.g. files corrupt, folder empty, missing clips 3-5…',
        required=True, max_length=900,
    )

    def __init__(self, folder_id):
        super().__init__()
        self._folder_id = folder_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        reason = str(self.details.value)[:1000]
        deadlines = load_deadlines()
        entry = deadlines.get(self._folder_id, {})
        if entry:
            # Pauses pickup nags — the flag itself is the explanation for "not started"
            entry['footage_flagged'] = True
            entry['footage_reason'] = reason
            entry['footage_flagged_at'] = time.time()
            deadlines[self._folder_id] = entry
            save_deadlines(deadlines)
        send_discord_ops_channel(embed={
            'title': '🚨 Footage Problem Reported',
            'color': 0xe74c3c,
            'fields': [
                {'name': 'Editor', 'value': entry.get('editor_name') or str(interaction.user), 'inline': True},
                {'name': 'Folder', 'value': f"{entry.get('client_name', '?')} / {entry.get('folder_name', self._folder_id)}", 'inline': True},
                {'name': 'Problem', 'value': reason, 'inline': False},
            ],
            'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        })
        # Also notify the creator so they can fix/re-upload the footage
        creator_notified = False
        if entry.get('client_name'):
            creator_notified = await handle_creator_footage_notify(
                entry['client_name'], entry.get('folder_name', self._folder_id),
                reason, entry.get('editor_name'),
            )
        note = ' The creator has also been notified.' if creator_notified else ''
        await interaction.followup.send(
            '✅ Reported to ops — pickup reminders for this folder are paused until it\'s sorted.' + note,
            ephemeral=True,
        )
        logger.info(f'footage problem reported for {self._folder_id} by {interaction.user} (creator_notified={creator_notified})')


class StartAssignmentView(discord.ui.View):
    """Persistent ▶️ Start / ⚠️ footage buttons on assignment embeds and pickup
    reminders. Stateless — callbacks re-load the deadlines entry fresh, so the view
    survives restarts (re-registered in on_ready) and stale clicks are harmless.
    started=True builds the post-start variant (footage button only)."""

    def __init__(self, folder_id, started=False):
        super().__init__(timeout=None)
        self._folder_id = folder_id
        if not started:
            start_btn = discord.ui.Button(
                label='▶️ Start', style=discord.ButtonStyle.green,
                custom_id=f'start_folder_{folder_id}'[:100],
            )
            start_btn.callback = self._on_start
            self.add_item(start_btn)
        footage_btn = discord.ui.Button(
            label='⚠️ Problem with footage', style=discord.ButtonStyle.grey,
            custom_id=f'footage_problem_{folder_id}'[:100],
        )
        footage_btn.callback = self._on_footage
        self.add_item(footage_btn)

    async def _on_start(self, interaction: discord.Interaction):
        await _start_folder_clicked(interaction, self._folder_id, source_message=interaction.message)

    async def _on_footage(self, interaction: discord.Interaction):
        await interaction.response.send_modal(FootageProblemModal(self._folder_id))


async def _apply_started_embed(message, entry, folder_id):
    """Edits the assignment message in place: title → In Progress, deadline field →
    live countdown, Start button removed (footage button stays). Best-effort."""
    try:
        if not message.embeds:
            return
        embed = message.embeds[0]
        embed.title = re.sub(r'^📁 New Assignment|^🔁 Reassigned to You', '🎬 In Progress', embed.title or '🎬 In Progress')
        embed.color = discord.Color.green()
        due_ts = entry.get('due_ts')
        due_val = f'<t:{int(due_ts)}:F> (<t:{int(due_ts)}:R>)' if due_ts else '♾️ No deadline'
        replaced = False
        for i, f in enumerate(embed.fields):
            if f.name == 'Deadline':
                embed.set_field_at(i, name='Deadline', value=due_val, inline=False)
                replaced = True
                break
        if not replaced:
            embed.add_field(name='Deadline', value=due_val, inline=False)
        started_at, assigned_at = entry.get('started_at'), entry.get('assigned_at')
        if started_at and assigned_at:
            pickup_h = round((started_at - assigned_at) / 3600, 1)
            embed.add_field(name='Started', value=f'<t:{int(started_at)}:f> · picked up after {pickup_h}h', inline=False)
        await message.edit(embed=embed, view=StartAssignmentView(folder_id, started=True))
    except Exception as e:
        logger.warning(f'_apply_started_embed failed: {e}')


async def _apply_unstarted_embed(message, folder_id):
    """Reverse of _apply_started_embed: title back to New Assignment, deadline
    field back to pending-start copy, Started field dropped, ▶️ Start button
    restored, message re-pinned. Best-effort."""
    try:
        if not message.embeds:
            return
        embed = message.embeds[0]
        embed.title = re.sub(r'^🎬 In Progress', '📁 New Assignment', embed.title or '📁 New Assignment')
        embed.color = discord.Color.blurple()
        for i, f in enumerate(embed.fields):
            if f.name == 'Deadline':
                embed.set_field_at(
                    i, name='Deadline',
                    value='⏸️ Starts when you press ▶️ Start (24h from then)', inline=False)
                break
        for i in range(len(embed.fields) - 1, -1, -1):
            if embed.fields[i].name == 'Started':
                embed.remove_field(i)
        await message.edit(embed=embed, view=StartAssignmentView(folder_id, started=False))
        try:
            await message.pin()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f'_apply_unstarted_embed failed: {e}')


async def _unstart_folder_clicked(interaction, folder_id):
    """Team-only undo for a misclicked ▶️ Start: back to the pending-start
    state (no deadline until Start is pressed again). assigned_at is kept so
    pickup tracking stays honest. Idempotent — an un-started folder reports
    nothing to undo."""
    await interaction.response.defer(ephemeral=True)
    deadlines = load_deadlines()
    entry = deadlines.get(folder_id)
    if not entry or entry.get('pending_start'):
        await interaction.followup.send(
            'This folder is not started — nothing to undo.', ephemeral=True)
        return

    undone_by   = interaction.user.display_name
    editor_name = entry.get('editor_name', '?')
    reset_start_state(entry)
    deadlines[folder_id] = entry
    save_deadlines(deadlines)
    logger.info(f'unstart: {folder_id} reverted to pending-start by {undone_by}')

    # Put the ▶️ Start button back on the assignment message
    rec = load_assignment_messages().get(folder_id, {})
    if rec.get('message_id') and rec.get('channel_id'):
        try:
            ch  = bot.get_channel(int(rec['channel_id'])) or await bot.fetch_channel(int(rec['channel_id']))
            msg = await ch.fetch_message(int(rec['message_id']))
            await _apply_unstarted_embed(msg, folder_id)
        except Exception as e:
            logger.warning(f'unstart: could not restore assignment message for {folder_id}: {e}')

    send_discord_ops_channel(embed={
        'title': '↩️ Start Undone',
        'color': 0xe67e22,
        'fields': [
            {'name': 'Editor', 'value': editor_name, 'inline': True},
            {'name': 'Folder', 'value': entry.get('folder_name', folder_id), 'inline': True},
            {'name': 'By', 'value': undone_by, 'inline': True},
        ],
    })
    await interaction.followup.send(
        '↩️ Undone — back to pending-start, no deadline. The ▶️ Start button '
        'is live again on the assignment message.', ephemeral=True)


async def _start_folder_clicked(interaction, folder_id, source_message=None):
    """Shared Start path for the button and the /start command. Idempotent: a
    double-click or a click after reassign gets an ephemeral notice, never a
    second timer."""
    await interaction.response.defer(ephemeral=True)
    loop  = asyncio.get_event_loop()
    entry = load_deadlines().get(folder_id)

    if not entry or not entry.get('pending_start'):
        await interaction.followup.send(
            'This folder is already started (or no longer being tracked) — no action needed.',
            ephemeral=True,
        )
        return

    # Ownership check: the clicker must be in the assigned editor's channel (or Team).
    is_team = any(r.name == 'Team' for r in getattr(interaction.user, 'roles', []))
    ch_editor, _ = await loop.run_in_executor(None, fetch_editor_by_channel_id, interaction.channel_id)
    if not is_team and ch_editor and entry.get('editor_name') and ch_editor != entry.get('editor_name'):
        await interaction.followup.send(
            '🚫 This folder is no longer assigned to you.', ephemeral=True,
        )
        return

    entry = mark_folder_started(folder_id)
    if not entry:  # race with another click
        await interaction.followup.send('Already started — the clock is running.', ephemeral=True)
        return

    # Update + unpin the assignment message
    msg = source_message
    if msg is None:
        rec = load_assignment_messages().get(folder_id, {})
        if rec.get('message_id') and rec.get('channel_id'):
            try:
                ch  = bot.get_channel(int(rec['channel_id'])) or await bot.fetch_channel(int(rec['channel_id']))
                msg = await ch.fetch_message(int(rec['message_id']))
            except Exception as e:
                logger.warning(f'start: could not fetch assignment message for {folder_id}: {e}')
    if msg is not None:
        await _apply_started_embed(msg, entry, folder_id)
        try:
            await msg.unpin()
        except Exception:
            pass

    due_ts = entry.get('due_ts')
    due_str = f'<t:{int(due_ts)}:F> (<t:{int(due_ts)}:R>)' if due_ts else 'no deadline (indefinite)'
    await interaction.followup.send(f'▶️ Started! Your deadline is {due_str}.', ephemeral=True)

    pickup_h = None
    if entry.get('assigned_at') and entry.get('started_at'):
        pickup_h = round((entry['started_at'] - entry['assigned_at']) / 3600, 1)
    send_discord_ops_channel(embed={
        'title': '▶️ Editing Started',
        'color': 0x2ecc71,
        'fields': [
            {'name': 'Editor', 'value': entry.get('editor_name', '?'), 'inline': True},
            {'name': 'Folder', 'value': f"{entry.get('client_name', '?')} / {entry.get('folder_name', '?')}", 'inline': True},
            {'name': 'Pickup', 'value': f'{pickup_h}h' if pickup_h is not None else '—', 'inline': True},
        ],
        'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    })
    if entry.get('client_name'):
        vc = load_assignment_messages().get(folder_id, {}).get('video_count', 0)
        asyncio.get_event_loop().create_task(handle_creator_start_notify(
            entry['client_name'], entry.get('folder_name', ''),
            entry.get('editor_name', ''), vc,
        ))
    logger.info(f"start: {entry.get('folder_name')} started by {entry.get('editor_name')} (pickup {pickup_h}h)")


async def assign_folder(
    client_name: str,
    folder_name: str,
    video_count: int,
    folder_id: str,
    editor_name: str,
    notion_queue_page_id: str = None,
    project_number: str = '',
    is_reassign: bool = False,
    from_dashboard: bool = False,
):
    """Send assignment notification embed to the editor's channel; immediately set In Progress."""
    editors = fetch_editors_from_notion()
    info = editors.get(editor_name)
    if not info:
        logger.error(f'Editor not found in Notion: {editor_name}')
        return
    if not info.get('discord_channel_id'):
        logger.error(f'No Discord Channel ID configured for editor: {editor_name}')
        return

    try:
        channel_id = int(info['discord_channel_id'])
    except ValueError:
        logger.error(f'Bad Discord Channel ID for {editor_name}: {info["discord_channel_id"]}')
        return

    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f'Cannot reach channel {channel_id}: {e}')
            return

    config = load_config()
    token  = config['notion_token']
    if notion_queue_page_id:
        update_active_queue_status(token, notion_queue_page_id, 'In Progress')
    elif folder_id:
        # No page id means nobody has PATCHed the Active Queue row yet. The
        # real assign paths (/assign, AssignEditorSelect) call
        # _assign_raw_to_editor() first and hand us the page id they got back;
        # a dashboard assign arrives straight off the outbox with no page id at
        # all, so without this the row keeps its old Editor (or none) while the
        # editor has already been pinged on Discord. /stats reads Notion, so
        # the folder simply never appears for them — exactly the bulk-assign
        # trap in CLAUDE.md, reached this time through the bridge.
        notion_queue_page_id = _assign_raw_to_editor(token, folder_id, editor_name)
        if not notion_queue_page_id:
            logger.warning(
                f'assign_folder: no Active Queue row for folder_id={folder_id!r} '
                f'({folder_name}) — Editor not set, {editor_name} will be missing '
                f'it in /stats'
            )
    recalculate_active_videos(token, editor_name)

    if folder_id:
        deadlines = load_deadlines()
        entry = deadlines.get(folder_id, {})
        # Every (re)assignment enters the pending-start state: no deadline until
        # the editor presses ▶️ Start, at which point due_ts = started_at + 24h.
        # The pickup ladder in deadline_checker() covers the gap. 'indefinite' is
        # preserved by reset_start_state — Start won't put a clock on those.
        reset_start_state(entry)
        entry['editor_name']   = editor_name
        entry['client_name']   = client_name
        entry['folder_name']   = folder_name
        entry['notion_page_id'] = notion_queue_page_id
        entry['assigned_at']   = time.time()  # resets on reassign — measures current editor's turnaround
        deadlines[folder_id]   = entry
        save_deadlines(deadlines)

    # Fetch Drive links top-down (non-blocking)
    loop = asyncio.get_event_loop()
    client_folder_link, raw_footage_link = await loop.run_in_executor(
        None, find_assignment_drive_links, client_name, folder_name
    )

    pnum        = project_number or get_project_number(folder_id)
    base_title  = '🔁 Reassigned to You' if is_reassign else '📁 New Assignment'
    title       = f'{base_title}  {pnum}' if pnum else base_title
    embed       = discord.Embed(title=title, color=discord.Color.orange() if is_reassign else discord.Color.blue())
    embed.add_field(name='Client', value=client_name, inline=False)
    embed.add_field(name='Folder', value=folder_name, inline=False)
    embed.add_field(name='Videos', value=str(video_count), inline=False)
    embed.add_field(name='Deadline', value='⏸️ Timer starts when you press ▶️ Start (24h from then)', inline=False)
    if client_folder_link or raw_footage_link:
        link_parts = []
        if client_folder_link:
            link_parts.append(f"📂 [Client Folder]({client_folder_link})")
        if raw_footage_link:
            link_parts.append(f"📁 [Raw Footage Folder]({raw_footage_link})")
        embed.add_field(name='Drive Links', value='\n'.join(link_parts), inline=False)
    embed.set_footer(text='⚠️ More videos may be added — you\'ll be notified if count increases.')

    # On reassign, kill the old editor's live Start button + pin before the
    # assignment_messages entry gets overwritten below.
    if is_reassign and folder_id:
        old_rec = load_assignment_messages().get(folder_id, {})
        if old_rec.get('message_id') and old_rec.get('channel_id'):
            try:
                old_ch  = bot.get_channel(int(old_rec['channel_id'])) or await bot.fetch_channel(int(old_rec['channel_id']))
                old_msg = await old_ch.fetch_message(int(old_rec['message_id']))
                await old_msg.edit(view=None)
                try:
                    await old_msg.unpin()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f'assign_folder: old assignment message cleanup failed for {folder_id}: {e}')

    user_id = info.get('discord_user_id', '')
    content = f"<@{user_id}>" if user_id else None
    sent    = await ch.send(content=content, embed=embed,
                            view=StartAssignmentView(folder_id) if folder_id else None)
    # Assignment messages are no longer pinned — pins cluttered the editor
    # channels. The Start button + /start command are how folders get started;
    # nags carry a jump link back to this message.

    if folder_id:
        save_assignment_message(folder_id, {
            'message_id': sent.id,
            'channel_id': channel_id,
            'client_name': client_name,
            'folder_name': folder_name,
            'video_count': video_count,
        })

    pending_assignments[sent.id] = {
        'client_name':          client_name,
        'folder_name':          folder_name,
        'video_count':          video_count,
        'folder_id':            folder_id,
        'editor_name':          editor_name,
        'editor_page_id':       info.get('page_id'),
        'editor_user_id':       info.get('discord_user_id', ''),
        'notion_queue_page_id': notion_queue_page_id,
        'project_number':       pnum,
        'status':               'in_progress',
        'channel_id':           channel_id,
        'discord_message_id':   sent.id,
    }

    send_discord_ops_channel(embed={
        'title': '📁 Assignment',
        'color': 0x3498db,
        'fields': [
            {'name': 'Editor',  'value': editor_name,  'inline': True},
            {'name': 'Client',  'value': client_name,  'inline': True},
            {'name': 'Folder',  'value': folder_name,  'inline': True},
        ],
        'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    })
    logger.info(f'Assignment sent: {folder_name} → {editor_name} (channel {channel_id})')

    # Mirror into the Creator Collective dashboard (no-op unless dashboard_url
    # + dashboard_secret exist in config.json). Discord stays the source of
    # truth — a dead dashboard only logs a warning. Skipped when the assignment
    # came FROM the dashboard, which already has it.
    if folder_id and not from_dashboard:
        creator_channel_id, creator_discord_id = await loop.run_in_executor(
            None, fetch_creator_discord_info, client_name
        )
        folder_created_at, drive_folder_name = await loop.run_in_executor(
            None, get_folder_drive_meta, folder_id
        )
        dashboard_payload = {
            'folder_id':          folder_id,
            'folder_name':        folder_name,
            'creator_name':       client_name,
            'editor_name':        editor_name,
            'editor_discord_id':  str(info.get('discord_user_id', '')),
            'video_count':        video_count,
            'raw_footage_link':   raw_footage_link or '',
            'client_folder_link': client_folder_link or '',
            'project_number':     pnum or '',
            'is_reassign':        bool(is_reassign),
        }
        if creator_channel_id:
            dashboard_payload['creator_channel_id'] = creator_channel_id
        if creator_discord_id:
            dashboard_payload['creator_discord_id'] = creator_discord_id
        if folder_created_at:
            dashboard_payload['folder_created_at'] = folder_created_at
        if drive_folder_name:
            dashboard_payload['drive_folder_name'] = drive_folder_name
        await loop.run_in_executor(None, post_dashboard_assignment, dashboard_payload)


# ── Revision assignment ────────────────────────────────────────────────────────

def log_revision_to_notion(client_name, folder_name, folder_id, video_count,
                            editor_name, notes, notion_queue_page_id):
    """Writes one row to the Revision Log Notion DB. Fire-and-forget; logs on failure."""
    try:
        config = load_config()
        token  = config['notion_token']

        raw_url = f'https://drive.google.com/drive/folders/{folder_id}' if folder_id else None

        # Resolve client root + edited folder. If cache is cold, do a live top-down lookup
        # rather than relying on find_client_edited_folder_id's fallback (which uses
        # files.get(parents) — broken on this Shared Drive).
        client_root_id = _client_root_folder_cache.get(client_name, '')
        edited_id      = _client_edited_folder_cache.get(client_name, '')
        if not client_root_id:
            service = get_drive_service()
            client_root_id, edited_id = _find_edited_folder_top_down(service, client_name)
            client_root_id = client_root_id or ''
            edited_id      = edited_id or ''
        elif not edited_id:
            edited_id = find_client_edited_folder_id(client_name) or ''

        edited_url = f'https://drive.google.com/drive/folders/{edited_id}' if edited_id else None
        client_url = f'https://drive.google.com/drive/folders/{client_root_id}' if client_root_id else None
        queue_url  = f'https://notion.so/{notion_queue_page_id.replace("-", "")}' if notion_queue_page_id else None

        now_iso = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

        properties = {
            'Folder Name': {'title': [{'text': {'content': folder_name}}]},
            'Creator':     {'rich_text': [{'text': {'content': client_name}}]},
            'Editor':      {'select': {'name': editor_name}},
            'Revision Notes': {'rich_text': [{'text': {'content': notes or ''}}]},
            'Date':        {'date': {'start': now_iso}},
            'Video Count': {'number': video_count},
        }
        if raw_url:
            properties['Raw Footage Folder'] = {'url': raw_url}
        if edited_url:
            properties['Edited Folder'] = {'url': edited_url}
        if client_url:
            properties['Client Folder'] = {'url': client_url}
        if queue_url:
            properties['Active Queue Link'] = {'url': queue_url}

        resp = requests.post(
            'https://api.notion.com/v1/pages',
            headers=notion_headers(token),
            json={'parent': {'database_id': REVISION_LOG_DB}, 'properties': properties},
            timeout=15,
        )
        if not resp.ok:
            logger.error(f'log_revision_to_notion: {resp.status_code} {resp.text[:200]}')
        else:
            logger.info(f'log_revision_to_notion: logged {folder_name} / {editor_name}')
    except Exception as e:
        logger.error(f'log_revision_to_notion: exception: {e}')


async def open_revision_assignment(client_name, folder_name, folder_id, video_count,
                                    editor_name, editor_info, notion_queue_page_id,
                                    notes: str = '', from_dashboard: bool = False):
    """Sends a revision assignment embed to the editor's Discord channel."""
    # Keep the dashboard ticket in step — unless the request came from there.
    if folder_id and not from_dashboard:
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: post_dashboard_status(
                folder_id, 'revisions',
                editor_name=editor_name,
                note=(notes or '')[:500],
            )
        )
    # The books close FIRST, then we try to reach the editor (2026-08-20).
    # This used to run the other way round: every channel problem — an editor
    # with no discord_channel_id on their Notion row, a bad id, a channel the
    # bot can't see — returned before any of it, so Active Queue never flipped
    # to Revision, the revisions counter never ticked, and nothing landed in the
    # Revision Log. A round that the creator asked for and the site recorded
    # simply never happened as far as Notion was concerned. Telling the editor
    # is best-effort; the record is not.
    config = load_config()
    token = config['notion_token']
    update_active_queue_status(token, notion_queue_page_id, 'Revision')
    recalculate_active_videos(token, editor_name)
    increment_editor_counter(editor_name, 'revisions')

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, log_revision_to_notion,
                         client_name, folder_name, folder_id, video_count,
                         editor_name, notes, notion_queue_page_id)

    async def _unreachable(why):
        """Books are already written; the person is not. Escalate so someone
        tells them by hand rather than letting the round go quiet."""
        logger.error(f'open_revision_assignment: {why}')
        send_discord_ops_channel(
            f'⚠️ Revision opened for **{client_name} / {folder_name}** '
            f'({video_count} videos) but **{editor_name}** could not be reached: {why}. '
            f'Notion is updated — tell them by hand.'
            + (f'\n**Notes:** {notes[:400]}' if notes else '')
        )

    ch_id_str = editor_info.get('discord_channel_id', '')
    if not ch_id_str:
        await _unreachable(f'no Discord channel on their Notion row')
        return
    try:
        ch_id = int(ch_id_str)
    except ValueError:
        await _unreachable(f'bad channel ID {ch_id_str!r}')
        return

    ch = bot.get_channel(ch_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(ch_id)
        except Exception as e:
            await _unreachable(f'cannot reach channel {ch_id}: {e}')
            return

    embed = discord.Embed(title='🔄 Revision Request', color=discord.Color.orange())
    embed.add_field(name='Client', value=client_name, inline=False)
    embed.add_field(name='Folder', value=folder_name, inline=False)
    embed.add_field(name='Videos', value=str(video_count), inline=False)
    if notes:
        embed.add_field(name='Revision Notes', value=notes, inline=False)
    embed.set_footer(text='Changes requested. Please re-edit and /complete when done.')

    user_id = editor_info.get('discord_user_id', '')
    content = f"<@{user_id}>" if user_id else None
    sent = await ch.send(content=content, embed=embed)

    if folder_id:
        save_assignment_message(folder_id, {
            'message_id': sent.id,
            'channel_id': ch_id,
            'client_name': client_name,
            'folder_name': folder_name,
            'video_count': video_count,
        })

    pending_assignments[sent.id] = {
        'client_name':          client_name,
        'folder_name':          folder_name,
        'video_count':          video_count,
        'folder_id':            folder_id,
        'editor_name':          editor_name,
        'editor_page_id':       editor_info.get('page_id'),
        'editor_user_id':       editor_info.get('discord_user_id', ''),
        'notion_queue_page_id': notion_queue_page_id,
        'status':               'revision',
        'channel_id':           ch_id,
        'discord_message_id':   sent.id,
        'is_revision':          True,
    }
    logger.info(f'Revision assignment sent: {folder_name} → {editor_name} (channel {ch_id})')


# ── Folder update message ──────────────────────────────────────────────────────

FOLDER_UPDATE_MSGS_FILE = os.path.join(BASE_DIR, 'folder_update_msgs.json')
FOLDER_UPDATE_EDIT_WINDOW = 6 * 3600  # keep editing the same message for this long


def _load_folder_update_msgs():
    try:
        with open(FOLDER_UPDATE_MSGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_folder_update_msgs(data):
    with open(FOLDER_UPDATE_MSGS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


async def send_folder_update_msg(item):
    """Notifies the assigned editor that a folder gained videos. Rather than
    stacking a new message per count change (uploads land in waves — one folder
    can churn out 5+ pings in 20 minutes), each folder keeps ONE running update
    message that gets edited in place with the cumulative count; a fresh message
    is only sent when there's no recent one to edit."""
    editors = fetch_editors_from_notion()
    editor_name = item['editor_name']
    info = editors.get(editor_name)
    if not info:
        logger.info(f'Skipping folder update for inactive/unknown editor {editor_name}')
        return
    if not info.get('discord_channel_id'):
        logger.error(f'Cannot send update: no Discord channel for {editor_name}')
        return
    try:
        channel_id = int(info['discord_channel_id'])
    except ValueError:
        logger.error(f'Bad Discord Channel ID for {editor_name}: {info["discord_channel_id"]}')
        return
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.error(f'Cannot reach channel {channel_id}: {e}')
            return

    folder_id = item.get('folder_id', '')
    now = time.time()
    cache = _load_folder_update_msgs()
    entry = cache.get(folder_id)

    # Reuse the existing message when it's recent and in the same channel
    if (entry and entry.get('channel_id') == channel_id
            and now - entry.get('updated_at', 0) < FOLDER_UPDATE_EDIT_WINDOW):
        first_count = entry.get('first_count', item['previous_count'])
        total_added = item['new_count'] - first_count
        embed = discord.Embed(
            title=f"📥 Folder Updated — {item['client_name']} / {item['folder_name']}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name='Videos', value=f"{first_count} → {item['new_count']} (+{total_added} added — still uploading)", inline=False)
        embed.add_field(name='Action', value='Please check the folder for new files.', inline=False)
        try:
            old = await ch.fetch_message(entry['message_id'])
            await old.edit(content=None, embed=embed)
            entry['updated_at'] = now
            cache[folder_id] = entry
            _save_folder_update_msgs(cache)
            return
        except Exception as e:
            logger.info(f'folder update: could not edit previous message, sending new ({e})')

    diff = item.get('diff', item['new_count'] - item['previous_count'])
    embed = discord.Embed(
        title=f"📥 Folder Updated — {item['client_name']} / {item['folder_name']}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name='Videos', value=f"{item['previous_count']} → {item['new_count']} (+{diff} added)", inline=False)
    embed.add_field(name='Action', value='Please check the folder for new files.', inline=False)
    sent = await ch.send(embed=embed)
    if folder_id:
        cache[folder_id] = {
            'message_id':  sent.id,
            'channel_id':  channel_id,
            'first_count': item['previous_count'],
            'updated_at':  now,
        }
        _save_folder_update_msgs(cache)
    logger.info(f"Update sent to {editor_name}: {item['folder_name']} {item['previous_count']}→{item['new_count']}")


# ── Queue poller: IPC from notion_bridge.py ────────────────────────────────────

async def handle_dashboard_reassign(item):
    """Reassign a folder to a new editor (triggered from dashboard)."""
    client_name    = item['client_name']
    folder_name    = item['folder_name']
    folder_id      = item.get('folder_id', '')
    video_count    = item.get('video_count', 0)
    old_editor     = item.get('old_editor', '')
    new_editor     = item['new_editor']
    notion_page_id = item.get('notion_page_id', '')
    project_number = item.get('project_number', '')

    config = load_config()
    token  = config['notion_token']
    loop   = asyncio.get_event_loop()

    # Update Notion row
    _notion_patch(token, notion_page_id, {
        'Editor': {'select': {'name': new_editor}},
        'Status': {'select': {'name': 'In Progress'}},
    })

    # Send new assignment embed to new editor
    await assign_folder(client_name, folder_name, video_count, folder_id, new_editor,
                        notion_page_id, project_number, is_reassign=True)

    # Update deadline to new editor (falls back to notion_page_id match if folder_id is blank)
    update_deadline_editor(folder_id, notion_page_id, new_editor)

    # Notify old editor + creator
    await handle_reassign_notify({
        'client_name': client_name, 'folder_name': folder_name,
        'old_editor': old_editor, 'new_editor': new_editor,
    })

    # Recalculate both editors
    for editor in {old_editor, new_editor}:
        if editor:
            await loop.run_in_executor(None, recalculate_active_videos, token, editor)

    logger.info(f'dashboard_reassign: {client_name}/{folder_name} {old_editor} → {new_editor}')


async def handle_dashboard_revision(item):
    """Open a revision for a folder (triggered from dashboard)."""
    client_name    = item['client_name']
    folder_name    = item['folder_name']
    folder_id      = item.get('folder_id', '')
    video_count    = item.get('video_count', 0)
    editor_name    = item['editor_name']
    notes          = item.get('notes', '')
    notion_page_id = item.get('notion_page_id', '')

    loop     = asyncio.get_event_loop()
    editors  = await loop.run_in_executor(None, fetch_editors_from_notion)
    editor_info = editors.get(editor_name)
    if not editor_info:
        logger.error(f'handle_dashboard_revision: editor {editor_name!r} not found')
        return

    await open_revision_assignment(client_name, folder_name, folder_id, video_count,
                                   editor_name, editor_info, notion_page_id, notes,
                                   from_dashboard=bool(item.get('from_dashboard')))
    logger.info(f'dashboard_revision: {client_name}/{folder_name} → {editor_name}')


async def _editor_channel(editor_name, context, email='', discord_user_id=''):
    """The editor's private Discord channel, or None (logged) if unreachable.

    Resolves on EMAIL first, then Discord user id, and only then the name.

    The name was the sole key until 2026-08-19, and it silently lost four of
    sixteen editors: the dashboard knows them as Jermaine, ronruzzelv, Ysabel
    and Zyon Kahili, and this database calls them Josh, Ron, Ysa and Zyon.
    Every message for those four missed their channel and fell through to a DM
    nobody read — including clock in/out pings, for weeks, for two people who
    aren't even new. Renaming someone on either side did that, quietly, with a
    warning in a log nobody was watching.

    Email is the key that actually holds: all sixteen rows carry one and every
    one matches the dashboard's. The Discord id is the same idea one step down.
    The name stays last so an editor with neither still resolves, but it is now
    the fallback rather than the contract."""
    loop = asyncio.get_event_loop()
    editors = await loop.run_in_executor(None, fetch_editors_from_notion)

    row, matched_on = None, ''
    want_email = (email or '').strip().lower()
    if want_email:
        hits = [r for r in editors.values() if r.get('email') == want_email]
        # Exactly one, or it isn't an answer — two rows sharing an address is
        # the ambiguity this is meant to remove, not silently pick through.
        if len(hits) == 1:
            row, matched_on = hits[0], 'email'
    if row is None and str(discord_user_id or '').strip():
        want_uid = str(discord_user_id).strip()
        hits = [r for r in editors.values() if str(r.get('discord_user_id') or '').strip() == want_uid]
        if len(hits) == 1:
            row, matched_on = hits[0], 'discord id'
    if row is None and editor_name:
        row, matched_on = editors.get(editor_name), 'name'

    ch_id_str = (row or {}).get('discord_channel_id', '')
    if not ch_id_str:
        logger.warning(
            f'{context}: no Discord channel for {editor_name!r} '
            f'(email={want_email or "-"}, discord_id={discord_user_id or "-"})'
        )
        return None
    if matched_on != 'name':
        logger.info(f'{context}: resolved {editor_name!r} by {matched_on}')
    try:
        return bot.get_channel(int(ch_id_str)) or await bot.fetch_channel(int(ch_id_str))
    except Exception as e:
        logger.error(f'{context}: channel {ch_id_str} unreachable: {e}')
        return None


async def handle_cc_dashboard_notify(item):
    """A ticket created in the dashboard (no Drive folder) was assigned to an
    editor. There's nothing for the Notion / /complete flow to work on, so this
    is a pure heads-up embed pointing back at the dashboard, where the editor
    picks up the files and delivers.

    A backfill notify (item['backfill']) is different: it's mirroring a batch
    the editor has already been holding since before dashboard_batches.json
    tracked website tickets — not a new or reassigned pickup, and the editor
    was already pinged for real when it actually happened. Rendering it as
    "New batch" / "Reassigned to you" would read as a duplicate assignment,
    and re-pinging the creator (who was already notified the first time) would
    be a second, unearned notification — so backfill skips both and shows a
    quieter "already tracked" embed with how long it's actually been sitting."""
    editor_name = item.get('editor_name', '')
    count = item.get('video_count') or 0
    backfill = bool(item.get('backfill'))
    upsert_active_dashboard_batch(item)
    loop   = asyncio.get_event_loop()
    config = load_config()
    token  = config['notion_token']
    if editor_name:
        await loop.run_in_executor(None, recalculate_active_videos, token, editor_name)
    prev_editor = item.get('previous_editor_name', '')
    if prev_editor and prev_editor != editor_name:
        await loop.run_in_executor(None, recalculate_active_videos, token, prev_editor)
    if backfill:
        embed = discord.Embed(
            title='📋 Now tracked in /stats',
            description=item.get('folder_name') or 'Untitled batch',
            colour=0x99AAB5,
        )
    else:
        embed = discord.Embed(
            title='🆕 Reassigned to you' if item.get('is_reassign') else '🆕 New batch',
            description=item.get('folder_name') or 'Untitled batch',
            colour=0x5865F2,
        )
    embed.add_field(name='Creator', value=creator_label(item), inline=True)
    if item.get('client_name'):
        embed.add_field(name='Brand', value=item['client_name'], inline=True)
    if count:
        embed.add_field(name='Videos', value=str(count), inline=True)
    if item.get('formats'):
        embed.add_field(name='Type', value=item['formats'], inline=False)
    if backfill:
        assigned_ts = _parse_dashboard_assigned_at(item.get('assigned_at'))
        embed.add_field(name='Assigned', value=f'<t:{int(assigned_ts)}:R>', inline=False)
    chat = creator_chat_link(item)
    if chat:
        embed.add_field(name='Their chat', value=chat, inline=False)
    if item.get('ticket_url'):
        embed.add_field(
            name='Where',
            value=f"[Open the batch]({item['ticket_url']})\nFiles and delivery live there — no /complete on this one.",
            inline=False,
        )
    # The editor's channel. Falling back to a DM matters: an editor who works
    # off the dashboard may have no Notion row at all, and before this they
    # simply never heard about the batch.
    channel = await _editor_channel(
        editor_name, 'cc_dashboard_notify',
        email=item.get('editor_email', ''),
        discord_user_id=item.get('editor_discord_id', ''),
    )
    if channel:
        await channel.send(embed=embed)
    else:
        dm = await _dm_channel(item.get('editor_discord_id'), 'cc_dashboard_notify')
        if dm:
            await dm.send(embed=embed)
        else:
            logger.warning(f'cc_dashboard_notify: nowhere to reach editor {editor_name!r}')

    if not backfill:
        # Tell the creator their batch got picked up — the Drive path has done
        # this forever via handle_creator_notify, the dashboard path never did.
        # Skipped on backfill: the creator was already told the first time.
        await _notify_dashboard_creator(item, editor_name)
        if item.get('is_reassign'):
            # Covers the outgoing editor for a website-batch reassign, whether
            # it was triggered from the dashboard UI or from Discord's own
            # /reassign (ReassignEditorSelect posts to the dashboard and never
            # enqueues its own old-editor ping for ticket_id batches — this is
            # the one code path both funnel through).
            await _notify_previous_editor(item)
    logger.info(
        f"cc_dashboard_notify: {item.get('folder_name')} → {editor_name}"
        + (' (backfill)' if backfill else '')
    )


async def handle_cc_dashboard_delivered(item):
    """A website-native batch (no Drive folder, no Notion row) was delivered on
    the dashboard. Mirrors the counter-increment half of finalize_delivery /
    _finalize_va_approval so these video counts land in the same Editor
    Profiles fields /stats already reads — otherwise a dashboard-only editor's
    delivered counts never move no matter how much they ship."""
    editor_name = item.get('editor_name', '')
    video_count = item.get('video_count') or 0
    batch, already_delivered = mark_dashboard_batch_delivered(item)
    if not batch:
        # Only reachable now if the write itself failed; the untracked case
        # records the batch and credits it.
        logger.warning(
            f"cc_dashboard_delivered: could not record "
            f"{item.get('folder_name')!r} / {editor_name!r} "
            f"(ticket_id={item.get('ticket_id')!r}) — stats not credited"
        )
        return
    if already_delivered:
        logger.info(
            f"cc_dashboard_delivered: ticket {item.get('ticket_id')!r} was "
            f"already delivered — treating as a resent/retried command, "
            f"not crediting stats again"
        )
        return
    if video_count <= 0:
        return
    loop   = asyncio.get_event_loop()
    config = load_config()
    token  = config['notion_token']
    editors     = await loop.run_in_executor(None, fetch_editors_from_notion)
    editor_info = editors.get(editor_name)
    if not editor_info:
        logger.warning(f"cc_dashboard_delivered: editor {editor_name!r} not found in Notion, stats not updated")
        return
    editor_page_id = editor_info.get('page_id')
    page  = _notion_get(token, editor_page_id)
    props = page.get('properties', {}) if page else {}
    week  = props.get('Delivered This Week',    {}).get('number') or 0
    month = props.get('Delivered This Month',   {}).get('number') or 0
    total = props.get('Total Videos Delivered', {}).get('number') or 0
    _notion_patch(token, editor_page_id, {
        'Delivered This Week':    {'number': week  + video_count},
        'Delivered This Month':   {'number': month + video_count},
        'Total Videos Delivered': {'number': total + video_count},
    })
    # The batch just left the active set (mark_dashboard_batch_delivered
    # flipped its status) — Active Videos would otherwise keep counting it.
    await loop.run_in_executor(None, recalculate_active_videos, token, editor_name)
    logger.info(f"cc_dashboard_delivered: {editor_name} +{video_count} (batch={item.get('folder_name')!r})")


async def handle_cc_dashboard_reopen(item):
    """A delivered website batch went back for changes. Flips it back to
    active in dashboard_batches.json — the counterpart to the dedupe check in
    handle_cc_dashboard_delivered, so the next 'delivered' for this ticket_id
    credits instead of being logged as a retry. Doesn't touch the *delivered
    video* counters (Delivered This Week/Month/Total): the first round was
    real delivered work, reopening it for revisions doesn't undo that credit.

    It does bump the editor's 'revisions' performance counter
    (editor_counters.json) — a Drive-origin revision does this via
    open_revision_assignment(), and a website-native revision was silently
    skipping it, so an editor's reliability stats never reflected revisions
    that only ever happened on the dashboard side."""
    batch = reopen_dashboard_batch(item)
    if not batch:
        logger.info(
            f"cc_dashboard_reopen: ticket {item.get('ticket_id')!r} "
            f"({item.get('folder_name')!r}) wasn't in delivered state — no-op"
        )
        return
    increment_editor_counter(item.get('editor_name') or batch.get('editor_name', ''), 'revisions')
    logger.info(
        f"cc_dashboard_reopen: {item.get('folder_name')!r} back to active "
        f"for {item.get('editor_name')}"
    )


def creator_chat_link(item):
    """A clickable link to the creator's edits channel, or None.

    Every editor is already in these channels, so pointing at the chat is more
    use than any name: it's where the footage and the back-and-forth already
    live. A full URL rather than a `<#id>` mention — a mention only renders
    inside the channel's own guild, and the assignments channel and the creator
    channels don't have to be in the same server."""
    ch_id = str(item.get('creator_channel_id') or '').strip()
    if not ch_id:
        return None
    try:
        ch = bot.get_channel(int(ch_id))
    except Exception:
        return None
    if ch is None:
        return None
    guild_id = getattr(getattr(ch, 'guild', None), 'id', None)
    if not guild_id:
        return None
    return f'[#{ch.name}](https://discord.com/channels/{guild_id}/{ch.id})'


def creator_label(item):
    """How a creator reads in an embed. Website batches lead with the username
    because names repeat — "Chris" is two students, "Talha" is three rows — and
    the username is the one thing guaranteed unique. Drive batches have no
    dashboard profile behind them, so they keep the Drive folder's client name."""
    name = (item.get('student_name') or '').strip()
    uname = (item.get('student_username') or '').strip()
    if uname and name:
        return f'{name} (@{uname})'
    if uname:
        return f'@{uname}'
    return name or (item.get('client_name') or '').strip() or '—'


async def _creator_channel(item, context):
    """Where to reach the creator: their own edits channel from the dashboard,
    then a DM, then — last resort only — the Notion Creator Assignments row by
    name. Name is last because two students answer to "Chris"."""
    ch_id = str(item.get('creator_channel_id') or '').strip()
    if ch_id:
        try:
            ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
            if ch:
                return ch
        except Exception as e:
            logger.warning(f'{context}: creator channel {ch_id} unreachable: {e}')
    dm = await _dm_channel(item.get('creator_discord_id'), context)
    if dm:
        return dm
    name = item.get('student_name') or item.get('client_name') or ''
    if not name:
        return None
    loop = asyncio.get_event_loop()
    notion_ch, notion_uid = await loop.run_in_executor(None, fetch_creator_discord_info, name)
    if notion_ch:
        try:
            return bot.get_channel(int(notion_ch)) or await bot.fetch_channel(int(notion_ch))
        except Exception as e:
            logger.warning(f'{context}: notion creator channel {notion_ch} unreachable: {e}')
    return await _dm_channel(notion_uid, context)


# A dashboard message is the only copy of what a creator or an editor was
# told, and the dashboard acks the command the moment it lands in
# discord_queue.json — long before delivery — so the site reads `sent`
# whatever happens here. Returning quietly on a dead channel is therefore
# indistinguishable, from every angle anyone can see, from having delivered
# it: that is how the footage report on Kio's "Zo Computer" batch reached
# nobody on 2026-08-19 while the site said it was sent.
#
# So an undeliverable message is never dropped. It retries (the queue loop
# re-appends anything that raises, every 3 s), and once it is plainly not a
# blip a human is handed the whole message in the ops channel to deliver by
# hand. Ten tries is ~30 s: long enough for a reconnect, short enough that
# the escalation still reaches someone while it matters.
MAX_MESSAGE_DELIVERY_ATTEMPTS = 10


class MessageUndeliverable(Exception):
    """Requeue signal — the queue loop re-appends the item and retries it."""


async def _dashboard_message_failed(item, context, reason):
    """Raises to retry; returns only once the message is a human's problem."""
    attempts = int(item.get('attempts') or 0) + 1
    # The queue re-appends THIS dict and json.dumps it, so the count carries
    # across retries and across a redeploy.
    item['attempts'] = attempts
    if attempts < MAX_MESSAGE_DELIVERY_ATTEMPTS:
        raise MessageUndeliverable(f'{context}: {reason} (attempt {attempts})')

    target = item.get('target') or 'creator'
    who = (item.get('editor_name') if target == 'editor' else creator_label(item)) or '—'
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, send_discord_ops_channel, None, {
        'title': "⚠️ Couldn't deliver a dashboard message",
        'description': (
            f'**To ({target}):** {who}\n'
            f'**Why:** {reason}\n\n'
            f"**{item.get('title') or '—'}**\n{item.get('description') or ''}"
        )[:4000],
        'color': 0xe67e22,
        'fields': (
            [{'name': 'Where',
              'value': f"[Open in the dashboard]({item['url']})",
              'inline': False}]
            if item.get('url') else []
        ),
    })
    # That alert is now the only copy of it. If Discord wouldn't take that
    # either, hold the item rather than losing both.
    if not ok:
        raise MessageUndeliverable(
            f'{context}: {reason}, and the ops alert failed too')
    # The site acked this on queueing and has read `sent` ever since. Correct
    # it now that a human has the message — after the ops post, so a dashboard
    # blip can never be what stops the escalation.
    await loop.run_in_executor(
        None, report_dashboard_undelivered, item.get('command_id'), reason)
    logger.error(
        f"{context}: gave up on {item.get('title')!r} after {attempts} tries "
        f'({reason}) — handed to the ops channel')


async def handle_cc_dashboard_message(item):
    """Deliver a dashboard-authored embed to one person. The dashboard decides
    what it says and who it's for; this only resolves the channel."""
    target = item.get('target') or 'creator'
    context = f'cc_dashboard_message({target})'
    if target == 'editor':
        ch = await _editor_channel(
            item.get('editor_name', ''), context,
            email=item.get('editor_email', ''),
            discord_user_id=item.get('editor_discord_id', ''),
        )
        if ch is None:
            ch = await _dm_channel(item.get('editor_discord_id'), context)
    else:
        ch = await _creator_channel(item, context)
    if ch is None:
        await _dashboard_message_failed(item, context, 'no channel or DM to deliver to')
        return

    embed = discord.Embed(
        title=item.get('title') or '—',
        description=item.get('description') or None,
        colour=0x5865F2,
    )
    for f in (item.get('fields') or [])[:10]:
        if f.get('name') and f.get('value'):
            embed.add_field(name=f['name'], value=str(f['value']), inline=bool(f.get('inline')))
    if item.get('url'):
        embed.add_field(name='Where', value=f"[Open in the dashboard]({item['url']})", inline=False)
    # A send that throws used to propagate into the queue loop, which requeues
    # forever with no bound and no alert — a permanently-403'd channel meant a
    # 3-second retry loop until somebody read the log.
    try:
        await ch.send(embed=embed)
    except Exception as e:
        await _dashboard_message_failed(item, context, f'discord refused the send: {e}')
        return
    logger.info(f"{context}: sent {item.get('title')!r}")


async def _dm_channel(user_id_str, context):
    """DM channel for a Discord user id, or None (logged)."""
    uid = str(user_id_str or '').strip()
    if not uid:
        return None
    try:
        user = bot.get_user(int(uid)) or await bot.fetch_user(int(uid))
        return user.dm_channel or await user.create_dm()
    except Exception as e:
        logger.warning(f'{context}: cannot DM {uid}: {e}')
        return None


async def _notify_previous_editor(item):
    """Pings the editor a website batch was just reassigned away from. Gated
    on the discord id being present — never pinged on a name alone, matching
    the inbound bridge's own id-first disambiguation rule (both fields may be
    absent/empty when the dashboard doesn't know the outgoing editor)."""
    prev_name = item.get('previous_editor_name', '')
    prev_id   = item.get('previous_editor_discord_id', '')
    if not prev_id:
        return
    embed = discord.Embed(title='📢 Batch Reassigned', color=discord.Color.orange())
    embed.add_field(name='Batch', value=item.get('folder_name') or 'Untitled batch', inline=False)
    embed.add_field(name='Reassigned To', value=item.get('editor_name') or '—', inline=False)
    dest = (await _editor_channel(prev_name, '_notify_previous_editor', discord_user_id=prev_id)
            if (prev_name or prev_id) else None) \
        or await _dm_channel(prev_id, '_notify_previous_editor')
    if not dest:
        logger.warning(f'_notify_previous_editor: nowhere to reach {prev_name!r} ({prev_id})')
        return
    try:
        await dest.send(content=f'<@{prev_id}>', embed=embed)
        logger.info(f'_notify_previous_editor: notified {prev_name or prev_id}')
    except Exception as e:
        logger.error(f'_notify_previous_editor: send failed: {e}')


async def _notify_dashboard_creator(item, editor_name):
    """Tell the creator a website batch was assigned. The channel id rides on
    the command (the creator's own <first>-edits channel, from their dashboard
    profile) — website batches have no Notion creator row to look up, and names
    are not safe to match on: two students answer to "Chris"."""
    ch = await _creator_channel(item, 'cc_dashboard_notify(creator)')
    if ch is None:
        logger.warning(f"cc_dashboard_notify: no creator channel for {item.get('student_name')!r}")
        return

    embed = discord.Embed(title='📁 Your batch was assigned', colour=discord.Color.blue())
    embed.add_field(name='Batch', value=item.get('folder_name') or '—', inline=False)
    if item.get('video_count'):
        embed.add_field(name='Videos', value=str(item['video_count']), inline=True)
    embed.add_field(name='Editor', value=editor_name or '—', inline=True)
    embed.add_field(name='Status', value='In Progress ⏳', inline=True)
    # The creator's own view of the batch, not the editor queue they can't open.
    link = item.get('creator_url') or item.get('ticket_url')
    if link:
        embed.add_field(name='Track it', value=f"[Open your batch]({link})", inline=False)
    await ch.send(embed=embed)


class DashboardAssignSelect(discord.ui.Select):
    """Editor picker for a website batch. Unlike the Drive version there's no
    Notion row to stamp and no deadline entry to make — the dashboard owns the
    whole ticket, so the pick is just sent back to it."""

    def __init__(self, item, editor_names):
        self._item = item
        options = [discord.SelectOption(label=name) for name in editor_names[:25]]
        ticket_id = (item.get('ticket_id') or 'unknown')[:60]
        super().__init__(
            placeholder='Select an editor to assign...',
            options=options,
            min_values=1, max_values=1,
            custom_id=f'cc_assign_{ticket_id}',
        )

    async def callback(self, interaction: discord.Interaction):
        editor = self.values[0]
        item   = self._item
        loop   = asyncio.get_event_loop()
        await interaction.response.defer()

        editors = await loop.run_in_executor(None, fetch_editors_from_notion)
        uid = str((editors.get(editor) or {}).get('discord_user_id') or '')

        # The dashboard is the source of truth for this ticket: it applies the
        # assignment and fires its own editor notification. Parked and retried
        # by post_dashboard_assignment if the site is down.
        result = await loop.run_in_executor(None, post_dashboard_assignment, {
            'ticket_id':         item.get('ticket_id', ''),
            'creator_name':      item.get('student_name', ''),
            'folder_name':       item.get('folder_name', ''),
            'editor_name':       editor,
            'editor_discord_id': uid,
            'video_count':       item.get('video_count', 0),
        })

        msg_id = interaction.message.id if interaction.message else None
        if msg_id:
            remove_pending_ops_assign(msg_id)

        # These posts never expire on their own, so one can outlive the batch:
        # a website batch archived on the site left its picker sitting here,
        # and picking an editor on it put them on work that was already pulled
        # (Henry's empty monid twin, 2026-08-18). The site refuses those now and
        # answers with `skipped` — say so instead of claiming an assignment that
        # did not happen.
        skipped = (result or {}).get('skipped')
        if skipped:
            why = {
                'archived': 'this batch was pulled from the queue — nothing to assign.',
                'already_delivered': 'this batch is already delivered — nothing to assign.',
            }.get(skipped, f'the dashboard did not take this assignment ({skipped}).')
            gone = discord.Embed(title='⚠️ No longer assignable',
                                 description=why, color=discord.Color.orange())
            gone.add_field(name='Creator', value=creator_label(item), inline=True)
            gone.add_field(name='Batch', value=item.get('folder_name') or '—', inline=True)
            if item.get('ticket_url'):
                gone.add_field(name='Where', value=f"[Open in the dashboard]({item['ticket_url']})", inline=False)
            await interaction.edit_original_response(embed=gone, view=None)
            logger.info(f"cc_assign_request skipped ({skipped}): {item.get('ticket_id')}")
            return

        embed = discord.Embed(title=f'✅ Assigned to {editor}', color=discord.Color.green())
        embed.add_field(name='Creator', value=creator_label(item), inline=True)
        embed.add_field(name='Batch', value=item.get('folder_name') or '—', inline=True)
        if item.get('video_count'):
            embed.add_field(name='Videos', value=str(item['video_count']), inline=True)
        chat = creator_chat_link(item)
        if chat:
            embed.add_field(name='Their chat', value=chat, inline=False)
        if item.get('ticket_url'):
            embed.add_field(name='Where', value=f"[Open in the dashboard]({item['ticket_url']})", inline=False)
        await interaction.edit_original_response(embed=embed, view=None)
        logger.info(f"cc_assign_request: {item.get('folder_name')} → {editor} (website ticket)")


class DashboardAssignView(discord.ui.View):
    def __init__(self, item, editor_names):
        super().__init__(timeout=None)
        self.add_item(DashboardAssignSelect(item, editor_names))


def _offer_summary_fields(embed, item):
    """The four lines every offer card carries, so accept/pass/expired all
    describe the same batch."""
    embed.add_field(name='Creator', value=creator_label(item), inline=True)
    embed.add_field(name='Brand', value=item.get('client_name') or '—', inline=True)
    if item.get('video_count'):
        embed.add_field(name='Videos', value=str(item['video_count']), inline=True)
    if item.get('formats'):
        embed.add_field(name='Type', value=item['formats'], inline=False)
    if item.get('ticket_url'):
        embed.add_field(
            name='Where',
            value=f"[Open in the dashboard]({item['ticket_url']})",
            inline=False,
        )


async def _settle_offer(interaction, item, decision, note=''):
    """Send the editor's answer to the site and rewrite the card to match it.

    The site is the source of truth: on accept IT does the assignment and fires
    its own notifications, exactly like the assignments-channel picker. We only
    report what it decided."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, post_dashboard_offer_response, {
        'offer_id':          item.get('offer_id', ''),
        'ticket_id':         item.get('ticket_id', ''),
        'decision':          decision,
        'editor_discord_id': str(interaction.user.id),
        'editor_name':       item.get('editor_name', ''),
        'note':              note or '',
    })

    msg_id = interaction.message.id if interaction.message else None
    if msg_id:
        remove_pending_ops_assign(msg_id)

    # An offer can go stale between the card being posted and the click: the
    # batch was pulled, staff assigned it by hand, or it expired and went to
    # someone else. The site answers those with `skipped` rather than pretending
    # the click worked — same contract the assignments picker already relies on.
    skipped = (result or {}).get('skipped')
    if skipped:
        why = {
            'archived': 'this batch was pulled from the queue — nothing to take.',
            'already_assigned': 'someone else already picked this one up.',
            'expired': 'this offer timed out and went back to the queue.',
            'not_offered': 'this offer is no longer open.',
        }.get(skipped, f'the dashboard did not take this answer ({skipped}).')
        gone = discord.Embed(title='⚠️ Too late', description=why,
                             color=discord.Color.orange())
        _offer_summary_fields(gone, item)
        await interaction.edit_original_response(embed=gone, view=None)
        logger.info(f"cc_assign_offer skipped ({skipped}): {item.get('ticket_id')}")
        return

    if decision == 'accept':
        embed = discord.Embed(
            title="✅ It's yours",
            description='Assigned to you on the dashboard — the 24h clock starts when you press start.',
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title='👍 Passed',
            description=(f'Noted: {note}' if note else
                         "No problem — it's gone back to the queue for someone else."),
            color=discord.Color.greyple(),
        )
    _offer_summary_fields(embed, item)
    await interaction.edit_original_response(embed=embed, view=None)
    logger.info(f"cc_assign_offer {decision}: {item.get('ticket_id')} by {interaction.user.id}")


class OfferPassModal(discord.ui.Modal):
    """Why they're passing. Optional, but it's the whole value of a pass over
    silence — staff need to know if it's capacity, the brief, or the deadline."""

    def __init__(self, item):
        super().__init__(title='Pass on this batch')
        self._item = item
        self.note = discord.ui.TextInput(
            label="What's the reason? (optional)",
            placeholder='too much on today / not my kind of edit / off after this shift',
            required=False,
            max_length=300,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _settle_offer(interaction, self._item, 'reject', str(self.note.value or '').strip())


class AssignOfferView(discord.ui.View):
    """Accept / Pass on one batch.

    Buttons are built imperatively rather than with the decorator because
    persistent views need an explicit custom_id on every child — the decorator
    leaves it unset and the view silently fails to re-register after a restart
    (the same trap DiscordReviewView documents)."""

    def __init__(self, item):
        super().__init__(timeout=None)
        self._item = item
        key = str(item.get('offer_id') or item.get('ticket_id') or 'unknown')

        accept = discord.ui.Button(
            label='Accept', style=discord.ButtonStyle.green, emoji='✅',
            custom_id=f'cc_offer_yes_{key}'[:100],
        )
        accept.callback = self._on_accept
        self.add_item(accept)

        rej = discord.ui.Button(
            label='Pass', style=discord.ButtonStyle.secondary,
            custom_id=f'cc_offer_no_{key}'[:100],
        )
        rej.callback = self._on_pass
        self.add_item(rej)

    def _is_addressee(self, interaction):
        """Only the editor it was offered to may answer. The card lands in a
        channel other people can see, and an offer answered by a bystander is
        an assignment nobody agreed to."""
        want = str(self._item.get('editor_discord_id') or '').strip()
        # No id on the card means we can't tell them apart — fall open rather
        # than making the offer unanswerable.
        return not want or want == str(interaction.user.id)

    async def _on_accept(self, interaction: discord.Interaction):
        if not self._is_addressee(interaction):
            await interaction.response.send_message(
                "this one isn't addressed to you.", ephemeral=True)
            return
        await interaction.response.defer()
        await _settle_offer(interaction, self._item, 'accept')

    async def _on_pass(self, interaction: discord.Interaction):
        if not self._is_addressee(interaction):
            await interaction.response.send_message(
                "this one isn't addressed to you.", ephemeral=True)
            return
        # A modal must be the FIRST response to the interaction — no defer.
        await interaction.response.send_modal(OfferPassModal(self._item))


async def handle_cc_dashboard_assign_offer(item):
    """Ask one editor whether they'll take a batch, instead of dropping it on
    them. Goes to their own channel (or a DM), never the assignments feed —
    this is a question for them, not a staff routing card."""
    context = 'cc_dashboard_assign_offer'
    # The escalation path counts attempts by mutating THIS dict — the queue
    # re-appends the same object, which is how the count survives retries and
    # redeploys. So the fields it reads have to live on `item` itself; handing
    # it a copy would reset the counter every pass and retry forever.
    item.setdefault('target', 'editor')
    item.setdefault('title', 'assignment offer')
    item.setdefault('url', item.get('ticket_url', ''))

    ch = await _editor_channel(
        item.get('editor_name', ''), context,
        email=item.get('editor_email', ''),
        discord_user_id=item.get('editor_discord_id', ''),
    )
    if ch is None:
        ch = await _dm_channel(item.get('editor_discord_id'), context)
    if ch is None:
        # Reuses the message escalation: bounded retries, then the ops channel
        # and an undelivered report so the site stops waiting on an answer that
        # is never coming.
        await _dashboard_message_failed(item, context, 'no channel or DM to deliver to')
        return

    embed = discord.Embed(
        title='📥 New batch for you — take it?',
        description=item.get('reason') or None,
        colour=0x5865F2,
    )
    _offer_summary_fields(embed, item)
    if item.get('expires_at'):
        embed.set_footer(text='If nobody answers, it goes back to the queue.')

    view = AssignOfferView(item)
    try:
        sent = await ch.send(embed=embed, view=view)
    except Exception as e:
        await _dashboard_message_failed(item, context, f'discord refused the send: {e}')
        return

    # Stored in the same map the assignments cards use, so an archive on the
    # site retracts this card too (retract_pending_assign_cards matches on
    # ticket_id). The kind marker is what keeps on_ready from restoring it as
    # an editor-picker card.
    save_pending_ops_assign(sent.id, {**item, 'channel_id': ch.id,
                                      'card_kind': 'assign_offer'})
    logger.info(f"cc_assign_offer posted: {item.get('ticket_id')} → {item.get('editor_name')}")


async def handle_cc_dashboard_assign_request(item):
    """A batch submitted on the website that nobody has claimed. Goes to the
    same assignments channel as an unassigned Drive folder so it's routed from
    one place — the difference is there's no Drive folder behind it, so the
    embed links back to the dashboard instead."""
    if not ASSIGNMENTS_CHANNEL_ID:
        return
    try:
        ch = bot.get_channel(ASSIGNMENTS_CHANNEL_ID)
        if ch is None:
            ch = await bot.fetch_channel(ASSIGNMENTS_CHANNEL_ID)
    except Exception as e:
        logger.error(f'handle_cc_dashboard_assign_request: cannot reach assignments channel: {e}')
        return

    loop         = asyncio.get_event_loop()
    editor_names = sorted((await loop.run_in_executor(None, fetch_editors_from_notion)).keys())

    embed = discord.Embed(title='🌐 New Website Batch — Assign Editor', color=discord.Color.blurple())
    embed.add_field(name='Creator', value=creator_label(item), inline=True)
    embed.add_field(name='Brand', value=item.get('client_name') or '—', inline=True)
    embed.add_field(name='Videos', value=str(item.get('video_count') or 0), inline=True)
    # What's actually being cut — "hook + demo ×3, green screen ×1". Website
    # batches have no folder name to describe them, so this is the description.
    if item.get('formats'):
        embed.add_field(name='Type', value=item['formats'], inline=False)
    chat = creator_chat_link(item)
    if chat:
        embed.add_field(name='Their chat', value=chat, inline=False)
    if item.get('ticket_url'):
        embed.add_field(
            name='Where',
            value=f"[Open in the dashboard]({item['ticket_url']})\nNo Drive folder on this one — files and delivery live there.",
            inline=False,
        )

    view    = DashboardAssignView(item, editor_names)

    # A correction edits the card that's already in the channel rather than
    # stacking a second picker for the same batch. If we can't find or reach the
    # original — it was claimed, retracted, or predates the store — fall through
    # and post a fresh one, which is still better than losing the correction.
    if item.get('is_update'):
        ticket_id = str(item.get('ticket_id') or '').strip()
        for msg_id, saved in list(load_pending_ops_assigns().items()):
            if not ticket_id or str(saved.get('ticket_id') or '').strip() != ticket_id:
                continue
            try:
                cid = int(saved.get('channel_id') or ASSIGNMENTS_CHANNEL_ID or 0)
                mch = bot.get_channel(cid) or await bot.fetch_channel(cid)
                msg = await mch.fetch_message(int(msg_id))
                await msg.edit(embed=embed, view=view)
                save_pending_ops_assign(msg_id, {**item, 'channel_id': cid})
                logger.info(
                    f"cc_assign_request updated in place: "
                    f"{item.get('student_name')}/{item.get('folder_name')}"
                )
                return
            except Exception as e:
                logger.warning(
                    f'handle_cc_dashboard_assign_request: could not edit card '
                    f'{msg_id} for ticket {ticket_id} ({e}) — posting a fresh one'
                )
                break

    content = f'<@{VEX_USER_ID}>' if VEX_USER_ID else None
    sent    = await ch.send(content=content, embed=embed, view=view)
    save_pending_ops_assign(sent.id, {**item, 'channel_id': ASSIGNMENTS_CHANNEL_ID})
    logger.info(f"cc_assign_request posted: {item.get('student_name')}/{item.get('folder_name')}")


async def handle_cc_dashboard_approve(item):
    """A student approved their cut in the Creator Collective dashboard — tell
    the editor in their own channel so approvals aren't invisible in Discord,
    and close the Notion row so it stops reading as outstanding work.

    The Notion half matters more than the embed. /stats is Notion-driven, and a
    Drive batch the creator has already signed off sat there as In Progress (or
    worse, Revision) forever — Zyon had 13 approved videos still showing as a
    revision to redo. Active Queue has no 'Approved' state; Delivered is the
    terminal one, and it's what /complete sets, so approval lands there too."""
    editor_name = item.get('editor_name', '')
    folder_id   = item.get('folder_id', '')
    if folder_id:
        loop  = asyncio.get_event_loop()
        token = load_config()['notion_token']
        # Same folder-keyed lookup the archive branch uses; a website batch has
        # no folder id and no Notion row, so it skips straight to the embed.
        snapshot = await loop.run_in_executor(None, fetch_active_queue_snapshot)
        match = next((r for r in snapshot if r['folder_id'] == folder_id), None)
        if match:
            await loop.run_in_executor(
                None, update_active_queue_status, token, match['page_id'], 'Delivered'
            )
            await loop.run_in_executor(None, pop_deadline_entry, folder_id, match['page_id'])
            logger.info(
                f"cc_dashboard_approve: notion -> Delivered for "
                f"{item.get('folder_name')} ({editor_name})"
            )
        else:
            logger.info(
                f"cc_dashboard_approve: no live Active Queue row for "
                f"folder_id={folder_id!r} — already closed, nothing to flip"
            )

    channel = await _editor_channel(
        editor_name, 'cc_dashboard_approve',
        email=item.get('editor_email', ''),
        discord_user_id=item.get('editor_discord_id', ''),
    )
    if not channel:
        return

    who = item.get('student_name') or item.get('client_name') or 'The creator'
    folder = item.get('folder_name') or 'the batch'
    client = item.get('client_name') or ''
    await channel.send(embed=discord.Embed(
        title='🎉 Approved',
        description=f"**{who}** approved **{folder}**" + (f' ({client})' if client else ''),
        colour=0x2ecc71,
    ))
    logger.info(f"cc_dashboard_approve: {item.get('folder_name')} → {editor_name}")


async def handle_dashboard_approve(item):
    """Approve a flagged completion (triggered from dashboard)."""
    review_id = item.get('review_id', '')
    with PENDING_REVIEW_LOCK:
        if not os.path.exists(PENDING_REVIEWS_FILE):
            logger.error(f'handle_dashboard_approve: no pending reviews file')
            return
        with open(PENDING_REVIEWS_FILE) as f:
            all_reviews = json.load(f)

    rd = all_reviews.get(review_id)
    if not rd:
        logger.error(f'handle_dashboard_approve: review {review_id} not found')
        return

    if await _approve_review(rd):
        logger.info(f'dashboard_approve: finalized review {review_id} ({rd.get("folder_name")})')
    else:
        logger.warning(f'dashboard_approve: review {review_id} was already approved, skipped')


async def handle_dashboard_remove(item):
    """Archive a folder (triggered from dashboard)."""
    notion_page_id = item['notion_page_id']
    config = load_config()
    token  = config['notion_token']
    loop   = asyncio.get_event_loop()

    resp = await loop.run_in_executor(None, lambda: requests.patch(
        f'https://api.notion.com/v1/pages/{notion_page_id}',
        headers=notion_headers(token),
        json={'archived': True},
        timeout=15,
    ))
    if resp.ok:
        cache_removed_folder(notion_page_id, item, item.get('status', ''))
        pop_deadline_entry(item.get('folder_id', ''), notion_page_id)
        logger.info(f'dashboard_remove: archived {item.get("folder_name")}')
    else:
        logger.error(f'dashboard_remove: Notion error {resp.status_code}')


async def handle_dashboard_recover(item):
    """Unarchive a folder (triggered from dashboard)."""
    notion_page_id = item['notion_page_id']
    config = load_config()
    token  = config['notion_token']
    loop   = asyncio.get_event_loop()

    row = pop_removed_folder(notion_page_id)
    resp = await loop.run_in_executor(None, lambda: requests.patch(
        f'https://api.notion.com/v1/pages/{notion_page_id}',
        headers=notion_headers(token),
        json={'archived': False},
        timeout=15,
    ))
    if not resp.ok:
        if row:
            cache_removed_folder(notion_page_id, row, row.get('status', ''))
        logger.error(f'dashboard_recover: Notion error {resp.status_code}')
    else:
        logger.info(f'dashboard_recover: unarchived {notion_page_id}')


async def process_queue_loop():
    """
    Poll discord_queue.json every 3 s for assignments written by notion_bridge.py.
    Queue format: list of {client_name, folder_name, video_count, folder_id,
                           editor_name, notion_queue_page_id (optional)}
    """
    while True:
        await asyncio.sleep(3)
        if not os.path.exists(QUEUE_FILE):
            continue

        # Read and clear atomically under the file lock
        try:
            with QUEUE_LOCK:
                with open(QUEUE_FILE) as f:
                    queue = json.load(f)
                if not queue:
                    continue
                with open(QUEUE_FILE, 'w') as f:
                    json.dump([], f)
        except (json.JSONDecodeError, OSError):
            continue

        # Process items outside the lock (may involve async I/O)
        remaining = []
        for item in queue:
            try:
                if item.get('type') == 'update':
                    await send_folder_update_msg(item)
                elif item.get('type') == 'finalize':
                    await handle_discord_finalize(item)
                elif item.get('type') == 'ops_assign_request':
                    await handle_ops_assign_request(item)
                elif item.get('type') == 'dashboard_reassign':
                    await handle_dashboard_reassign(item)
                elif item.get('type') == 'dashboard_revision':
                    await handle_dashboard_revision(item)
                elif item.get('type') == 'dashboard_approve':
                    await handle_dashboard_approve(item)
                elif item.get('type') == 'dashboard_remove':
                    await handle_dashboard_remove(item)
                elif item.get('type') == 'dashboard_recover':
                    await handle_dashboard_recover(item)
                elif item.get('type') == 'creator_detected':
                    await handle_creator_detected(item)
                elif item.get('type') == 'creator_notify':
                    await handle_creator_notify(item)
                elif item.get('type') == 'creator_complete_notify':
                    await handle_creator_complete_notify(item)
                elif item.get('type') == 'reassign_notify':
                    await handle_reassign_notify(item)
                elif item.get('type') == 'announce':
                    await handle_announce(item)
                elif item.get('type') == 'cc_dashboard_approve':
                    await handle_cc_dashboard_approve(item)
                elif item.get('type') == 'cc_dashboard_notify':
                    await handle_cc_dashboard_notify(item)
                elif item.get('type') == 'cc_dashboard_assign_request':
                    await handle_cc_dashboard_assign_request(item)
                elif item.get('type') == 'cc_dashboard_assign_offer':
                    await handle_cc_dashboard_assign_offer(item)
                elif item.get('type') == 'cc_dashboard_message':
                    await handle_cc_dashboard_message(item)
                elif item.get('type') == 'cc_dashboard_delivered':
                    await handle_cc_dashboard_delivered(item)
                elif item.get('type') == 'cc_dashboard_reopen':
                    await handle_cc_dashboard_reopen(item)
                elif item.get('type') == 'approve_pending_review':
                    await handle_approve_pending_review(item)
                else:
                    is_reassign = item.get('is_reassign', False)
                    await assign_folder(
                        item['client_name'],
                        item['folder_name'],
                        item['video_count'],
                        item.get('folder_id', ''),
                        item['editor_name'],
                        item.get('notion_queue_page_id'),
                        item.get('project_number', ''),
                        is_reassign,
                        item.get('from_dashboard', False),
                    )
                    # A dashboard-driven reassign has no handle_reassign_notify
                    # behind it — that only fires for moves made in Discord — so
                    # the outgoing editor is told here or not at all. No-ops
                    # unless we were given their discord id.
                    if is_reassign and item.get('from_dashboard'):
                        await _notify_previous_editor(item)
                    # New assignments always notify the creator's Discord channel.
                    # Reassigns are covered separately by handle_reassign_notify
                    # (different message: "reassigned to", not "new folder").
                    if not is_reassign:
                        await handle_creator_notify({
                            'client_name':    item['client_name'],
                            'folder_name':    item['folder_name'],
                            'editor_name':    item['editor_name'],
                            'video_count':    item['video_count'],
                            'folder_id':      item.get('folder_id', ''),
                            'project_number': item.get('project_number', ''),
                        })
            except Exception as e:
                logger.error(f'Queue item failed: {e} — {item}')
                remaining.append(item)

        # Re-append failures under the lock so concurrent writers aren't clobbered
        if remaining:
            try:
                with QUEUE_LOCK:
                    existing = []
                    if os.path.exists(QUEUE_FILE):
                        with open(QUEUE_FILE) as f:
                            existing = json.load(f)
                    with open(QUEUE_FILE, 'w') as f:
                        json.dump(existing + remaining, f, indent=2)
            except OSError as e:
                logger.error(f'Failed to requeue failed items: {e}')


# ── Extend deadline command ────────────────────────────────────────────────────

class ExtendHoursModal(discord.ui.Modal, title='Extend Deadline'):
    hours_input = discord.ui.TextInput(
        label='Add hours (or 0 to set Indefinite)',
        placeholder='e.g. 12  (enter 0 for no deadline)',
        min_length=1, max_length=5,
    )

    def __init__(self, folder_id, folder_label):
        super().__init__()
        self._folder_id    = folder_id
        self._folder_label = folder_label

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.hours_input.value.strip()
        try:
            hours = int(raw)
        except ValueError:
            await interaction.response.send_message('Enter a whole number of hours.', ephemeral=True)
            return

        deadlines = load_deadlines()
        entry = deadlines.get(self._folder_id, {})

        entry['missed_deadline_logged'] = False

        # Vex explicitly setting a deadline (or indefinite) overrides the pickup
        # flow — the folder leaves the pending-start state and nags stop.
        if entry.get('pending_start'):
            entry['pending_start'] = False
            entry['started_at']    = entry.get('started_at') or time.time()

        if hours == 0:
            entry['indefinite'] = True
            entry['due_ts']     = None
            entry['warned_6h']  = False
            msg = f'♾️ **{self._folder_label}** set to **Indefinite** — no deadline until you set one or editor completes it.'
        else:
            entry['indefinite'] = False
            base_ts = entry.get('due_ts') or time.time()
            if base_ts < time.time():
                base_ts = time.time()
            entry['due_ts'] = base_ts + hours * 3600
            # Only clear warned_6h if the new deadline is more than 6h out — otherwise
            # the checker re-fires the "due soon" warning on its next tick immediately
            # after the extension, which reads as "the extension didn't take effect".
            entry['warned_6h'] = (entry['due_ts'] - time.time()) <= 6 * 3600
            due_dt  = datetime.fromtimestamp(entry['due_ts'], tz=timezone.utc).astimezone(IST)
            due_str = due_dt.strftime('%d %b %I:%M %p IST')
            msg = f'⏰ **{self._folder_label}** deadline extended by **{hours}h** — now due **{due_str}**'

        deadlines[self._folder_id] = entry
        save_deadlines(deadlines)
        await interaction.response.send_message(msg, ephemeral=False)


class ExtendFolderSelect(discord.ui.View):
    def __init__(self, in_progress_rows):
        super().__init__(timeout=120)
        options = [
            discord.SelectOption(
                label=f"{r['client_name']} / {r['folder_name']}"[:100],
                value=r['folder_id'],
                description=format_deadline(r['folder_id']) or 'No deadline tracked',
            )
            for r in in_progress_rows if r.get('folder_id')
        ][:25]
        select = discord.ui.Select(placeholder='Choose a folder…', options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._rows = {r['folder_id']: r for r in in_progress_rows if r.get('folder_id')}

    async def _on_select(self, interaction: discord.Interaction):
        folder_id = interaction.data['values'][0]
        r = self._rows.get(folder_id, {})
        label = f"{r.get('client_name', '?')} / {r.get('folder_name', '?')}"
        await interaction.response.send_modal(ExtendHoursModal(folder_id, label))


@tree.command(
    name='extend',
    description='Extend deadline for an in-progress folder (or set to Indefinite)',
    guilds=[GUILD_OBJ],
)
async def extend_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team only.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()

    # Check if this is an editor channel — if so, show only that editor's folders
    channel_id     = interaction.channel_id
    editor_result  = await loop.run_in_executor(None, fetch_editor_by_channel_id, channel_id)
    channel_editor = editor_result[0] if editor_result else None

    if channel_editor:
        editor_rows = await loop.run_in_executor(None, fetch_in_progress_for_editor, channel_editor)
        rows = [{
            'folder_name': r.get('folder_name', ''),
            'client_name': r.get('client_name', ''),
            'folder_id':   r.get('folder_id', ''),
        } for r in editor_rows]
    else:
        in_progress_rows, revision_rows = await asyncio.gather(
            loop.run_in_executor(None, fetch_active_queue_in_progress),
            loop.run_in_executor(None, fetch_all_revision_folders),
        )
        rows = in_progress_rows + revision_rows

    rows_with_id = [r for r in rows if r.get('folder_id')]
    if not rows_with_id:
        msg = f'No in-progress or revision folders for **{channel_editor}**.' if channel_editor else 'No in-progress or revision folders found.'
        await interaction.followup.send(msg, ephemeral=True)
        return

    view   = ExtendFolderSelect(rows_with_id)
    prompt = f"Which of **{channel_editor}**'s folders to extend?" if channel_editor else 'Which folder?'
    await interaction.followup.send(prompt, view=view, ephemeral=True)


# ── Reassign command (Discord) ─────────────────────────────────────────────────

class ReassignEditorSelect(discord.ui.View):
    def __init__(self, folder_id, client_name, folder_name, video_count, notion_page_id, editors,
                 old_editor='', is_revision=False, ticket_id=''):
        super().__init__(timeout=120)
        self._folder_id      = folder_id
        self._client_name    = client_name
        self._folder_name    = folder_name
        self._video_count    = video_count
        self._notion_page_id = notion_page_id
        self._old_editor     = old_editor
        self._is_revision    = is_revision
        self._ticket_id      = ticket_id
        options = [discord.SelectOption(label=e, value=e) for e in editors][:25]
        select  = discord.ui.Select(placeholder='Choose new editor…', options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        new_editor = interaction.data['values'][0]
        await interaction.response.defer()

        loop = asyncio.get_event_loop()

        # Website-native batch — no Notion page, no Drive folder. The dashboard
        # write endpoint does the assignment, event log, outbox ping, and
        # notify entirely on its own; the eventual inbound 'notify' command it
        # sends back covers pinging the incoming editor + creator, and (via
        # previous_editor_name/previous_editor_discord_id on that same
        # command) the outgoing editor too — see handle_cc_dashboard_notify /
        # _notify_previous_editor. So nothing folder-keyed runs here, and we
        # don't enqueue our own reassign notify (that would double-ping).
        if self._ticket_id:
            editors_map    = await loop.run_in_executor(None, fetch_editors_from_notion)
            new_discord_id = (editors_map.get(new_editor) or {}).get('discord_user_id', '')
            ok, err = await loop.run_in_executor(
                None, post_dashboard_ticket_reassign, self._ticket_id, new_editor, new_discord_id
            )
            if not ok:
                await interaction.edit_original_response(
                    content=f'❌ Failed to reassign **{self._client_name} / {self._folder_name}** '
                            f'(website batch) on the dashboard: {err}',
                    view=None,
                )
                return
            await interaction.edit_original_response(
                content=f'✅ **{self._client_name} / {self._folder_name}** (website batch) '
                        f'reassigned to **{new_editor}**.',
                view=None,
            )
            logger.info(
                f'Reassigned website batch {self._folder_name} (ticket={self._ticket_id}) → {new_editor}'
            )
            return

        config = load_config()
        token  = config['notion_token']

        # Keep Revision status for revision folders; set In Progress for normal reassigns
        new_status = 'Revision' if self._is_revision else 'In Progress'

        notion_ok = True
        if self._notion_page_id:
            def _do_patch():
                return requests.patch(
                    f'https://api.notion.com/v1/pages/{self._notion_page_id}',
                    headers=notion_headers(token),
                    json={'properties': {
                        'Editor': {'select': {'name': new_editor}},
                        'Status': {'select': {'name': new_status}},
                    }},
                    timeout=15,
                )
            try:
                resp = await loop.run_in_executor(None, _do_patch)
                if not resp.ok:
                    logger.error(f'Reassign Notion PATCH failed {resp.status_code}: {resp.text[:200]}')
                    notion_ok = False
            except Exception as e:
                logger.error(f'Reassign Notion PATCH error: {e}')
                notion_ok = False

        if not notion_ok:
            await interaction.edit_original_response(
                content=f'❌ Failed to update Notion for **{self._client_name} / {self._folder_name}**. Try again.',
                view=None,
            )
            return

        # Update deadline to new editor (falls back to notion_page_id match if folder_id is blank)
        update_deadline_editor(self._folder_id, self._notion_page_id, new_editor)

        await loop.run_in_executor(None, recalculate_active_videos, token, new_editor)
        if self._old_editor and self._old_editor != new_editor:
            await loop.run_in_executor(None, recalculate_active_videos, token, self._old_editor)

        if self._is_revision:
            # Send a revision assignment embed to the new editor's channel
            editors_map = await loop.run_in_executor(None, fetch_editors_from_notion)
            editor_info = editors_map.get(new_editor)
            if editor_info:
                await open_revision_assignment(
                    client_name=self._client_name,
                    folder_name=self._folder_name,
                    folder_id=self._folder_id,
                    video_count=self._video_count,
                    editor_name=new_editor,
                    editor_info=editor_info,
                    notion_queue_page_id=self._notion_page_id,
                )
            else:
                logger.warning(f'ReassignEditorSelect: editor {new_editor!r} not found in Notion for revision notify')
        else:
            # Send a regular assignment notification to the new editor
            await loop.run_in_executor(None, _enqueue_reassign,
                self._client_name, self._folder_name, self._video_count,
                self._folder_id, new_editor, self._notion_page_id)

        # Notify creator + old editor
        if self._old_editor and self._old_editor != new_editor:
            await loop.run_in_executor(None, _enqueue_reassign_notify,
                self._client_name, self._folder_name, self._old_editor, new_editor)

        label = '🔄 Revision' if self._is_revision else 'folder'
        await interaction.edit_original_response(
            content=f'✅ **{self._client_name} / {self._folder_name}** ({label}) reassigned to **{new_editor}**.',
            view=None,
        )
        logger.info(f'Reassigned {self._folder_name} (revision={self._is_revision}) → {new_editor}')


def _enqueue_reassign(client_name, folder_name, video_count, folder_id, editor_name, notion_page_id):
    try:
        with QUEUE_LOCK:
            existing = []
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE) as f:
                    existing = json.load(f)
            existing.append({
                'client_name':          client_name,
                'folder_name':          folder_name,
                'video_count':          video_count,
                'folder_id':            folder_id,
                'editor_name':          editor_name,
                'notion_queue_page_id': notion_page_id,
                'is_reassign':          True,
            })
            with open(QUEUE_FILE, 'w') as f:
                json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error(f'_enqueue_reassign failed: {e}')


def _enqueue_reassign_notify(client_name, folder_name, old_editor, new_editor):
    """Enqueue a reassign_notify IPC item to ping creator + old editor."""
    try:
        item = {
            'type':        'reassign_notify',
            'client_name': client_name,
            'folder_name': folder_name,
            'old_editor':  old_editor,
            'new_editor':  new_editor,
        }
        with QUEUE_LOCK:
            existing = []
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE) as f:
                    existing = json.load(f)
            existing.append(item)
            with open(QUEUE_FILE, 'w') as f:
                json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error(f'_enqueue_reassign_notify failed: {e}')


class ReassignFolderSelect(discord.ui.View):
    def __init__(self, rows, editors):
        super().__init__(timeout=120)
        self._editors = editors
        options = [
            discord.SelectOption(
                label=f"{'🔄 ' if r.get('is_revision') else ''}{r['client_name']} / {r['folder_name']}"[:100],
                value=r.get('ticket_id', '') or r.get('notion_page_id', '') or r.get('folder_id', '') or r['folder_name'],
                description='Revision' if r.get('is_revision') else ('Website' if r.get('source') == 'website' else 'In Progress'),
            )
            for r in rows
        ][:25]
        select = discord.ui.Select(placeholder='Choose folder to reassign…', options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._rows = {
            (r.get('ticket_id', '') or r.get('notion_page_id', '') or r.get('folder_id', '') or r['folder_name']): r
            for r in rows
        }

    async def _on_select(self, interaction: discord.Interaction):
        key = interaction.data['values'][0]
        r   = self._rows.get(key, {})
        view = ReassignEditorSelect(
            folder_id      = r.get('folder_id', ''),
            client_name    = r.get('client_name', ''),
            folder_name    = r.get('folder_name', ''),
            video_count    = r.get('video_count', 0),
            notion_page_id = r.get('notion_page_id', ''),
            ticket_id      = r.get('ticket_id', ''),
            old_editor     = r.get('editor_name', ''),
            editors        = self._editors,
            is_revision    = r.get('is_revision', False),
        )
        label = '🔄 Revision' if r.get('is_revision') else 'folder'
        await interaction.response.edit_message(
            content=f"Reassigning **{r.get('client_name')} / {r.get('folder_name')}** ({label}) — pick new editor:",
            view=view,
        )


@tree.command(
    name='unavailable',
    description='Mark yourself as unavailable today — Vex will skip you for auto-assign',
    guilds=[GUILD_OBJ],
)
async def unavailable_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop        = asyncio.get_event_loop()
    editor_name = await loop.run_in_executor(None, fetch_editor_by_user_id, interaction.user.id)
    if not editor_name:
        await interaction.followup.send(
            '❌ Your Discord account isn\'t linked to an editor profile. Ask Vex to add your Discord User ID.',
            ephemeral=True,
        )
        return
    await loop.run_in_executor(None, set_editor_available_today, editor_name, False)
    today = datetime.now(EDT).strftime('%A')
    await interaction.followup.send(
        f'❌ **{editor_name}** marked as **unavailable** for today ({today}).\n'
        'You won\'t be recommended for new assignments. Use `/available` when you\'re back.',
        ephemeral=True,
    )
    logger.info(f'{editor_name} marked unavailable for {today} via Discord')


@tree.command(
    name='available',
    description='Mark yourself as available — re-enables you for assignment recommendations',
    guilds=[GUILD_OBJ],
)
async def available_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop        = asyncio.get_event_loop()
    editor_name = await loop.run_in_executor(None, fetch_editor_by_user_id, interaction.user.id)
    if not editor_name:
        await interaction.followup.send(
            '❌ Your Discord account isn\'t linked to an editor profile. Ask Vex to add your Discord User ID.',
            ephemeral=True,
        )
        return
    await loop.run_in_executor(None, set_editor_available_today, editor_name, True)
    today = datetime.now(EDT).strftime('%A')
    await interaction.followup.send(
        f'✅ **{editor_name}** marked as **available** for today ({today}).\n'
        'You\'re back in the recommendation pool.',
        ephemeral=True,
    )
    logger.info(f'{editor_name} marked available for {today} via Discord')


@tree.command(
    name='reassign',
    description='Reassign an in-progress or revision folder to a different editor',
    guilds=[GUILD_OBJ],
)
async def reassign_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team only.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()

    try:
        # Check if this is an editor channel — if so, show only that editor's folders
        channel_id    = interaction.channel_id
        editor_result = await loop.run_in_executor(None, fetch_editor_by_channel_id, channel_id)
        channel_editor = editor_result[0] if editor_result else None
        if channel_editor:
            editor_rows, editors_map = await asyncio.gather(
                loop.run_in_executor(None, fetch_in_progress_for_editor, channel_editor),
                loop.run_in_executor(None, fetch_editors_from_notion),
            )
            # Normalize key: fetch_in_progress_for_editor uses notion_queue_page_id
            rows = []
            for r in editor_rows:
                rows.append({
                    'folder_name':    r.get('folder_name', ''),
                    'client_name':    r.get('client_name', ''),
                    'editor_name':    channel_editor,
                    'video_count':    r.get('video_count', 0),
                    'folder_id':      r.get('folder_id', ''),
                    'notion_page_id': r.get('notion_queue_page_id', ''),
                    'is_revision':    r.get('is_revision', False),
                })
            channel_editor_discord_id = (editors_map.get(channel_editor) or {}).get('discord_user_id', '')
        else:
            in_progress_rows, revision_rows, editors_map = await asyncio.gather(
                loop.run_in_executor(None, fetch_active_queue_in_progress),
                loop.run_in_executor(None, fetch_all_revision_folders),
                loop.run_in_executor(None, fetch_editors_from_notion),
            )
            for r in revision_rows:
                r['is_revision'] = True
            rows = in_progress_rows + revision_rows
    except Exception as e:
        logger.error(f'reassign_command: Notion fetch failed: {e}')
        await interaction.followup.send('❌ Could not reach Notion right now. Try again in a moment.', ephemeral=True)
        return

    # Website-native batches have no Notion row, so Notion never surfaces them —
    # pull them from the dashboard and concatenate. Scoped to this editor when
    # run in their channel (id first, per fetch_dashboard_assignable_batches'
    # own disambiguation rule). A dashboard outage here must not take
    # /reassign down — log and continue with just the Notion rows.
    try:
        if channel_editor:
            website_rows = await loop.run_in_executor(
                None, fetch_dashboard_assignable_batches, channel_editor_discord_id, channel_editor
            )
        else:
            website_rows = await loop.run_in_executor(None, fetch_dashboard_assignable_batches)
        rows += website_rows
    except Exception as e:
        logger.error(f'reassign_command: dashboard assignable-batches fetch failed: {e}')

    if not rows:
        msg = f'No in-progress or revision folders for **{channel_editor}**.' if channel_editor else 'No in-progress or revision folders.'
        await interaction.followup.send(msg, ephemeral=True)
        return

    editors = list(editors_map.keys())
    view    = ReassignFolderSelect(rows, editors)
    prompt  = f"Which of **{channel_editor}**'s folders to reassign?" if channel_editor else 'Which folder?'
    await interaction.followup.send(prompt, view=view, ephemeral=True)


# ── Schedule view + change request ────────────────────────────────────────────

SCHEDULE_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

def _parse_utc_offset(tz_str: str) -> float:
    """Parse UTC offset hours from strings like 'PHT (UTC+8)' or 'EST (UTC-5)'."""
    import re as _re
    m = _re.search(r'UTC([+-]\d+(?:\.\d+)?)', tz_str or '')
    return float(m.group(1)) if m else 0.0

def _convert_schedule_to_est(raw: str, utc_offset: float) -> str:
    """Convert 'HH:MM-HH:MM' blocks (pipe-separated) from editor tz to EST (UTC-5)."""
    if not raw.strip():
        return 'Off'
    est_offset = -5.0
    shift = est_offset - utc_offset  # hours to add
    results = []
    for block in raw.strip().split('|'):
        block = block.strip()
        if '-' not in block:
            continue
        parts = block.split('-', 1)
        converted = []
        for t in parts:
            t = t.strip()
            try:
                h, m = map(int, t.split(':'))
            except ValueError:
                converted.append(t)
                continue
            total_min = h * 60 + m + int(shift * 60)
            total_min = total_min % (24 * 60)
            nh, nm = divmod(total_min, 60)
            converted.append(f'{nh:02d}:{nm:02d}')
        results.append('-'.join(converted))
    return ', '.join(results) if results else raw

def fetch_editor_schedule(editor_name: str) -> dict:
    """Returns {day: raw_str, timezone: str} from Editor Profiles for editor_name."""
    config = load_config()
    token  = config['notion_token']
    resp   = requests.post(
        f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query',
        headers=notion_headers(token),
        json={'filter': {'property': 'Editor', 'title': {'equals': editor_name}}},
        timeout=15,
    )
    if not resp.ok or not resp.json().get('results'):
        return {}
    props  = resp.json()['results'][0]['properties']
    result = {}
    for day in SCHEDULE_DAYS:
        rt = props.get(f'{day} Schedule', {}).get('rich_text', [])
        result[day] = ''.join(seg.get('plain_text', '') for seg in rt)
    tz_rt = props.get('Timezone', {}).get('rich_text', [])
    result['timezone'] = ''.join(seg.get('plain_text', '') for seg in tz_rt)
    return result


# ── Ops Assignment Channel (assignments channel for Vex) ──────────────────────

class AssignEditorSelect(discord.ui.Select):
    def __init__(self, item, editor_names):
        self._item = item
        options = [discord.SelectOption(label=name) for name in editor_names[:25]]
        # Stable custom_id so the view survives bot restarts
        folder_id = item.get('folder_id', 'unknown')[:60]
        super().__init__(
            placeholder='Select an editor to assign...',
            options=options,
            min_values=1, max_values=1,
            custom_id=f'ops_assign_{folder_id}',
        )

    async def callback(self, interaction: discord.Interaction):
        editor = self.values[0]
        item   = self._item
        config = load_config()
        token  = config['notion_token']
        loop   = asyncio.get_event_loop()

        await interaction.response.defer()

        client_name    = item['client_name']
        folder_name    = item['folder_name']
        video_count    = item['video_count']
        folder_id      = item.get('folder_id', '')
        notion_page_id = item.get('notion_page_id', '')
        project_number = item.get('project_number', '')

        result_pid = await loop.run_in_executor(None, _assign_raw_to_editor, token, folder_id, editor)
        notion_page_id = result_pid or notion_page_id

        await assign_folder(client_name, folder_name, video_count, folder_id, editor, notion_page_id, project_number)
        await handle_creator_notify({
            'client_name':    client_name,
            'folder_name':    folder_name,
            'editor_name':    editor,
            'video_count':    video_count,
            'folder_id':      folder_id,
            'project_number': project_number,
        })

        # Clean up persistent store — this folder is now assigned
        msg_id = interaction.message.id if interaction.message else None
        if msg_id:
            remove_pending_ops_assign(msg_id)

        embed = discord.Embed(title=f'✅ Assigned to {editor}', color=discord.Color.green())
        embed.add_field(name='Client', value=client_name, inline=True)
        embed.add_field(name='Folder', value=folder_name, inline=True)
        embed.add_field(name='Videos', value=str(video_count), inline=True)
        try:
            client_link, _raw = await loop.run_in_executor(
                None, find_assignment_drive_links, client_name, folder_name)
            client_root_id = client_link.rstrip('/').split('/')[-1] if client_link else None
            drive_links = build_drive_links_field(client_root_id, folder_id)
            if drive_links:
                embed.add_field(name='Drive Links', value=drive_links, inline=False)
        except Exception as e:
            logger.warning(f'AssignEditorSelect: drive link resolve failed: {e}')
        await interaction.edit_original_response(embed=embed, view=None)
        logger.info(f"ops_assign_request: {client_name}/{folder_name} → {editor} (via assignments channel)")


class IgnoreAssignmentButton(discord.ui.Button):
    def __init__(self, item):
        self._item = item
        folder_id = item.get('folder_id', 'unknown')[:60]
        super().__init__(
            label='🚫 Ignore',
            style=discord.ButtonStyle.secondary,
            custom_id=f'ops_ignore_{folder_id}',
        )

    async def callback(self, interaction: discord.Interaction):
        item      = self._item
        folder_id = item.get('folder_id', '')
        if folder_id:
            _add_ignored_folder_id(folder_id)
        msg_id = interaction.message.id if interaction.message else None
        if msg_id:
            remove_pending_ops_assign(msg_id)
        embed = discord.Embed(
            title='🚫 Folder Ignored',
            description=f"{item['client_name']} / {item['folder_name']}",
            color=discord.Color.dark_gray(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        logger.info(f"ops_ignore: {item['client_name']}/{item['folder_name']} (folder_id={folder_id})")


class AssignEditorView(discord.ui.View):
    def __init__(self, item, editor_names):
        super().__init__(timeout=None)
        self.add_item(AssignEditorSelect(item, editor_names))
        self.add_item(IgnoreAssignmentButton(item))


async def handle_ops_assign_request(item):
    """Posts a folder assignment embed to the assignments channel, pinging Vex."""
    if not ASSIGNMENTS_CHANNEL_ID:
        return
    try:
        ch = bot.get_channel(ASSIGNMENTS_CHANNEL_ID)
        if ch is None:
            ch = await bot.fetch_channel(ASSIGNMENTS_CHANNEL_ID)
    except Exception as e:
        logger.error(f'handle_ops_assign_request: cannot reach assignments channel: {e}')
        return

    loop         = asyncio.get_event_loop()
    editor_names = sorted((await loop.run_in_executor(None, fetch_editors_from_notion)).keys())

    pnum  = item.get('project_number', '')
    title = f'📁 New Folder — Assign Editor  {pnum}' if pnum else '📁 New Folder — Assign Editor'
    embed = discord.Embed(title=title, color=discord.Color.yellow())
    embed.add_field(name='Client', value=item['client_name'], inline=True)
    embed.add_field(name='Folder', value=item['folder_name'], inline=True)
    embed.add_field(name='Videos', value=str(item['video_count']), inline=True)

    # Drive links — Client Folder + Raw Footage subfolder (item['folder_id'] is
    # already the Raw Footage subfolder Drive ID; resolve the client root too).
    try:
        client_link, _raw_link = await loop.run_in_executor(
            None, find_assignment_drive_links, item['client_name'], item['folder_name'])
    except Exception as e:
        logger.warning(f'handle_ops_assign_request: drive link resolve failed: {e}')
        client_link = None
    client_root_id = None
    if client_link:
        client_root_id = client_link.rstrip('/').split('/')[-1]
    drive_links = build_drive_links_field(client_root_id, item.get('folder_id'))
    if drive_links:
        embed.add_field(name='Drive Links', value=drive_links, inline=False)

    view    = AssignEditorView(item, editor_names)
    content = f'<@{VEX_USER_ID}>' if VEX_USER_ID else None
    sent    = await ch.send(content=content, embed=embed, view=view)

    # Persist so view survives bot restarts
    save_pending_ops_assign(sent.id, {**item, 'channel_id': ASSIGNMENTS_CHANNEL_ID})
    logger.info(f"ops_assign_request posted: {item['client_name']}/{item['folder_name']} ({item['video_count']} videos)")

    # Mirror the unassigned folder into the Creator Collective dashboard so Vex
    # can see and assign it there before it's ever touched in Discord. Fires
    # after the embed is already sent — a dead dashboard must never hold up or
    # break the ops-channel post. editor_name/editor_discord_id are omitted on
    # purpose: the site creates the ticket unassigned (status `submitted`).
    try:
        raw_footage_link = (
            f"https://drive.google.com/drive/folders/{item['folder_id']}"
            if item.get('folder_id') else ''
        )
        creator_channel_id, creator_discord_id = await loop.run_in_executor(
            None, fetch_creator_discord_info, item['client_name']
        )
        folder_created_at, drive_folder_name = await loop.run_in_executor(
            None, get_folder_drive_meta, item.get('folder_id')
        )
        dashboard_payload = {
            'folder_id':          item.get('folder_id', ''),
            'folder_name':        item.get('folder_name', ''),
            'creator_name':       item['client_name'],
            'video_count':        item.get('video_count', 0),
            'raw_footage_link':   raw_footage_link,
            'client_folder_link': client_link or '',
            'project_number':     pnum or '',
        }
        if creator_channel_id:
            dashboard_payload['creator_channel_id'] = creator_channel_id
        if creator_discord_id:
            dashboard_payload['creator_discord_id'] = creator_discord_id
        if folder_created_at:
            dashboard_payload['folder_created_at'] = folder_created_at
        if drive_folder_name:
            dashboard_payload['drive_folder_name'] = drive_folder_name
        await loop.run_in_executor(None, post_dashboard_assignment, dashboard_payload)
    except Exception as e:
        logger.warning(f"handle_ops_assign_request: dashboard detection push failed for "
                        f"{item['client_name']}/{item['folder_name']}: {e}")


def _pop_pending_review(review_id):
    """Atomically remove a review from pending_reviews.json.
    Returns True if it was still pending (caller may finalize), False if already
    taken — this is the guard against double-approval (button + /reviews, or
    two button clicks racing)."""
    if not review_id:
        return True
    with PENDING_REVIEW_LOCK:
        if not os.path.exists(PENDING_REVIEWS_FILE):
            return False
        with open(PENDING_REVIEWS_FILE) as f:
            all_reviews = json.load(f)
        if review_id not in all_reviews:
            return False
        all_reviews.pop(review_id)
        with open(PENDING_REVIEWS_FILE, 'w') as f:
            json.dump(all_reviews, f, indent=2)
    return True


def _approved_review_embed(rd):
    embed = discord.Embed(
        title='✅ Approved & Finalized',
        description=f"{rd['editor_name']} · {rd['client_name']} / {rd['folder_name']} · {rd['videos_done']} videos",
        color=discord.Color.green(),
    )
    drive_links = build_drive_links_field(rd.get('client_root_id'), rd.get('folder_id'), rd.get('edited_subfolder_id'))
    if drive_links:
        embed.add_field(name='Drive Links', value=drive_links, inline=False)
    return embed


async def _approve_review(rd):
    """Shared approve path: pop from pending (dedup guard), finalize, and mark the
    review-channel embed approved. Returns True if this call did the finalize."""
    if not _pop_pending_review(rd.get('review_id')):
        return False
    await finalize_delivery(
        rd.get('discord_message_id'),
        rd['videos_done'],
        rd,
        rd['edited_folder'],
        rd.get('edited_subfolder_id'),
    )
    # Mark the review message in the completion channel as approved (if not the caller's own message)
    msg_id, ch_id = rd.get('review_message_id'), rd.get('review_channel_id')
    if msg_id and ch_id:
        try:
            ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            msg = await ch.fetch_message(msg_id)
            await msg.edit(embed=_approved_review_embed(rd), view=None)
        except Exception as e:
            logger.warning(f'_approve_review: could not edit review message {msg_id}: {e}')
    return True


async def handle_approve_pending_review(item):
    """Queue-triggered approve, e.g. from a one-off ops script — looks up the
    review by id in pending_reviews.json and runs it through the exact same
    _approve_review() path as the Discord button / /reviews dropdown, so
    behavior (finalize_delivery, stats, dedup guard) never diverges."""
    review_id = item.get('review_id')
    with PENDING_REVIEW_LOCK:
        if not os.path.exists(PENDING_REVIEWS_FILE):
            logger.warning(f'handle_approve_pending_review: no pending_reviews.json, review_id={review_id}')
            return
        with open(PENDING_REVIEWS_FILE) as f:
            all_reviews = json.load(f)
        rd = all_reviews.get(review_id)
    if not rd:
        logger.warning(f'handle_approve_pending_review: review_id={review_id} not found (already approved?)')
        return
    ok = await _approve_review(rd)
    logger.info(f'handle_approve_pending_review: review_id={review_id} folder={rd.get("folder_name")} approved={ok}')


def _discrepancy_review_embed(rd, feedback, reviewer_name):
    embed = discord.Embed(
        title='🚫 Discrepancy — Sent Back to Editor',
        description=f"{rd['editor_name']} · {rd['client_name']} / {rd['folder_name']} · {rd['videos_done']} videos",
        color=discord.Color.red(),
    )
    embed.add_field(name='Feedback', value=feedback, inline=False)
    embed.add_field(name='Flagged by', value=reviewer_name, inline=False)
    return embed


class DiscrepancyFeedbackModal(discord.ui.Modal, title='Flag Discrepancy'):
    """Captures what's wrong with a flagged completion, then routes it back to
    the editor through the existing revision pipeline (Notion Status→Revision,
    revision counter, Revision Log, editor-channel embed) so a discrepancy
    decision behaves exactly like any other revision request rather than
    inventing a second, divergent 'send back' mechanism."""
    feedback = discord.ui.TextInput(
        label='What needs to change?',
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Count doesn't match, or wrong footage — Drive shows 4 videos, editor reported 6",
        max_length=1000,
    )

    def __init__(self, review_data):
        super().__init__()
        self._review_data = review_data

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        rd = self._review_data
        if not _pop_pending_review(rd.get('review_id')):
            await interaction.followup.send('This review was already resolved.', ephemeral=True)
            return

        loop = asyncio.get_event_loop()
        editors = await loop.run_in_executor(None, fetch_editors_from_notion)
        editor_info = editors.get(rd['editor_name'])
        if not editor_info:
            logger.error(f"DiscrepancyFeedbackModal: editor '{rd['editor_name']}' not found/inactive — "
                         f"cannot route discrepancy back for {rd['folder_name']}")
            await interaction.followup.send(
                f"⚠️ Popped the review, but couldn't find an active editor channel for "
                f"**{rd['editor_name']}** — tell them about this manually:\n{self.feedback.value}",
                ephemeral=True,
            )
        else:
            await open_revision_assignment(
                rd['client_name'], rd['folder_name'], rd.get('folder_id'), rd['videos_done'],
                rd['editor_name'], editor_info, rd.get('notion_page_id'),
                notes=self.feedback.value,
            )

        msg_id, ch_id = rd.get('review_message_id'), rd.get('review_channel_id')
        if msg_id and ch_id:
            try:
                ch = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
                msg = await ch.fetch_message(msg_id)
                await msg.edit(embed=_discrepancy_review_embed(rd, self.feedback.value, interaction.user.display_name), view=None)
            except Exception as e:
                logger.warning(f'DiscrepancyFeedbackModal: could not edit review message {msg_id}: {e}')
        logger.info(f"DiscrepancyFeedbackModal: flagged {rd['folder_name']} by {rd['editor_name']} — "
                    f"sent back to editor by {interaction.user.display_name}")


class DiscordReviewView(discord.ui.View):
    """Decision buttons shown in the review channel when an editor's completion has flags:
    approve & finalize as-is, or flag a discrepancy and route it back to the editor."""
    def __init__(self, review_data):
        super().__init__(timeout=None)
        self._review_data = review_data
        # Persistent views require every item to have an explicit custom_id — the decorator
        # below leaves it unset, so views silently failed re-registration on every bot restart.
        self.approve.custom_id = f"review_approve_{review_data.get('review_id', '')}"
        self.discrepancy.custom_id = f"review_discrepancy_{review_data.get('review_id', '')}"

    @discord.ui.button(label='🔍 Approve & Finalize', style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if 'Team' not in [r.name for r in interaction.user.roles]:
            await interaction.response.send_message('🚫 Team role required.', ephemeral=True)
            return
        await interaction.response.defer()
        rd = self._review_data
        if not _pop_pending_review(rd.get('review_id')):
            await interaction.followup.send('This review was already approved.', ephemeral=True)
            try:
                await interaction.edit_original_response(embed=_approved_review_embed(rd), view=None)
            except Exception:
                pass
            return
        await finalize_delivery(
            rd.get('discord_message_id'),
            rd['videos_done'],
            rd,
            rd['edited_folder'],
            rd.get('edited_subfolder_id'),
        )
        await interaction.edit_original_response(embed=_approved_review_embed(rd), view=None)
        logger.info(f"DiscordReviewView: approved {rd['folder_name']} by {rd['editor_name']}")

    @discord.ui.button(label='⚠️ Flag Discrepancy', style=discord.ButtonStyle.red)
    async def discrepancy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if 'Team' not in [r.name for r in interaction.user.roles]:
            await interaction.response.send_message('🚫 Team role required.', ephemeral=True)
            return
        await interaction.response.send_modal(DiscrepancyFeedbackModal(self._review_data))


class ReviewApproveSelect(discord.ui.View):
    """Dropdown for /reviews — pick a pending review to approve & finalize."""
    def __init__(self, reviews):
        super().__init__(timeout=180)
        options = [
            discord.SelectOption(
                label=f"{rd['client_name']} / {rd['folder_name']}"[:100],
                value=rid,
                description=f"{rd['editor_name']} · {rd['videos_done']} vids · {len(rd.get('flags', []))} flag(s)"[:100],
                emoji='⚠️',
            )
            for rid, rd in reviews
        ][:25]
        select = discord.ui.Select(placeholder='Approve a review…', options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._reviews = dict(reviews)

    async def _on_select(self, interaction: discord.Interaction):
        rid = interaction.data['values'][0]
        rd  = self._reviews.get(rid)
        await interaction.response.defer(ephemeral=True)
        if not rd:
            await interaction.followup.send('Review not found.', ephemeral=True)
            return
        if await _approve_review(rd):
            await interaction.followup.send(
                f"✅ Approved: **{rd['client_name']} / {rd['folder_name']}** — "
                f"{rd['videos_done']} videos by {rd['editor_name']}. Run `/reviews` again for the rest.",
                ephemeral=True,
            )
            logger.info(f"/reviews: approved {rd['folder_name']} by {rd['editor_name']}")
        else:
            await interaction.followup.send('This review was already approved.', ephemeral=True)


@tree.command(
    name='reviews',
    description='List pending completion reviews and approve them (Team only)',
    guilds=[GUILD_OBJ],
)
async def reviews_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team only.', ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    pending = [(rid, rd) for rid, rd in load_pending_reviews().items() if rd.get('status') == 'pending']
    if not pending:
        await interaction.followup.send('✅ No pending reviews.', ephemeral=True)
        return
    pending.sort(key=lambda x: str(x[1].get('created_at', '')))

    now = datetime.now(timezone.utc)
    lines = []
    for i, (rid, rd) in enumerate(pending, 1):
        try:
            created = datetime.fromisoformat(str(rd.get('created_at')))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = int((now - created).total_seconds() // 3600)
            age = f'{age_h // 24}d {age_h % 24}h' if age_h >= 24 else f'{age_h}h'
        except Exception:
            age = '?'
        flag_txt = '; '.join(f.replace('⚠️ ', '').replace('🚨 ', '') for f in rd.get('flags', []))
        lines.append(
            f"**{i}. {rd['client_name']} / {rd['folder_name']}** — {rd['editor_name']} · "
            f"{rd['videos_done']} vids · {age} old\n└ {flag_txt}"
        )
    desc = '\n'.join(lines)
    if len(desc) > 3900:
        desc = desc[:3900] + '…'
    embed = discord.Embed(
        title=f'⚠️ Pending Reviews ({len(pending)})',
        description=desc,
        color=discord.Color.orange(),
    )
    await interaction.followup.send(embed=embed, view=ReviewApproveSelect(pending), ephemeral=True)


class RemoveFolderSelect(discord.ui.View):
    def __init__(self, rows):
        super().__init__(timeout=120)
        def _emoji(r):
            if r['status'] == 'Pending':   return '⏳'
            if r['status'] == 'Revision':  return '🔄'
            return '🔧'
        options = [
            discord.SelectOption(
                label=f"{r['client_name']} / {r['folder_name']}"[:100],
                value=r['notion_page_id'],
                description=r['status'],
                emoji=_emoji(r),
            )
            for r in rows
        ][:25]
        select = discord.ui.Select(placeholder='Choose folder to remove…', options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._rows = {r['notion_page_id']: r for r in rows}

    async def _on_select(self, interaction: discord.Interaction):
        page_id = interaction.data['values'][0]
        row     = self._rows.get(page_id, {})
        config  = load_config()
        token   = config['notion_token']
        loop    = asyncio.get_event_loop()

        if row.get('status') == 'Revision':
            # Cancel the revision — flip back to Delivered, restore Videos Completed.
            # Don't archive; the row stays in Notion as Delivered.
            now_edt   = datetime.now(EDT)
            today_str = now_edt.strftime('%Y-%m-%d')
            patch_payload = {'properties': {
                'Status':           {'select': {'name': 'Delivered'}},
                'Videos Completed': {'number': row.get('videos_completed', row.get('video_count', 0))},
                'Delivered':        {'date':   {'start': today_str}},
            }}
            resp = await loop.run_in_executor(None, lambda: requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=notion_headers(token),
                json=patch_payload,
                timeout=15,
            ))
            if not resp.ok:
                await interaction.response.edit_message(content='Notion error — could not cancel revision.', view=None)
                return
            await loop.run_in_executor(None, cache_removed_folder, page_id, row, 'Revision')
            await interaction.response.edit_message(
                content=f"✅ Revision cancelled — **{row['client_name']} / {row['folder_name']}** marked as Delivered "
                        f"({row.get('videos_completed', row.get('video_count', 0))} videos).\n"
                        f"Use `/recover` to send it back to Revision.",
                view=None,
            )
        else:
            ok = await loop.run_in_executor(None, archive_active_queue_page, page_id, row)
            if not ok:
                await interaction.response.edit_message(content='Notion error — could not remove folder.', view=None)
                return
            await interaction.response.edit_message(
                content=f"🗑️ Removed **{row['client_name']} / {row['folder_name']}** ({row['status']}).\n"
                        f"Use `/recover` to restore it.",
                view=None,
            )


class RecoverFolderSelect(discord.ui.View):
    def __init__(self, data):
        super().__init__(timeout=120)
        def _emoji(row):
            if row['status'] == 'Pending':   return '⏳'
            if row['status'] == 'Revision':  return '🔄'
            return '🔧'
        # Discord caps a select at 25 options. This used to slice the first 25
        # in dict insertion order, i.e. the OLDEST removals — so anything
        # removed recently was unreachable the moment the cache passed 25
        # entries, with nothing on screen to say so. Newest first is what you
        # actually want: you recover something you just removed by mistake, not
        # something from six weeks ago.
        # `removed_at` is not one type. Entries written before the isoformat
        # switch hold a float epoch, everything since holds an ISO string, and
        # removed_folders.json still carries both (11 floats against 56 strings
        # on 2026-08-22). Sorting them together raises
        # "'<' not supported between instances of 'float' and 'str'" and /recover
        # dies before it can render — which is exactly what happened in
        # #naomi-edits. dashboard.py already normalises these on read; this is
        # the same coercion for the Discord side.
        def _removed_key(kv):
            ra = kv[1].get('removed_at')
            if isinstance(ra, (int, float)):
                try:
                    return datetime.fromtimestamp(ra, tz=timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    return ''
            return ra if isinstance(ra, str) else ''

        rows = sorted(data.items(), key=_removed_key, reverse=True)
        hidden = max(0, len(rows) - 25)
        options = [
            discord.SelectOption(
                label=f"{row['client_name']} / {row['folder_name']}"[:100],
                value=page_id,
                description=row['status'],
                emoji=_emoji(row),
            )
            for page_id, row in rows[:25]
        ]
        placeholder = (
            f'Choose folder to recover… (newest 25 of {len(rows)})'
            if hidden else 'Choose folder to recover…'
        )
        select = discord.ui.Select(placeholder=placeholder, options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        page_id = interaction.data['values'][0]
        loop    = asyncio.get_event_loop()
        row     = await loop.run_in_executor(None, pop_removed_folder, page_id)
        if row is None:
            await interaction.response.edit_message(content='That folder is no longer in the removed cache.', view=None)
            return
        config = load_config()
        token  = config['notion_token']

        if row.get('status') == 'Revision':
            # Page was never archived — just flip Status back to Revision.
            resp = await loop.run_in_executor(None, lambda: requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=notion_headers(token),
                json={'properties': {'Status': {'select': {'name': 'Revision'}}}},
                timeout=15,
            ))
            if not resp.ok:
                await loop.run_in_executor(None, cache_removed_folder, page_id, row, 'Revision')
                await interaction.response.edit_message(content='Notion error — could not recover revision.', view=None)
                return
            await interaction.response.edit_message(
                content=f"♻️ Recovered **{row['client_name']} / {row['folder_name']}** — Status restored to Revision.",
                view=None,
            )
        else:
            resp = await loop.run_in_executor(None, lambda: requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=notion_headers(token),
                json={'archived': False},
                timeout=15,
            ))
            if not resp.ok:
                await loop.run_in_executor(None, cache_removed_folder, page_id, row, row['status'])
                await interaction.response.edit_message(content='Notion error — could not recover folder.', view=None)
                return
            await interaction.response.edit_message(
                content=f"♻️ Recovered **{row['client_name']} / {row['folder_name']}** ({row['status']}).",
                view=None,
            )


@tree.command(
    name='remove',
    description='Remove a folder from the pending or active queue (cached for /recover)',
    guilds=[GUILD_OBJ],
)
async def remove_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team only.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()

    # In an editor's channel, scope to that editor's folders; elsewhere show everything
    editor_result  = await loop.run_in_executor(None, fetch_editor_by_channel_id, interaction.channel_id)
    channel_editor = editor_result[0] if editor_result else None

    rows = await loop.run_in_executor(None, fetch_removable_folders, channel_editor)
    if not rows:
        msg = f'No pending, active, or revision folders to remove for {channel_editor}.' if channel_editor \
            else 'No pending, active, or revision folders to remove.'
        await interaction.followup.send(msg, ephemeral=True)
        return
    await interaction.followup.send(
        '🗑️ Which folder to remove? (Revision folders will be marked Delivered — use `/recover` to restore)',
        view=RemoveFolderSelect(rows),
        ephemeral=True,
    )


@tree.command(
    name='recover',
    description='Restore a folder removed via /remove',
    guilds=[GUILD_OBJ],
)
async def recover_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team only.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()

    # In an editor's channel, scope to that editor's removed folders; elsewhere show everything
    editor_result  = await loop.run_in_executor(None, fetch_editor_by_channel_id, interaction.channel_id)
    channel_editor = editor_result[0] if editor_result else None

    data = await loop.run_in_executor(None, load_removed_folders)
    if channel_editor:
        data = {pid: row for pid, row in data.items() if row.get('editor_name') == channel_editor}
    if not data:
        msg = f'No removed folders to recover for {channel_editor}.' if channel_editor \
            else 'No removed folders to recover.'
        await interaction.followup.send(msg, ephemeral=True)
        return
    await interaction.followup.send(
        '♻️ Which folder to recover?',
        view=RecoverFolderSelect(data),
        ephemeral=True,
    )


@tree.command(
    name='myschedule',
    description='View your current weekly schedule in EST',
    guilds=[GUILD_OBJ],
)
async def myschedule_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()

    editor_name = await loop.run_in_executor(
        None, lambda: (fetch_editor_by_channel_id(interaction.channel_id) or [None])[0]
    )
    if not editor_name:
        await interaction.followup.send('This command only works in your editor channel.', ephemeral=True)
        return

    sched = await loop.run_in_executor(None, fetch_editor_schedule, editor_name)
    if not sched:
        await interaction.followup.send('Could not fetch your schedule from Notion.', ephemeral=True)
        return

    tz_str     = sched.get('timezone', '')
    utc_offset = _parse_utc_offset(tz_str)
    tz_label   = tz_str or 'Unknown TZ'

    embed = discord.Embed(
        title=f'📅 {editor_name}\'s Schedule (EST)',
        description=f'Your timezone: **{tz_label}**\nTimes shown converted to **EST (UTC-5)**',
        color=discord.Color.blurple(),
    )
    for day in SCHEDULE_DAYS:
        raw   = sched.get(day, '')
        est   = _convert_schedule_to_est(raw, utc_offset)
        label = '🟢' if est != 'Off' else '🔴'
        embed.add_field(name=f'{label} {day}', value=est or 'Off', inline=True)

    embed.set_footer(text='Use /changeschedule to request changes')
    await interaction.followup.send(embed=embed, ephemeral=True)


class ScheduleChangeModal(discord.ui.Modal, title='Request Schedule Change'):
    request_input = discord.ui.TextInput(
        label='Describe the change you want',
        style=discord.TextStyle.paragraph,
        placeholder='e.g. "Can I move Wednesday off and work Saturday instead starting next week?"',
        min_length=10,
        max_length=500,
    )

    def __init__(self, editor_name: str):
        super().__init__()
        self._editor_name = editor_name

    async def on_submit(self, interaction: discord.Interaction):
        msg = self.request_input.value.strip()
        loop = asyncio.get_event_loop()

        tg_text = (
            f'📅 Schedule change request from <b>{self._editor_name}</b>:\n\n'
            f'{msg}'
        )
        await loop.run_in_executor(None, send_telegram_html, tg_text)

        await loop.run_in_executor(None, send_discord_ops_channel, None, {
            'title':       '📅 Schedule Change Request',
            'color':       0xf1c40f,
            'description': msg,
            'fields': [
                {'name': 'Editor', 'value': self._editor_name, 'inline': True},
            ],
            'timestamp': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        })

        await interaction.response.send_message(
            '✅ Your request has been sent to Vex. You\'ll be updated when the schedule is changed.',
            ephemeral=True,
        )
        logger.info(f'Schedule change request from {self._editor_name}: {msg[:100]}')


@tree.command(
    name='changeschedule',
    description='Request a change to your weekly schedule',
    guilds=[GUILD_OBJ],
)
async def changeschedule_command(interaction: discord.Interaction):
    loop = asyncio.get_event_loop()
    editor_name = await loop.run_in_executor(
        None, lambda: (fetch_editor_by_channel_id(interaction.channel_id) or [None])[0]
    )
    if not editor_name:
        await interaction.response.send_message(
            'This command only works in your editor channel.', ephemeral=True
        )
        return
    await interaction.response.send_modal(ScheduleChangeModal(editor_name))


# ── Background deadline checker ────────────────────────────────────────────────

@tasks.loop(minutes=30)
async def deadline_checker():
    deadlines = load_deadlines()
    if not deadlines:
        return

    now     = time.time()
    changed = False
    loop    = asyncio.get_event_loop()
    editors = await loop.run_in_executor(None, fetch_editors_from_notion)

    stale_ids = []
    for folder_id, d in deadlines.items():
        # ── Pickup ladder for un-started folders ────────────────────────────
        # pending_start entries have no due_ts yet; instead of a deadline they
        # get escalating pickup nags: 4h gentle → 8h stronger → 12h ops ping,
        # then ops re-pinged every 12h. Editor nags are held while the editor
        # is off-shift (schedule_cache.json) so they don't fire mid-sleep; a
        # footage_flagged folder pauses nagging entirely (ops already knows why).
        if d.get('pending_start'):
            if d.get('footage_flagged'):
                continue
            assigned_at = d.get('assigned_at')
            if not assigned_at:
                continue
            waiting = now - assigned_at
            level   = d.get('pickup_nag_level', 0)
            editor_name = d.get('editor_name', '')
            label = f"**{d.get('client_name')} / {d.get('folder_name')}**"
            wh = int(waiting // 3600)

            # Slow-pickup counters: logged on pure elapsed time (not nag
            # delivery, which can be shift-held), once per assignment; the
            # flags are reset by reset_start_state() so a reassign starts the
            # new editor's clock fresh. footage_flagged entries never reach
            # here (continue above), so flagged folders don't count.
            if editor_name:
                if waiting >= PICKUP_NAG_1_SECS and not d.get('slow_pickup_4h_logged'):
                    increment_editor_counter(editor_name, 'slow_pickups_4h')
                    d['slow_pickup_4h_logged'] = True
                    changed = True
                if waiting >= PICKUP_OPS_SECS and not d.get('slow_pickup_12h_logged'):
                    increment_editor_counter(editor_name, 'slow_pickups_12h')
                    d['slow_pickup_12h_logged'] = True
                    changed = True

            # Ops escalation runs independently of the editor nags below, so an
            # editor being off-shift for a long stretch can't delay the 12h ping.
            # Only when someone actually holds the folder: "assigned to
            # **unassigned** but never started" named nobody and asked for
            # nothing, and it re-posted every 12h forever (founder 2026-08-21:
            # "remove unassigned reminder"). An unassigned folder already has
            # its live card in the assignments channel — that is the reminder.
            if editor_name and waiting >= PICKUP_OPS_SECS and now - d.get('last_pickup_ops_ts', 0) >= PICKUP_OPS_SECS:
                status = ''
                notion_page_id = d.get('notion_page_id')
                if notion_page_id:
                    try:
                        config = load_config()
                        page   = await loop.run_in_executor(None, _notion_get, config['notion_token'], notion_page_id)
                        status = (page.get('properties', {}).get('Status', {}).get('select') or {}).get('name', '')
                    except Exception as e:
                        logger.warning(f'deadline_checker: pickup status check failed for {folder_id}: {e}')
                if status == 'Delivered':
                    stale_ids.append(folder_id)
                    changed = True
                    continue
                try:
                    send_discord_ops_channel(embed={
                        'title': f'⏸️ Not started for {wh}h',
                        'description': f'{label} — assigned to **{editor_name or "unassigned"}** but never started.\n'
                                       f'Consider checking in or reassigning.',
                        'color': 0xe67e22,
                    })
                    d['last_pickup_ops_ts'] = now
                    changed = True
                    logger.info(f'deadline_checker: pickup ops escalation for {folder_id} ({wh}h un-started)')
                except Exception as e:
                    logger.error(f'deadline_checker: pickup ops escalation failed for {folder_id}: {e}')

            if level < 2 and waiting >= (PICKUP_NAG_1_SECS if level == 0 else PICKUP_NAG_2_SECS):
                if editor_name and _editor_on_shift_now(editor_name):
                    info = editors.get(editor_name, {})
                    ch_id, user_id = info.get('discord_channel_id'), info.get('discord_user_id', '')
                    if ch_id:
                        try:
                            ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
                            mention = f'<@{user_id}>' if user_id else None
                            rec = load_assignment_messages().get(folder_id, {})
                            jump = ''
                            if rec.get('message_id') and rec.get('channel_id'):
                                _gid = load_config()['discord_guild_id']
                                jump = (f"\n[Jump to assignment ↗](https://discord.com/channels/"
                                        f"{_gid}/{rec['channel_id']}/{rec['message_id']})")
                            icon = '⏰' if level == 0 else '⚠️'
                            embed = discord.Embed(
                                description=f'{icon} {label} — assigned **{wh}h** ago, not started\n'
                                            f'{jump.strip() or "*(no assignment link on file)*"}',
                                color=discord.Color.gold() if level == 0 else discord.Color.orange(),
                            )
                            await ch.send(content=mention, embed=embed)
                            d['pickup_nag_level'] = level + 1
                            changed = True
                            logger.info(f'deadline_checker: pickup nag {level + 1} sent for {folder_id} → {editor_name}')
                        except Exception as e:
                            logger.error(f'deadline_checker: pickup nag failed for {editor_name}: {e}')
            continue

        if d.get('indefinite') or not d.get('due_ts'):
            continue
        remaining = d['due_ts'] - now

        # Missed deadline: past due and not yet logged
        if remaining <= 0 and not d.get('missed_deadline_logged'):
            editor_name = d.get('editor_name', '')
            if editor_name:
                notion_page_id = d.get('notion_page_id')
                already_delivered = False
                if notion_page_id:
                    try:
                        config = load_config()
                        page   = await loop.run_in_executor(None, _notion_get, config['notion_token'], notion_page_id)
                        status = (page.get('properties', {}).get('Status', {}).get('select') or {}).get('name', '')
                        if status == 'Delivered':
                            already_delivered = True
                    except Exception as e:
                        logger.warning(f'deadline_checker: status check failed for missed check {folder_id}: {e}')
                if not already_delivered:
                    increment_editor_counter(editor_name, 'missed_deadlines')
                    logger.info(f'deadline_checker: missed deadline for {editor_name} — {d.get("folder_name")}')
            d['missed_deadline_logged'] = True
            changed = True

        # ── Escalating overdue reminders ────────────────────────────────────
        # One silent log entry isn't enough — folders have sat 200+ hours
        # overdue unnoticed. Re-ping the editor at +12h, escalate to the ops
        # channel at +24h, then re-escalate there every 24h until delivered.
        overdue = -remaining
        if overdue >= 12 * 3600:
            # Confirm still undelivered before pinging anyone
            notion_page_id = d.get('notion_page_id')
            status = ''
            if notion_page_id:
                try:
                    config = load_config()
                    page   = await loop.run_in_executor(None, _notion_get, config['notion_token'], notion_page_id)
                    status = (page.get('properties', {}).get('Status', {}).get('select') or {}).get('name', '')
                except Exception as e:
                    logger.warning(f'deadline_checker: escalation status check failed for {folder_id}: {e}')
            if status == 'Delivered':
                stale_ids.append(folder_id)
                changed = True
                continue

            editor_name = d.get('editor_name', '')
            label = f"**{d.get('client_name')} / {d.get('folder_name')}**"
            oh = int(overdue // 3600)

            if not d.get('escalated_12h') and editor_name:
                info = editors.get(editor_name, {})
                ch_id, user_id = info.get('discord_channel_id'), info.get('discord_user_id', '')
                if ch_id:
                    try:
                        ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
                        mention = f'<@{user_id}>' if user_id else None
                        embed = discord.Embed(title='🚨 Overdue', description=label, color=discord.Color.red())
                        embed.add_field(name='Overdue By', value=f'{oh}h', inline=False)
                        embed.add_field(name='Action', value="Please deliver or message Vex if you're blocked.", inline=False)
                        await ch.send(content=mention, embed=embed)
                        d['escalated_12h'] = True
                        changed = True
                        logger.info(f'deadline_checker: 12h overdue ping sent for {folder_id} → {editor_name}')
                    except Exception as e:
                        logger.error(f'deadline_checker: 12h overdue ping failed for {editor_name}: {e}')

            # Same rule as the pickup escalation above: no editor, no nag —
            # "**unassigned** has not delivered" is not an action anyone can take.
            if editor_name and overdue >= 24 * 3600 and now - d.get('last_vex_escalation_ts', 0) >= 24 * 3600:
                try:
                    send_discord_ops_channel(embed={
                        'title': f'🚨 Overdue {oh}h',
                        'description': f'{label} — **{editor_name or "unassigned"}** has not delivered.\n'
                                       f'Consider reassigning or checking in.',
                        'color': 0xe74c3c,
                    })
                    d['last_vex_escalation_ts'] = now
                    changed = True
                    logger.info(f'deadline_checker: ops escalation sent for {folder_id} ({oh}h overdue)')
                except Exception as e:
                    logger.error(f'deadline_checker: ops escalation failed for {folder_id}: {e}')

        if d.get('warned_6h') or remaining <= 0:
            continue
        if 0 < remaining <= 6 * 3600:
            # Verify folder is still active in Notion before pinging
            notion_page_id = d.get('notion_page_id')
            if notion_page_id:
                try:
                    config = load_config()
                    page = await loop.run_in_executor(None, _notion_get, config['notion_token'], notion_page_id)
                    status = (page.get('properties', {}).get('Status', {}).get('select') or {}).get('name', '')
                    if status == 'Delivered':
                        logger.info(f'deadline_checker: {folder_id} already Delivered in Notion — removing entry')
                        stale_ids.append(folder_id)
                        changed = True
                        continue
                except Exception as e:
                    logger.warning(f'deadline_checker: Notion status check failed for {folder_id}: {e}')

            editor_name = d.get('editor_name', '')
            info = editors.get(editor_name, {})
            ch_id = info.get('discord_channel_id')
            user_id = info.get('discord_user_id', '')
            if ch_id:
                try:
                    ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
                    h  = int(remaining // 3600)
                    m  = int((remaining % 3600) // 60)
                    mention = f'<@{user_id}>' if user_id else None
                    embed = discord.Embed(
                        title='⏰ Deadline Reminder',
                        description=f'{d.get("client_name")} / {d.get("folder_name")}',
                        color=discord.Color.gold(),
                    )
                    embed.add_field(name='Due In', value=f'{h}h {m}m', inline=False)
                    await ch.send(content=mention, embed=embed)
                    d['warned_6h'] = True
                    changed = True
                    logger.info(f'6h deadline warning sent for {folder_id} → {editor_name}')
                except Exception as e:
                    logger.error(f'deadline_checker: failed to ping {editor_name}: {e}')

    for fid in stale_ids:
        deadlines.pop(fid, None)

    if changed:
        save_deadlines(deadlines)


# ── Pending-review auto re-verify ──────────────────────────────────────────────
# Half of review flags are transient: the editor runs /complete while uploads are
# still landing, so Drive briefly shows fewer videos (or none). Re-check Drive-only
# flags every 10 min for up to 6 tries; if counts now match, auto-approve.
# Name-mismatch / wrong-folder flags need human judgment and are never auto-cleared.

_RECHECK_MAX_ATTEMPTS = 6

def _review_flags_drive_only(rd):
    return all(('Count mismatch' in f) or ('not found in client' in f) for f in rd.get('flags', []))


@tasks.loop(minutes=10)
async def review_recheck_loop():
    pending = [(rid, rd) for rid, rd in load_pending_reviews().items()
               if rd.get('status') == 'pending'
               and _review_flags_drive_only(rd)
               and rd.get('recheck_count', 0) < _RECHECK_MAX_ATTEMPTS]
    if not pending:
        return

    loop = asyncio.get_event_loop()
    for rid, rd in pending:
        try:
            count, _names, _fuzzy, sub_id = await loop.run_in_executor(
                None, find_edited_folder_videos,
                rd.get('folder_id', ''), rd.get('edited_folder', ''), rd.get('client_name'),
            )
        except Exception as e:
            logger.warning(f'review_recheck: Drive check failed for {rd.get("folder_name")}: {e}')
            continue

        if count is not None and count >= rd.get('videos_done', 0):
            rd['edited_subfolder_id'] = rd.get('edited_subfolder_id') or sub_id
            if await _approve_review(rd):
                logger.info(f'review_recheck: auto-approved {rd.get("folder_name")} '
                            f'({rd.get("editor_name")}) — Drive now has {count} videos')
                if COMPLETION_CHANNEL_ID:
                    try:
                        ch = bot.get_channel(COMPLETION_CHANNEL_ID) or await bot.fetch_channel(COMPLETION_CHANNEL_ID)
                        await ch.send(
                            f'🔁 Auto-approved after re-check: **{rd["client_name"]} / {rd["folder_name"]}** — '
                            f'{rd["videos_done"]} videos by {rd["editor_name"]} (Drive now shows {count}).'
                        )
                    except Exception as e:
                        logger.warning(f'review_recheck: completion-channel note failed: {e}')
        else:
            rd['recheck_count'] = rd.get('recheck_count', 0) + 1
            save_pending_review(rid, rd)
            if rd['recheck_count'] == _RECHECK_MAX_ATTEMPTS:
                logger.info(f'review_recheck: giving up on {rd.get("folder_name")} after '
                            f'{_RECHECK_MAX_ATTEMPTS} attempts (drive={count})')


@review_recheck_loop.before_loop
async def before_review_recheck_loop():
    await bot.wait_until_ready()


# ── Leaderboard auto-post task ─────────────────────────────────────────────────

@tasks.loop(hours=1)
async def leaderboard_loop():
    global _leaderboard_last_weekly_post, _leaderboard_last_monthly_post
    now = datetime.utcnow()

    # Weekly: Sunday (weekday=6) at 00:xx UTC — kept in sync with reset_weekly.py's
    # Sunday reset even though this whole branch is currently unused (superseded
    # by weekly_leaderboard_post.py, see WEEKLY_LEADERBOARD_AUTOPOST_ENABLED).
    if WEEKLY_LEADERBOARD_AUTOPOST_ENABLED and now.weekday() == 6 and now.hour == 0:
        today = now.date()
        if _leaderboard_last_weekly_post != today:
            try:
                ch = bot.get_channel(LEADERBOARD_CHANNEL_ID) or await bot.fetch_channel(LEADERBOARD_CHANNEL_ID)
                loop       = asyncio.get_event_loop()
                # Post stats for the week that just ended (last Sun–Sat), not the
                # live "Delivered This Week" counter — reset_weekly.py runs at the
                # same time (Sunday 00:00 UTC) and may have already zeroed it.
                week_start = today - timedelta(days=7)
                week_end   = today - timedelta(days=1)
                editors = await loop.run_in_executor(
                    None, fetch_all_editor_stats_for_range, week_start.isoformat(), today.isoformat())
                title   = f"📊 Weekly Leaderboard — {week_start.strftime('%b %-d')} – {week_end.strftime('%b %-d')}"
                embed   = build_weekly_leaderboard_embed(editors, title=title)
                await ch.send(embed=embed)
                _leaderboard_last_weekly_post = today
                logger.info(f"Auto-posted weekly leaderboard for {today}")
            except Exception as e:
                logger.error(f'Failed to auto-post weekly leaderboard: {e}', exc_info=True)

    # Monthly: last day of month at 23:xx UTC — post weekly + monthly
    last_day   = calendar.monthrange(now.year, now.month)[1]
    month_key  = (now.year, now.month)
    if MONTHLY_LEADERBOARD_AUTOPOST_ENABLED and now.day == last_day and now.hour == 23:
        if _leaderboard_last_monthly_post != month_key:
            try:
                ch = bot.get_channel(LEADERBOARD_CHANNEL_ID) or await bot.fetch_channel(LEADERBOARD_CHANNEL_ID)
                loop    = asyncio.get_event_loop()
                editors = await loop.run_in_executor(None, fetch_all_editor_stats)
                weekly_embed    = build_weekly_leaderboard_embed(editors)
                editors_monthly = sorted(editors, key=lambda x: x['month'], reverse=True)
                monthly_embed   = build_monthly_leaderboard_embed(editors_monthly, now.year, now.month)
                await ch.send(embeds=[weekly_embed, monthly_embed])
                _leaderboard_last_monthly_post = month_key
                logger.info(f"Auto-posted monthly leaderboard for {now.year}-{now.month:02d}")
            except Exception as e:
                logger.error(f'Failed to auto-post monthly leaderboard: {e}', exc_info=True)


@leaderboard_loop.before_loop
async def before_leaderboard_loop():
    await bot.wait_until_ready()


# ── Main ───────────────────────────────────────────────────────────────────────

# Railway is the only place this bot is allowed to log in. Two gateway sessions
# on the same token both receive every interaction, and whichever one loses the
# ack race gets "404 10062 Unknown interaction" — that is what broke /stats on
# 2026-08-19, with a laptop copy running alongside the deployed one. The same
# double-login also double-consumes the dashboard outbox, so editors and
# creators get every ping twice.
#
# A laptop run needs its OWN bot token (a second Discord application) plus
# CC_ALLOW_LOCAL_BOT=1. Never set that flag with the production token.
def refuse_duplicate_login():
    if os.environ.get('CC_ALLOW_LOCAL_BOT') == '1':
        logger.warning('CC_ALLOW_LOCAL_BOT=1 — starting off-Railway. This MUST be a dev bot token.')
        return
    on_railway = any(os.environ.get(k) for k in (
        'RAILWAY_ENVIRONMENT', 'RAILWAY_ENVIRONMENT_NAME',
        'RAILWAY_SERVICE_ID', 'RAILWAY_PROJECT_ID',
    ))
    if on_railway:
        return
    sys.exit(
        'Refusing to start: this bot runs on Railway, and a second login on the '
        'same token steals slash-command interactions (404 10062) and duplicates '
        'every notification.\n'
        'To run a local copy, create a separate Discord application, put its token '
        'in config.json, and start with CC_ALLOW_LOCAL_BOT=1.'
    )


def main():
    refuse_duplicate_login()
    config = load_config()
    bot.run(config['discord_bot_token'])


if __name__ == '__main__':
    main()

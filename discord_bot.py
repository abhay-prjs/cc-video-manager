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
import threading
import time
import traceback
import uuid
import requests
import discord
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
DELIVERY_DATE_PROP      = 'date:Delivered Date:start'  # actual Notion property name in Delivery History DB
TOKEN_FILE           = os.path.join(BASE_DIR, 'token.json')
PENDING_REVIEWS_FILE     = os.path.join(BASE_DIR, 'pending_reviews.json')
PENDING_ASSIGNMENTS_FILE = os.path.join(BASE_DIR, 'pending_assignments.json')
VIDEO_EXTENSIONS     = {'.mp4', '.mov', '.webm', '.avi'}
DRIVE_ROOT_ID        = '1hKXUhKZZo1WN-B5h309CEiSgZbogUoum'

ASSIGNMENT_MESSAGES_FILE  = os.path.join(BASE_DIR, 'assignment_messages.json')
LEADERBOARD_CHANNEL_ID    = 1499407261381038242

QUEUE_LOCK               = FileLock(QUEUE_FILE               + '.lock')
PENDING_REVIEW_LOCK      = FileLock(PENDING_REVIEWS_FILE     + '.lock')
ASSIGNMENT_MESSAGES_LOCK = FileLock(ASSIGNMENT_MESSAGES_FILE + '.lock')

DEADLINES_FILE         = os.path.join(BASE_DIR, 'deadlines.json')
PROJECT_NUMBERS_FILE   = os.path.join(BASE_DIR, 'project_numbers.json')
_DEADLINES_LOCK        = threading.Lock()
_PROJECT_NUMBERS_LOCK  = FileLock(PROJECT_NUMBERS_FILE + '.lock')


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


with open(CONFIG_FILE) as _cf:
    _cfg_tmp = json.load(_cf)
    _GUILD_ID         = int(_cfg_tmp.get('discord_guild_id',   0))
    _CREATOR_GUILD_ID = int(_cfg_tmp.get('creator_guild_id',   0))
GUILD_OBJ         = discord.Object(id=_GUILD_ID)
CREATOR_GUILD_OBJ = discord.Object(id=_CREATOR_GUILD_ID)

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


def fetch_editors_from_notion():
    """Returns {name: {page_id, active, capacity, discord_channel_id, discord_user_id}}.
    Excludes editors where Capacity is None or 0 (treated as inactive)."""
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
            ch_rt      = props.get('Discord Channel ID',{}).get('rich_text', [])
            channel_id = ch_rt[0].get('plain_text', '') if ch_rt else ''
            uid_rt     = props.get('Discord User ID',  {}).get('rich_text', [])
            user_id    = uid_rt[0].get('plain_text', '') if uid_rt else ''
            if name and capacity:
                editors[name] = {
                    'page_id':            page['id'],
                    'active':             active,
                    'capacity':           capacity,
                    'discord_channel_id': channel_id,
                    'discord_user_id':    user_id,
                }
    return editors


def fetch_creator_discord_channel(client_name):
    """Returns Discord Channel ID string for client_name from Creator Assignments DB."""
    return fetch_creator_discord_info(client_name)[0]


def fetch_creator_discord_info(client_name):
    """Returns (channel_id_str, user_id_str) for client_name from Creator Assignments DB."""
    config = load_config()
    token = config['notion_token']
    url = f'https://api.notion.com/v1/databases/{CREATOR_ASSIGNMENTS_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    if resp.ok:
        for page in resp.json().get('results', []):
            props = page['properties']
            name_rt = props.get('Creator/Folder', {}).get('title', [])
            name = name_rt[0].get('plain_text', '') if name_rt else ''
            if name.lower() == client_name.lower():
                ch_rt  = props.get('Discord Channel ID', {}).get('rich_text', [])
                uid_rt = props.get('Discord User ID',    {}).get('rich_text', [])
                ch_id  = ch_rt[0].get('plain_text', '')  if ch_rt  else ''
                u_id   = uid_rt[0].get('plain_text', '') if uid_rt else ''
                return ch_id, u_id
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
            ch = ch_rt[0].get('plain_text', '') if ch_rt else ''
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
        rows.append({'folder_name': folder_name, 'video_count': video_count})
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


def update_editor_active_videos(token, editor_page_id, delta):
    page    = _notion_get(token, editor_page_id)
    current = page.get('properties', {}).get('Active Videos', {}).get('number') or 0
    _notion_patch(token, editor_page_id, {'Active Videos': {'number': max(0, current + delta)}})


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
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    total = 0
    if resp.ok:
        for page in resp.json().get('results', []):
            notes_rt = page['properties'].get('Notes', {}).get('rich_text', [])
            notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m = re.search(r'Videos:\s*(\d+)', notes)
            total += int(m.group(1)) if m else 0

    editors = fetch_editors_from_notion()
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
            ch     = ch_rt[0].get('plain_text', '') if ch_rt else ''
            if ch != target:
                continue
            name_rt = props.get('Editor', {}).get('title', [])
            name    = name_rt[0].get('plain_text', '') if name_rt else ''
            if not name:
                continue
            return name, {
                'active':   props.get('Active Videos',          {}).get('number') or 0,
                'capacity': props.get('Capacity',               {}).get('number') or 70,
                'week':     props.get('Delivered This Week',    {}).get('number') or 0,
                'month':    props.get('Delivered This Month',   {}).get('number') or 0,
                'total':    props.get('Total Videos Delivered', {}).get('number') or 0,
                'avg':      props.get('Avg Turnaround Days',    {}).get('number') or 0,
            }
    return '', {}


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
    """Returns Active Queue rows where Editor == editor_name and Status == In Progress."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': editor_name}},
                {'property': 'Status', 'select': {'equals': 'In Progress'}},
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
            notes_rt    = props.get('Notes', {}).get('rich_text', [])
            notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m           = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            drive_link  = (props.get('Drive Link', {}).get('url') or '')
            m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
            folder_id   = m2.group(1) if m2 else ''
            rows.append({
                'folder_name':          folder_name,
                'client_name':          client_name,
                'video_count':          video_count,
                'folder_id':            folder_id,
                'notion_queue_page_id': page['id'],
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
            rows.append({
                'folder_name':      folder_name,
                'client_name':      client_name,
                'videos_completed': videos,
                'delivered_date':   date_prop,
            })
    return rows


def fetch_delivery_history_for_creator(client_name):
    """Returns all Delivery History rows for client_name, newest first."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    body   = {
        'filter': {'property': 'Client', 'rich_text': {'equals': client_name}},
        'sorts':  [{'property': DELIVERY_DATE_PROP, 'direction': 'descending'}],
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props     = page['properties']
            videos    = props.get('Videos Completed', {}).get('number') or 0
            date_prop = (props.get(DELIVERY_DATE_PROP, {}).get('date') or {}).get('start', '')
            rows.append({'videos_completed': videos, 'delivered_date': date_prop})
    return rows


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
            editors.append({'name': name, 'week': week, 'month': month, 'capacity': capacity})
    return sorted(editors, key=lambda x: x['week'], reverse=True)


def build_weekly_leaderboard_embed(editors, title=None):
    """Builds a Discord embed for the weekly leaderboard."""
    medals = ['🥇', '🥈', '🥉']
    lines  = []
    for i, e in enumerate(editors):
        medal = medals[i] if i < 3 else ''
        prefix = f"{i + 1}. {medal}" if medal else f"{i + 1}."
        lines.append(f"{prefix} {e['name']} — {e['week']} videos")

    today      = datetime.now(EDT).date()
    monday     = today - timedelta(days=today.weekday())
    sunday     = monday + timedelta(days=6)
    week_range = f"Week of {monday.strftime('%b %-d')} — {sunday.strftime('%b %-d')}"

    embed_title = title or '🏆 Weekly Leaderboard'
    embed = discord.Embed(
        title=embed_title,
        description='\n'.join(lines) if lines else 'No data yet.',
        color=discord.Color.gold(),
    )
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
    """Returns Active Queue rows where Status != Delivered: {client_name, folder_name, video_count, status}."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {'filter': {'property': 'Status', 'select': {'does_not_equal': 'Delivered'}}, 'page_size': 100}
    resp   = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows   = []
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
            rows.append({
                'client_name': client_name,
                'folder_name': folder_name,
                'video_count': video_count,
                'status':      status,
            })
    raw_count = sum(1 for r in rows if r['status'] == 'Raw')
    logger.info(f"fetch_active_queue_non_delivered: {len(rows)} total rows, {raw_count} Raw (unassigned)")
    return rows


def fetch_active_queue_in_progress():
    """Returns Active Queue rows where Status is In Progress, oldest first."""
    config = load_config()
    token  = config['notion_token']
    url    = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body   = {
        'filter': {'property': 'Status', 'select': {'equals': 'In Progress'}},
        'sorts':  [{'property': 'Submitted', 'direction': 'ascending'}],
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
    """Returns Delivery History rows where Editor == editor_name AND Delivered Date >= Monday (EDT)."""
    config    = load_config()
    token     = config['notion_token']
    now_edt      = datetime.now(EDT)
    today_str    = now_edt.strftime('%Y-%m-%d')
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    monday_str   = (now_edt - timedelta(days=now_edt.weekday())).strftime('%Y-%m-%d')
    logger.info(f"fetch_delivered_this_week_for_editor: editor={editor_name}, week={monday_str}..{today_str} (EDT)")
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    resp = requests.post(url, headers=notion_headers(token),
                         json=_delivery_history_week_filter(monday_str, tomorrow_str, editor_name), timeout=15)
    rows = _parse_delivery_history_rows(resp.json().get('results', []), include_editor=False) if resp.ok else []
    total = sum(r['videos_completed'] for r in rows)
    logger.info(
        f"fetch_delivered_this_week_for_editor: {editor_name} → {len(rows)} folders, "
        f"{total} videos this week (since {monday_str})"
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


def format_deadline(folder_id):
    """Returns a human-readable deadline string for display in /stats."""
    if not folder_id:
        return None
    d = load_deadlines().get(folder_id)
    if not d:
        return None
    if d.get('indefinite') or not d.get('due_ts'):
        return '♾️ Indefinite'
    remaining = d['due_ts'] - time.time()
    if remaining <= 0:
        return '⛔ Overdue'
    h = int(remaining // 3600)
    m = int((remaining % 3600) // 60)
    label = f'{h}h {m}m left'
    return f'⚠️ {label}' if h < 6 else f'⏰ {label}'


# ── Drive helpers ───────────────────────────────────────────────────────────────

def _drive_escape(name):
    return name.replace('\\', '\\\\').replace("'", "\\'")


def get_drive_service():
    logger.info(f"Loading Drive credentials from: {TOKEN_FILE}")
    creds = Credentials.from_authorized_user_file(
        TOKEN_FILE, ['https://www.googleapis.com/auth/drive']
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
                    'python /home/ubuntu/gdrive_watcher/register_watch.py'
                )
            raise
    return build('drive', 'v3', credentials=creds)


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
            elif os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS:
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
            resp = service.files().list(
                q=f"'{parent_id}' in parents and name='Edited' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id)',
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            edited_dirs = resp.get('files', [])
            if edited_dirs:
                return _count_videos_recursive(service, edited_dirs[0]['id'])
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
            search = service.files().list(
                q=f"'{client_root_id}' in parents and name='Edited' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id)',
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            if search.get('files'):
                edited_id = search['files'][0]['id']
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
            search    = service.files().list(
                q=f"'{parent_id}' in parents and name='Edited' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                fields='files(id)',
                pageSize=1,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            if search.get('files'):
                edited_id = search['files'][0]['id']
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
        root_resp = service.files().list(
            q=f"name='{root_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields='files(id)', pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        if not root_resp.get('files'):
            logger.warning(f"_find_edited_folder_top_down: root folder '{root_name}' not found")
            return None, None
        root_id = root_resp['files'][0]['id']

        safe_name = client_name.replace("'", "\\'")
        client_resp = service.files().list(
            q=f"'{root_id}' in parents and name='{safe_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields='files(id)', pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        if not client_resp.get('files'):
            logger.warning(f"_find_edited_folder_top_down: client folder '{client_name}' not found under root")
            return None, None
        client_root_id = client_resp['files'][0]['id']
        _client_root_folder_cache[client_name] = client_root_id

        edited_resp = service.files().list(
            q=f"'{client_root_id}' in parents and name='Edited' and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields='files(id)', pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        if not edited_resp.get('files'):
            logger.warning(f"_find_edited_folder_top_down: 'Edited' folder not found under client '{client_name}'")
            return client_root_id, None
        return client_root_id, edited_resp['files'][0]['id']
    except Exception as e:
        logger.error(f"_find_edited_folder_top_down error for client '{client_name}': {e}")
        return None, None


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
                resp = service.files().list(
                    q=f"'{parent_id}' in parents and name='Edited' and mimeType='application/vnd.google-apps.folder' and trashed=false",
                    fields='files(id, name)', pageSize=1,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                ).execute()
                if resp.get('files'):
                    edited_folder_id = resp['files'][0]['id']
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

        if not matched:
            logger.info(f"Match result: NOT FOUND. Available folder names: {subfolder_names}")
            return None, [], None, None

        target_id = matched['id']
        logger.info(f"  Matched subfolder: '{matched['name']}' id='{target_id}'")

        all_files = []
        page_token = None
        while True:
            resp3 = service.files().list(
                q=f"'{target_id}' in parents and trashed=false",
                fields='nextPageToken, files(name, mimeType)',
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            all_files.extend(resp3.get('files', []))
            page_token = resp3.get('nextPageToken')
            if not page_token:
                break
        video_names = [
            f['name'] for f in all_files
            if os.path.splitext(f['name'])[1].lower() in VIDEO_EXTENSIONS
        ]
        logger.info(f"  Found {len(video_names)} video(s) in matched subfolder")
        return len(video_names), video_names, fuzzy_note, target_id

    except Exception as e:
        logger.error(f'Drive error finding edited folder: {e}', exc_info=True)
        return None, [], None, None


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
    notion_page_id = a.get('notion_queue_page_id')
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
                submitted_date  = datetime.strptime(submitted_start, '%Y-%m-%d').date()
                turnaround_days = (now_edt.date() - submitted_date).days
            except Exception:
                pass
        drive_link = page_props.get('Drive Link', {}).get('url') or ''

        _notion_patch(token, notion_page_id, {
            'Status':             {'select':    {'name': 'Delivered'}},
            'Videos Completed':   {'number':    confirmed_count},
            'Edited Folder Name': {'rich_text': [{'text': {'content': edited_folder}}]},
            'Delivered':          {'date':      {'start': today_str}},
        })

    # Clear deadline on completion
    folder_id_for_dl = a.get('folder_id', '')
    if folder_id_for_dl:
        deadlines = load_deadlines()
        deadlines.pop(folder_id_for_dl, None)
        save_deadlines(deadlines)

    if editor_page_id:
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

    # Resolve the original assignment message — prefer explicit msg_id, fall back to file lookup
    edit_msg_id = msg_id
    if not edit_msg_id:
        folder_id = a.get('folder_id', '')
        if folder_id:
            edit_msg_id = load_assignment_messages().get(folder_id, {}).get('message_id')

    if edit_msg_id:
        try:
            ch_id = a.get('channel_id')
            ch    = bot.get_channel(ch_id) or await bot.fetch_channel(ch_id)
            orig  = await ch.fetch_message(edit_msg_id)
            embed = discord.Embed(title='✅ Completed', color=discord.Color.green())
            embed.add_field(name='Client',    value=a['client_name'],   inline=False)
            embed.add_field(name='Folder',    value=a['folder_name'],   inline=False)
            embed.add_field(name='Videos',    value=str(confirmed_count), inline=False)
            embed.add_field(name='Delivered', value=today_str,          inline=False)
            await orig.edit(embed=embed, view=None)
        except Exception as e:
            logger.error(f'Failed to edit assignment message: {e}', exc_info=True)

    completion_msg = (
        f"🎬 {a['editor_name']} completed {confirmed_count} videos\n"
        f"Client: {a['client_name']} / {a['folder_name']}\n"
        f"Delivered: {to_ist(now_edt)}"
    )
    send_telegram(completion_msg)

    # Build the edited folder Drive link for the creator notification
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
        }
        logger.info(f"creator_complete_notify payload: {payload}")
        _enqueue_item(payload)
    except Exception as e:
        logger.error(f'Failed to enqueue creator complete notify: {e}', exc_info=True)

    logger.info(f"Finalized: {a['folder_name']} — {confirmed_count} videos by {a['editor_name']}")


# ── Discord finalize handler (called from queue when notion_bridge finalizes) ───

async def handle_discord_finalize(item):
    msg_id          = item.get('discord_message_id')
    ch_id           = item.get('discord_channel_id')
    confirmed_count = item.get('confirmed_count')
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
            )
            await orig.edit(embed=embed, view=None)
            a['status'] = 'delivered'
        else:
            await orig.edit(content=f'✅ Completed: {confirmed_count} videos delivered', view=None)
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

    text = f"📢 **Note from Vex:**\n{message}"
    for editor_name, info in send_to.items():
        ch_id = info.get('discord_channel_id', '')
        if not ch_id:
            logger.warning(f'handle_announce: no Discord channel for {editor_name}')
            continue
        try:
            ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
            await ch.send(text)
            logger.info(f'Announce sent to {editor_name}')
        except Exception as e:
            logger.error(f'handle_announce: failed to send to {editor_name}: {e}')


# ── Creator channel notification ──────────────────────────────────────────────

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

    pnum_line = f"\n{pnum}" if pnum else ''
    msg = (
        f"📁 New folder assigned: {folder_name}\n"
        f"Videos: {video_count}\n"
        f"Editor: {editor_name}\n"
        f"Status: In Progress ⏳{pnum_line}"
    )
    await ch.send(msg)
    logger.info(f'Creator notify sent to {client_name} (channel {channel_id}): {folder_name}')


async def handle_creator_complete_notify(item):
    """Sends delivery completion notification to the creator's Discord channel."""
    client_name     = item.get('client_name', '')
    folder_name     = item.get('folder_name', '')
    editor_name     = item.get('editor_name', '')
    confirmed_count = item.get('confirmed_count', 0)
    edited_folder   = item.get('edited_folder', '')

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

    mention      = f"<@{user_id_str}> " if user_id_str else ''
    edited_folder_drive_link = item.get('edited_folder_drive_link')
    folder_id_for_pnum = item.get('edited_folder_id', '') or item.get('folder_id', '')
    pnum = item.get('project_number') or get_project_number(folder_id_for_pnum)
    pnum_line = f"\n{pnum}" if pnum else ''
    logger.info(f"Sending creator notify, edited_folder_link={edited_folder_drive_link}")

    if edited_folder_drive_link:
        msg = (
            f"{mention}✅ **{folder_name}** has been completed!\n"
            f"Videos delivered: **{confirmed_count}**\n"
            f"Editor: {editor_name}\n\n"
            f"📂 [View Edited Folder]({edited_folder_drive_link}){pnum_line}"
        )
    else:
        logger.warning("No edited folder link in creator_complete_notify")
        msg = (
            f"{mention}✅ **{folder_name}** has been completed!\n"
            f"Videos delivered: **{confirmed_count}**\n"
            f"Editor: {editor_name}{pnum_line}"
        )
    await ch.send(msg)
    logger.info(f'Creator complete notify sent to {client_name} (channel {channel_id}): {folder_name}')


# ── In-memory state ────────────────────────────────────────────────────────────

# message_id (int) → assignment dict
pending_assignments: dict[int, dict] = {}

# client_name → Drive ID of their Edited/ folder (populated on first notify)
_client_edited_folder_cache: dict[str, str] = {}

# client_name → Drive ID of the client root folder (two levels above assignment folder)
_client_root_folder_cache: dict[str, str] = {}

# client_name → Drive ID of the client's Raw Footage folder
_client_raw_footage_folder_cache: dict[str, str] = {}

# Leaderboard auto-post tracking (reset each startup; loop prevents double-posting)
_leaderboard_last_weekly_post: date | None  = None
_leaderboard_last_monthly_post: tuple | None = None  # (year, month)


# ── Embed builder ──────────────────────────────────────────────────────────────

def assignment_embed(client_name, folder_name, video_count, status=None):
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
    return embed


# ── Completion modal ───────────────────────────────────────────────────────────

class CompleteModal(discord.ui.Modal, title='Mark Assignment Complete'):
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

        logger.info(
            f"CompleteModal parsed: editor={editor_name} client={client_name} "
            f"folder={folder_name} folder_id={folder_id} "
            f"videos_done={videos_done} edited_folder='{edited_folder}' "
            f"notion_page_id={notion_page_id}"
        )

        drive_link = ''
        if notion_page_id:
            _cfg = load_config()
            _page = _notion_get(_cfg['notion_token'], notion_page_id)
            drive_link = _page.get('properties', {}).get('Drive Link', {}).get('url') or ''

        original_drive_link = drive_link

        folder_name_match = edited_folder.lower() == folder_name.lower()

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
        if not folder_name_match:
            flags.append(
                f"⚠️ Folder name mismatch: editor said '{edited_folder}' but assigned folder was '{folder_name}'"
            )
        # fuzzy_note is informational — a fuzzy match still counts as found, so no review flag
        if drive_count is not None and drive_count != videos_done:
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

        if client_root_id:
            client_root_link = f'https://drive.google.com/drive/folders/{client_root_id}'
            drive_link_line = f'<a href="{client_root_link}">Client Folder</a>'
            if edited_subfolder_id:
                edited_folder_link = f'https://drive.google.com/drive/folders/{edited_subfolder_id}'
                drive_link_line += f' · <a href="{edited_folder_link}">Edited Folder</a>'
        elif original_drive_link:
            drive_link_line = f'<a href="{original_drive_link}">Raw Footage</a>'
        else:
            drive_link_line = ''

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
                'videos_done':        videos_done,
                'drive_count':        drive_count,
                'edited_folder':      edited_folder,
                'editor_page_id':     a.get('editor_page_id'),
                'notion_page_id':     notion_page_id,
                'flags':              flags,
                'created_at':         datetime.now(timezone.utc).isoformat(),
                'status':             'pending',
            }
            save_pending_review(review_id, review_data)
            keyboard = {
                'inline_keyboard': [[
                    {'text': '🔍 Review', 'callback_data': f'review:{review_id}'}
                ]]
            }
            send_notion_bridge_telegram(tg_msg, keyboard)
            await interaction.edit_original_response(content='⚠️ Submitted for manager review.')
        else:
            send_notion_bridge_telegram(tg_msg)
            await finalize_delivery(a.get('discord_message_id'), videos_done, a, edited_folder, edited_subfolder_id)
            await interaction.edit_original_response(content='✅ Delivery confirmed!')

        logger.info(f"Completion submitted: {folder_name} — {videos_done} videos by {editor_name}")


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
            else:
                folder_view = FolderSelectView(client_rows, client_name, base_assignment)
                await interaction.response.edit_message(
                    content=f'Which folder for {client_name}?', view=folder_view
                )

        select.callback = on_select
        self.add_item(select)


class EditorStatsView(discord.ui.View):
    """View for /editorstats — holds [Show Delivered Today] and [Show In Progress] buttons."""

    def __init__(self, embed: discord.Embed, delivered_rows: list, in_progress_rows: list):
        super().__init__(timeout=600)
        self._embed           = embed
        self._delivered       = delivered_rows
        self._in_progress     = in_progress_rows
        self._detail_shown    = False
        self._progress_shown  = False
        # Update the In Progress button label with the live count
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
                link  = f" — [Open in Drive 🔗]({r['drive_link']})" if r.get('drive_link') else ''
                lines.append(
                    f"• {r['client_name']} / {r['folder_name']} — {r['editor_name']} — {r['videos_completed']} videos{link}"
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
                lines.append(
                    f"• {r['client_name']} / {r['folder_name']} — {editor} — {r['video_count']} videos — since {date_str}{dl_part}"
                )
            field_value = '\n'.join(lines)
            if len(field_value) > 1020:
                field_value = field_value[:1020] + '…'
        else:
            field_value = 'No folders in progress'

        self._embed.add_field(
            name='⏳ In Progress — Detail',
            value=field_value,
            inline=False,
        )
        button.disabled = True
        button.label    = '⏳ In Progress (loaded)'
        await interaction.edit_original_response(embed=self._embed, view=self)


# ── Discord client ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.guilds = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)


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
    if not leaderboard_loop.is_running():
        leaderboard_loop.start()
    if not deadline_checker.is_running():
        deadline_checker.start()


@tree.command(name='stats', description='View your video stats', guilds=[GUILD_OBJ, CREATOR_GUILD_OBJ])
async def stats_command(interaction: discord.Interaction):
    await interaction.response.defer()

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
        fresh_active, (active_rows, history_rows, today_rows, week_rows) = await asyncio.gather(
            loop.run_in_executor(None, recalculate_active_videos, token, editor_name),
            asyncio.gather(
                loop.run_in_executor(None, fetch_active_queue_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivery_history_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivered_today_for_editor, editor_name),
                loop.run_in_executor(None, fetch_delivered_this_week_for_editor, editor_name),
            ),
        )
        today_videos = sum(r['videos_completed'] for r in today_rows)
        week_videos  = sum(r['videos_completed'] for r in week_rows)
        # Use the higher of the live Delivery History query and the Editor Profiles counter —
        # old rows have no dates so the live query may undercount for editors with prior deliveries.
        week_videos  = max(week_videos, editor_data.get('week', 0))

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
                lines.append(
                    f"• {r['client_name']} / {r['folder_name']} — {r['status']} — {r['video_count']} videos{dl_part}"
                )
            embed.add_field(
                name=f"📁 Active Folders ({len(active_rows)})",
                value='\n'.join(lines),
                inline=False,
            )
        else:
            embed.add_field(name='📁 Active Folders (0)', value='None', inline=False)

        embed.add_field(
            name='✅ Delivered',
            value=(
                f"• Today: {today_videos} videos\n"
                f"• This week: {week_videos} videos\n"
                f"• This month: {editor_data['month']} videos\n"
                f"• All time: {editor_data['total']} videos"
            ),
            inline=False,
        )

        valid_history = [r for r in history_rows if (r['videos_completed'] or 0) >= 1]
        if valid_history:
            lines = [
                f"• {r['client_name']} / {r['folder_name']} — {r['videos_completed']} videos — {r['delivered_date']}"
                for r in valid_history
            ]
            embed.add_field(
                name='📋 Completed Folders (last 10)',
                value='\n'.join(lines),
                inline=False,
            )

        await interaction.followup.send(embed=embed)

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

        queue_rows, pending_rows = await asyncio.gather(
            loop.run_in_executor(None, fetch_active_queue_for_creator, client_name),
            loop.run_in_executor(None, fetch_pending_assignments_for_creator, client_name),
        )
        statuses = [r['status'] for r in queue_rows]
        logger.info(f"/stats creator {client_name}: {len(queue_rows)} rows, statuses={statuses}")

        active_rows = [r for r in queue_rows if r['status'] != 'Delivered']
        logger.info(f"/stats creator {client_name}: active={len(active_rows)}, pending(unassigned)={len(pending_rows)}")

        embed = discord.Embed(title=f'📊 Stats for {client_name}', color=discord.Color.blurple())

        if active_rows:
            lines = [
                f"• {r['folder_name']} — {r['editor_name'] or 'Unassigned'} — {r['status']} — {r['video_count']} videos"
                for r in active_rows
            ]
            embed.add_field(
                name=f'📁 Active Folders ({len(active_rows)})',
                value='\n'.join(lines),
                inline=False,
            )
        else:
            embed.add_field(name='📁 Active Folders (0)', value='None', inline=False)

        if pending_rows:
            pending_lines = [
                f"• {r['folder_name']} — {r['video_count']} videos — awaiting assignment"
                for r in pending_rows
            ]
            embed.add_field(
                name=f'⏳ Pending ({len(pending_rows)})',
                value='\n'.join(pending_lines),
                inline=False,
            )
        else:
            embed.add_field(name='⏳ Pending (0)', value='None', inline=False)

        await interaction.followup.send(embed=embed)

    else:
        await interaction.followup.send('This server is not configured.', ephemeral=True)


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

    editor_loads, active_rows, delivered_today, in_progress_rows = await asyncio.gather(
        loop.run_in_executor(None, fetch_editor_loads_list),
        loop.run_in_executor(None, fetch_active_queue_non_delivered),
        loop.run_in_executor(None, fetch_delivered_today),
        loop.run_in_executor(None, fetch_active_queue_in_progress),
    )

    unassigned = [r for r in active_rows if r['status'] == 'Raw']

    delivered_folder_count = len(delivered_today)
    delivered_video_total  = sum(r['videos_completed'] for r in delivered_today)

    embed = discord.Embed(
        title='📊 Overall Operations — CC Video Manager',
        color=discord.Color.blurple(),
    )

    # ── Editor Load ────────────────────────────────────────────────────────────
    if editor_loads:
        load_lines = [f"• {e['name']}: {round((e['active'] / e['capacity']) * 100) if e['capacity'] > 0 else 0}%" for e in editor_loads]
        embed.add_field(name='⚙️ Editor Load', value='\n'.join(load_lines), inline=False)
    else:
        embed.add_field(name='⚙️ Editor Load', value='No editors found', inline=False)

    # ── Unassigned Folders ─────────────────────────────────────────────────────
    if unassigned:
        ua_lines = [
            f"• {r['client_name']} / {r['folder_name']} — {r['video_count']} videos"
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

    view = EditorStatsView(embed, delivered_today, in_progress_rows)
    await interaction.followup.send(embed=embed, view=view)


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
            'active assignments with deadlines remaining.\n'
            '**Who:** Editors & Creators'
        ),
        inline=False,
    )

    embed.add_field(
        name='✅ /complete',
        value=(
            'Mark an assignment as done.\n'
            '**How:** Run in your editor channel → enter the edited folder name and video count '
            '→ bot verifies against Drive → sends review to Vex on Telegram.\n'
            '**Who:** Editors (run in your assigned channel)'
        ),
        inline=False,
    )

    embed.add_field(
        name='🏆 /leaderboard',
        value=(
            'Editor leaderboard sorted by videos delivered.\n'
            '**Shows:** Weekly rankings for everyone. Team members also see the monthly board.\n'
            '**Who:** Everyone'
        ),
        inline=False,
    )

    if is_team:
        embed.add_field(
            name='📋 /editorstats  〔Team〕',
            value=(
                'Full ops overview.\n'
                '**Shows:** Editor load %, unassigned folders, delivered today, in-progress count. '
                'Buttons to expand delivered today and in-progress folder lists.\n'
                '**Who:** Team only'
            ),
            inline=False,
        )

        embed.add_field(
            name='🔁 /reassign  〔Team〕',
            value=(
                'Move an in-progress folder to a different editor.\n'
                '**How:** Select the folder from the dropdown → select the new editor '
                '→ Notion updates, deadline transfers, new Discord assignment embed posts.\n'
                '**Who:** Team only'
            ),
            inline=False,
        )

        embed.add_field(
            name='⏱️ /extend  〔Team〕',
            value=(
                "Extend a folder's deadline.\n"
                '**How:** Select the folder → enter hours to add (enter `0` to set Indefinite).\n'
                '**Who:** Team only'
            ),
            inline=False,
        )

        embed.add_field(
            name='🩺 /health  〔Team〕',
            value=(
                'Show the last 10 errors and warnings from the bot log.\n'
                '**Who:** Team only'
            ),
            inline=False,
        )

    embed.set_footer(text='Team-only commands are hidden from non-Team members.')
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
    loop    = asyncio.get_event_loop()
    editors = await loop.run_in_executor(None, fetch_all_editor_stats)

    member_roles = [r.name for r in interaction.user.roles]
    is_team      = 'Team' in member_roles

    weekly_embed = build_weekly_leaderboard_embed(editors)

    if is_team:
        editors_monthly = sorted(editors, key=lambda x: x['month'], reverse=True)
        now = datetime.now(EDT)
        monthly_embed = build_monthly_leaderboard_embed(editors_monthly, now.year, now.month)
        await interaction.followup.send(embeds=[weekly_embed, monthly_embed])
    else:
        await interaction.followup.send(embed=weekly_embed)


@tree.command(name='complete', description='Mark a folder as complete', guilds=[GUILD_OBJ])
async def complete_command(interaction: discord.Interaction):
    loop       = asyncio.get_event_loop()
    channel_id = interaction.channel_id

    editor_name, _ = await loop.run_in_executor(None, fetch_editor_by_channel_id, channel_id)
    if not editor_name:
        await interaction.response.send_message(
            'This channel is not registered as an editor channel.', ephemeral=True
        )
        return

    editors        = await loop.run_in_executor(None, fetch_editors_from_notion)
    editor_page_id = editors.get(editor_name, {}).get('page_id', '')

    rows = await loop.run_in_executor(None, fetch_in_progress_for_editor, editor_name)

    if not rows:
        await interaction.response.send_message('No active assignments found.', ephemeral=True)
        return

    base = {'editor_name': editor_name, 'editor_page_id': editor_page_id, 'channel_id': channel_id}

    if len(rows) == 1:
        await interaction.response.send_modal(CompleteModal({**rows[0], **base}))
        return

    unique_clients = list(dict.fromkeys(r['client_name'] for r in rows))
    if len(unique_clients) == 1:
        client_name = unique_clients[0]
        view = FolderSelectView(rows, client_name, base)
        await interaction.response.send_message(
            f'Which folder for {client_name}?', view=view, ephemeral=True
        )
    else:
        view = ClientSelectView(rows, base)
        await interaction.response.send_message(
            'Which client?', view=view, ephemeral=True
        )


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
            safe_name = _drive_escape(client_name)
            resp = service.files().list(
                q=(f"'{DRIVE_ROOT_ID}' in parents and name='{safe_name}' "
                   f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
                fields='files(id)', pageSize=1,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            if resp.get('files'):
                client_root_id = resp['files'][0]['id']
                _client_root_folder_cache[client_name] = client_root_id
                logger.info(f"find_assignment_drive_links: cached client_root_id={client_root_id} for '{client_name}'")

        if not client_root_id:
            logger.warning(f"find_assignment_drive_links: client folder '{client_name}' not found under root")
            return None, None

        client_folder_link = f'https://drive.google.com/drive/folders/{client_root_id}'

        # Step 2: Raw Footage inside client folder (use cache)
        raw_footage_id = _client_raw_footage_folder_cache.get(client_name)
        if not raw_footage_id:
            resp2 = service.files().list(
                q=(f"'{client_root_id}' in parents and name='Raw Footage' "
                   f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
                fields='files(id)', pageSize=1,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            if resp2.get('files'):
                raw_footage_id = resp2['files'][0]['id']
                _client_raw_footage_folder_cache[client_name] = raw_footage_id

        if not raw_footage_id:
            logger.warning(f"find_assignment_drive_links: 'Raw Footage' not found for '{client_name}'")
            return client_folder_link, None

        # Step 3: assigned subfolder inside Raw Footage
        safe_folder = _drive_escape(folder_name)
        resp3 = service.files().list(
            q=(f"'{raw_footage_id}' in parents and name='{safe_folder}' "
               f"and mimeType='application/vnd.google-apps.folder' and trashed=false"),
            fields='files(id)', pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()

        if resp3.get('files'):
            subfolder_id   = resp3['files'][0]['id']
            subfolder_link = f'https://drive.google.com/drive/folders/{subfolder_id}'
            logger.info(f"find_assignment_drive_links: found subfolder '{folder_name}' id={subfolder_id}")
            return client_folder_link, subfolder_link

        logger.warning(f"find_assignment_drive_links: subfolder '{folder_name}' not found in Raw Footage")
        return client_folder_link, None

    except Exception as e:
        logger.error(f'Drive error finding assignment links for {client_name}/{folder_name}: {e}')
        return None, None


# ── assign_folder (public API, also called from queue) ─────────────────────────

async def assign_folder(
    client_name: str,
    folder_name: str,
    video_count: int,
    folder_id: str,
    editor_name: str,
    notion_queue_page_id: str = None,
    project_number: str = '',
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
    recalculate_active_videos(token, editor_name)

    if folder_id:
        deadlines = load_deadlines()
        entry = deadlines.get(folder_id, {})
        # Clock started at notification time; just stamp in the editor name.
        # If no entry exists yet (edge case), start the clock now.
        if not entry:
            entry = {
                'due_ts':     time.time() + 86400,
                'indefinite': False,
                'warned_6h':  False,
            }
        entry['editor_name']   = editor_name
        entry['client_name']   = client_name
        entry['folder_name']   = folder_name
        entry['notion_page_id'] = notion_queue_page_id
        deadlines[folder_id]   = entry
        save_deadlines(deadlines)

    # Fetch Drive links top-down (non-blocking)
    loop = asyncio.get_event_loop()
    client_folder_link, raw_footage_link = await loop.run_in_executor(
        None, find_assignment_drive_links, client_name, folder_name
    )

    pnum = project_number or get_project_number(folder_id)
    title = f'📁 New Assignment  {pnum}' if pnum else '📁 New Assignment'
    embed = discord.Embed(title=title, color=discord.Color.blue())
    embed.add_field(name='Client', value=client_name, inline=False)
    embed.add_field(name='Folder', value=folder_name, inline=False)
    embed.add_field(name='Videos', value=str(video_count), inline=False)
    if client_folder_link or raw_footage_link:
        link_parts = []
        if client_folder_link:
            link_parts.append(f"📂 [Client Folder]({client_folder_link})")
        if raw_footage_link:
            link_parts.append(f"📁 [Raw Footage Folder]({raw_footage_link})")
        embed.add_field(name='Drive Links', value='\n'.join(link_parts), inline=False)
    embed.set_footer(text='⚠️ More videos may be added — you\'ll be notified if count increases.')

    user_id = info.get('discord_user_id', '')
    content = f"<@{user_id}>" if user_id else None
    sent    = await ch.send(content=content, embed=embed)

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

    send_telegram(f"{editor_name} has been assigned {client_name}/{folder_name}")
    logger.info(f'Assignment sent: {folder_name} → {editor_name} (channel {channel_id})')


# ── Folder update message ──────────────────────────────────────────────────────

async def send_folder_update_msg(item):
    """Sends a plain update message to the assigned editor's Discord channel."""
    editors = fetch_editors_from_notion()
    editor_name = item['editor_name']
    info = editors.get(editor_name)
    if not info or not info.get('discord_channel_id'):
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
    diff = item.get('diff', item['new_count'] - item['previous_count'])
    msg = (
        f"📥 **Folder Updated** — {item['client_name']} / {item['folder_name']}\n"
        f"{item['previous_count']} → {item['new_count']} videos (+{diff} added)\n"
        f"Please check the folder for new files."
    )
    await ch.send(msg)
    logger.info(f"Update sent to {editor_name}: {item['folder_name']} {item['previous_count']}→{item['new_count']}")


# ── Queue poller: IPC from notion_bridge.py ────────────────────────────────────

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
                elif item.get('type') == 'creator_notify':
                    await handle_creator_notify(item)
                elif item.get('type') == 'creator_complete_notify':
                    await handle_creator_complete_notify(item)
                elif item.get('type') == 'announce':
                    await handle_announce(item)
                else:
                    await assign_folder(
                        item['client_name'],
                        item['folder_name'],
                        item['video_count'],
                        item.get('folder_id', ''),
                        item['editor_name'],
                        item.get('notion_queue_page_id'),
                        item.get('project_number', ''),
                    )
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

        if hours == 0:
            entry['indefinite'] = True
            entry['due_ts']     = None
            entry['warned_6h']  = False
            msg = f'♾️ **{self._folder_label}** set to **Indefinite** — no deadline until you set one or editor completes it.'
        else:
            entry['indefinite'] = False
            entry['warned_6h']  = False
            base_ts = entry.get('due_ts') or time.time()
            if base_ts < time.time():
                base_ts = time.time()
            entry['due_ts'] = base_ts + hours * 3600
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
    rows = await loop.run_in_executor(None, fetch_active_queue_in_progress)

    # Attach deadline info so the select shows current status
    rows_with_id = [r for r in rows if r.get('folder_id') or r.get('submitted_date')]
    # fetch_active_queue_in_progress already includes folder_id
    if not rows_with_id:
        await interaction.followup.send('No in-progress folders found.', ephemeral=True)
        return

    view = ExtendFolderSelect(rows_with_id)
    await interaction.followup.send('Which folder?', view=view, ephemeral=True)


# ── Reassign command (Discord) ─────────────────────────────────────────────────

class ReassignEditorSelect(discord.ui.View):
    def __init__(self, folder_id, client_name, folder_name, video_count, notion_page_id, editors):
        super().__init__(timeout=120)
        self._folder_id    = folder_id
        self._client_name  = client_name
        self._folder_name  = folder_name
        self._video_count  = video_count
        self._notion_page_id = notion_page_id
        options = [discord.SelectOption(label=e, value=e) for e in editors][:25]
        select  = discord.ui.Select(placeholder='Choose new editor…', options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        new_editor = interaction.data['values'][0]
        await interaction.response.defer()

        config = load_config()
        token  = config['notion_token']

        # Update Notion: set new editor, keep In Progress
        if self._notion_page_id:
            requests.patch(
                f'https://api.notion.com/v1/pages/{self._notion_page_id}',
                headers=notion_headers(token),
                json={'properties': {
                    'Editor': {'select': {'name': new_editor}},
                    'Status': {'select': {'name': 'In Progress'}},
                }},
                timeout=15,
            )

        # Update deadline to new editor
        if self._folder_id:
            deadlines = load_deadlines()
            entry = deadlines.get(self._folder_id, {})
            entry['editor_name'] = new_editor
            entry['warned_6h']   = False
            deadlines[self._folder_id] = entry
            save_deadlines(deadlines)

        recalculate_active_videos(token, new_editor)

        # Send Discord assignment notification to new editor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _enqueue_reassign,
            self._client_name, self._folder_name, self._video_count,
            self._folder_id, new_editor, self._notion_page_id)

        await interaction.edit_original_response(
            content=f'✅ **{self._client_name} / {self._folder_name}** reassigned to **{new_editor}**.',
            view=None,
        )
        logger.info(f'Reassigned {self._folder_name} → {new_editor}')


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
            })
            with open(QUEUE_FILE, 'w') as f:
                json.dump(existing, f, indent=2)
    except Exception as e:
        logger.error(f'_enqueue_reassign failed: {e}')


class ReassignFolderSelect(discord.ui.View):
    def __init__(self, in_progress_rows, editors):
        super().__init__(timeout=120)
        self._editors = editors
        options = [
            discord.SelectOption(
                label=f"{r['client_name']} / {r['folder_name']}"[:100],
                value=r.get('folder_id', '') or r['folder_name'],
            )
            for r in in_progress_rows
        ][:25]
        select = discord.ui.Select(placeholder='Choose folder to reassign…', options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._rows = {
            (r.get('folder_id', '') or r['folder_name']): r
            for r in in_progress_rows
        }

    async def _on_select(self, interaction: discord.Interaction):
        key = interaction.data['values'][0]
        r   = self._rows.get(key, {})
        view = ReassignEditorSelect(
            folder_id    = r.get('folder_id', ''),
            client_name  = r.get('client_name', ''),
            folder_name  = r.get('folder_name', ''),
            video_count  = r.get('video_count', 0),
            notion_page_id = r.get('notion_queue_page_id', ''),
            editors      = self._editors,
        )
        await interaction.response.edit_message(
            content=f"Reassigning **{r.get('client_name')} / {r.get('folder_name')}** — pick new editor:",
            view=view,
        )


@tree.command(
    name='reassign',
    description='Reassign an in-progress folder to a different editor',
    guilds=[GUILD_OBJ],
)
async def reassign_command(interaction: discord.Interaction):
    if 'Team' not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message('🚫 Team only.', ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    loop = asyncio.get_event_loop()
    rows, editors_map = await asyncio.gather(
        loop.run_in_executor(None, fetch_active_queue_in_progress),
        loop.run_in_executor(None, fetch_editors_from_notion),
    )
    if not rows:
        await interaction.followup.send('No in-progress folders.', ephemeral=True)
        return

    editors = list(editors_map.keys())
    view    = ReassignFolderSelect(rows, editors)
    await interaction.followup.send('Which folder?', view=view, ephemeral=True)


# ── Background deadline checker ────────────────────────────────────────────────

@tasks.loop(minutes=30)
async def deadline_checker():
    deadlines = load_deadlines()
    if not deadlines:
        return

    now     = time.time()
    changed = False
    editors = fetch_editors_from_notion()

    for folder_id, d in deadlines.items():
        if d.get('indefinite') or d.get('warned_6h') or not d.get('due_ts'):
            continue
        remaining = d['due_ts'] - now
        if 0 < remaining <= 6 * 3600:
            editor_name = d.get('editor_name', '')
            info = editors.get(editor_name, {})
            ch_id = info.get('discord_channel_id')
            user_id = info.get('discord_user_id', '')
            if ch_id:
                try:
                    ch = bot.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
                    h  = int(remaining // 3600)
                    m  = int((remaining % 3600) // 60)
                    mention = f'<@{user_id}> ' if user_id else ''
                    await ch.send(
                        f'⏰ {mention}**Deadline reminder:** '
                        f'**{d.get("client_name")} / {d.get("folder_name")}** '
                        f'is due in **{h}h {m}m**.'
                    )
                    d['warned_6h'] = True
                    changed = True
                    logger.info(f'6h deadline warning sent for {folder_id} → {editor_name}')
                except Exception as e:
                    logger.error(f'deadline_checker: failed to ping {editor_name}: {e}')

    if changed:
        save_deadlines(deadlines)


# ── Leaderboard auto-post task ─────────────────────────────────────────────────

@tasks.loop(hours=1)
async def leaderboard_loop():
    global _leaderboard_last_weekly_post, _leaderboard_last_monthly_post
    now = datetime.utcnow()

    # Weekly: Monday (weekday=0) at 00:xx UTC
    if now.weekday() == 0 and now.hour == 0:
        today = now.date()
        if _leaderboard_last_weekly_post != today:
            try:
                ch = bot.get_channel(LEADERBOARD_CHANNEL_ID) or await bot.fetch_channel(LEADERBOARD_CHANNEL_ID)
                loop    = asyncio.get_event_loop()
                editors = await loop.run_in_executor(None, fetch_all_editor_stats)
                monday  = today - timedelta(days=today.weekday())
                sunday  = monday + timedelta(days=6)
                title   = f"📊 Weekly Leaderboard — {monday.strftime('%b %-d')} – {sunday.strftime('%b %-d')}"
                embed   = build_weekly_leaderboard_embed(editors, title=title)
                await ch.send(embed=embed)
                _leaderboard_last_weekly_post = today
                logger.info(f"Auto-posted weekly leaderboard for {today}")
            except Exception as e:
                logger.error(f'Failed to auto-post weekly leaderboard: {e}', exc_info=True)

    # Monthly: last day of month at 23:xx UTC — post weekly + monthly
    last_day   = calendar.monthrange(now.year, now.month)[1]
    month_key  = (now.year, now.month)
    if now.day == last_day and now.hour == 23:
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

def main():
    config = load_config()
    bot.run(config['discord_bot_token'])


if __name__ == '__main__':
    main()

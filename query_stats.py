"""
query_stats.py
CLI tool for querying Notion databases and printing clean stats output.

Usage:
    python3 query_stats.py --today
    python3 query_stats.py --load
    python3 query_stats.py --pending
    python3 query_stats.py --editor Iana
    python3 query_stats.py --client Chris
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
import requests

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

ACTIVE_QUEUE_DB    = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def notion_query(token, db_id, body=None):
    resp = requests.post(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        headers=headers(token),
        json=body or {},
        timeout=15,
    )
    if not resp.ok:
        print(f'Notion error {resp.status_code}: {resp.text}', file=sys.stderr)
        sys.exit(1)
    return resp.json().get('results', [])


def prop_title(props, key):
    rt = props.get(key, {}).get('title', [])
    return rt[0].get('plain_text', '') if rt else ''


def prop_text(props, key):
    rt = props.get(key, {}).get('rich_text', [])
    return rt[0].get('plain_text', '') if rt else ''


def prop_select(props, key):
    sel = props.get(key, {}).get('select') or {}
    return sel.get('name', '')


def prop_number(props, key):
    return props.get(key, {}).get('number') or 0


def prop_date(props, key):
    return (props.get(key, {}).get('date') or {}).get('start', '')


def video_count_from_notes(notes):
    m = re.search(r'Videos:\s*(\d+)', notes)
    return int(m.group(1)) if m else 0


# ── --today ───────────────────────────────────────────────────────────────────

def cmd_today(token):
    EDT = timezone(timedelta(hours=-4))
    now_edt      = datetime.now(EDT)
    today_str    = now_edt.strftime('%Y-%m-%d')
    tomorrow_str = (now_edt + timedelta(days=1)).strftime('%Y-%m-%d')
    pages = notion_query(token, DELIVERY_HISTORY_DB, {
        'filter': {
            'and': [
                {'property': 'Delivered Date', 'date': {'on_or_after': today_str}},
                {'property': 'Delivered Date', 'date': {'before': tomorrow_str}},
            ]
        },
    })

    if not pages:
        print(f'No deliveries recorded today ({today_str}).')
        return

    total_videos = 0
    lines = []
    for p in pages:
        props   = p['properties']
        folder  = prop_title(props, 'Folder')
        client  = prop_text(props, 'Client')
        editor  = prop_select(props, 'Editor')
        videos  = prop_number(props, 'Videos Completed')
        total_videos += videos
        lines.append(f'  {editor}: {client} / {folder} — {videos} videos')

    print(f'Delivered today ({today_str}) — {len(pages)} folder(s), {total_videos} videos total:')
    for line in lines:
        print(line)


# ── --load ────────────────────────────────────────────────────────────────────

def cmd_load(token):
    pages = notion_query(token, EDITOR_PROFILES_DB)

    editors = []
    for p in pages:
        props    = p['properties']
        name     = prop_title(props, 'Editor')
        active   = prop_number(props, 'Active Videos')
        capacity = prop_number(props, 'Capacity') or 70
        if not name:
            continue
        pct = round((active / capacity) * 100)
        editors.append((name, active, capacity, pct))

    if not editors:
        print('No editor profiles found.')
        return

    editors.sort(key=lambda x: x[0])
    print('Editor load:')
    for name, active, capacity, pct in editors:
        bar = '#' * (pct // 10) + '-' * (10 - pct // 10)
        print(f'  {name}: {active}/{capacity} [{bar}] {pct}%')


# ── --pending ─────────────────────────────────────────────────────────────────

def cmd_pending(token):
    pages = notion_query(token, ACTIVE_QUEUE_DB, {
        'filter': {'property': 'Status', 'select': {'equals': 'Raw'}},
        'sorts':  [{'property': 'Submitted', 'direction': 'ascending'}],
    })

    if not pages:
        print('No unassigned folders.')
        return

    print(f'Unassigned folders ({len(pages)}):')
    for p in pages:
        props   = p['properties']
        folder  = prop_title(props, 'Video')
        client  = prop_text(props, 'Creator')
        notes   = prop_text(props, 'Notes')
        videos  = video_count_from_notes(notes)
        submitted = prop_date(props, 'Submitted')
        age = ''
        if submitted:
            try:
                delta = date.today() - datetime.fromisoformat(submitted).date()
                age = f', {delta.days}d ago'
            except Exception:
                pass
        print(f'  {client} / {folder} — {videos} videos{age}')


# ── --editor ──────────────────────────────────────────────────────────────────

def cmd_editor(token, name):
    # Find editor profile (case-insensitive prefix match)
    profile_pages = notion_query(token, EDITOR_PROFILES_DB)
    profile = None
    matched_name = name
    for p in profile_pages:
        props   = p['properties']
        editor  = prop_title(props, 'Editor')
        if editor.lower().startswith(name.lower()):
            profile      = props
            matched_name = editor
            break

    if not profile:
        print(f'Editor not found: {name}')
        return

    active   = prop_number(profile, 'Active Videos')
    capacity = prop_number(profile, 'Capacity') or 70
    week     = prop_number(profile, 'Delivered This Week')
    month    = prop_number(profile, 'Delivered This Month')
    total    = prop_number(profile, 'Total Videos Delivered')
    pct      = round((active / capacity) * 100)

    print(f'Editor: {matched_name}')
    print(f'  Load:  {active}/{capacity} ({pct}%)')
    print(f'  Delivered this week:  {week}')
    print(f'  Delivered this month: {month}')
    print(f'  All time:             {total}')

    # Active folders
    active_pages = notion_query(token, ACTIVE_QUEUE_DB, {
        'filter': {
            'and': [
                {'property': 'Editor', 'select': {'equals': matched_name}},
                {'property': 'Status', 'select': {'does_not_equal': 'Delivered'}},
            ]
        }
    })

    if active_pages:
        print(f'  Active folders ({len(active_pages)}):')
        for p in active_pages:
            props  = p['properties']
            folder = prop_title(props, 'Video')
            client = prop_text(props, 'Creator')
            status = prop_select(props, 'Status')
            notes  = prop_text(props, 'Notes')
            videos = video_count_from_notes(notes)
            print(f'    {client} / {folder} — {videos} videos — {status}')
    else:
        print('  Active folders: none')

    # Last 5 deliveries
    history_pages = notion_query(token, DELIVERY_HISTORY_DB, {
        'filter':    {'property': 'Editor', 'rich_text': {'contains': matched_name}},
        'sorts':     [{'property': 'Delivered Date', 'direction': 'descending'}],
        'page_size': 5,
    })

    if history_pages:
        print('  Recent deliveries:')
        for p in history_pages:
            props  = p['properties']
            folder = prop_title(props, 'Folder')
            client = prop_text(props, 'Client')
            videos = prop_number(props, 'Videos Completed')
            d      = prop_date(props, 'Delivered Date')
            print(f'    {client} / {folder} — {videos} videos — {d}')


# ── --client ──────────────────────────────────────────────────────────────────

def cmd_client(token, name):
    pages = notion_query(token, ACTIVE_QUEUE_DB, {
        'filter': {'property': 'Creator', 'rich_text': {'contains': name}},
        'sorts':  [{'property': 'Submitted', 'direction': 'ascending'}],
    })

    if not pages:
        print(f'No active folders found for client: {name}')
        return

    by_status = {}
    for p in pages:
        props   = p['properties']
        folder  = prop_title(props, 'Video')
        client  = prop_text(props, 'Creator')
        status  = prop_select(props, 'Status')
        editor  = prop_select(props, 'Editor')
        notes   = prop_text(props, 'Notes')
        videos  = video_count_from_notes(notes)
        by_status.setdefault(status, []).append((client, folder, editor, videos))

    print(f'Folders for client matching "{name}":')
    for status in ('Raw', 'In Progress', 'Delivered'):
        rows = by_status.get(status, [])
        if not rows:
            continue
        print(f'  [{status}]')
        for client, folder, editor, videos in rows:
            editor_str = f' — {editor}' if editor else ''
            print(f'    {client} / {folder} — {videos} videos{editor_str}')


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Query Notion stats for CC Video Manager')
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--today',  action='store_true', help='Videos completed today')
    group.add_argument('--load',   action='store_true', help='Current editor load')
    group.add_argument('--pending', action='store_true', help='Queries Notion Active Queue for Status=Raw rows only. Does NOT scan Drive.')
    group.add_argument('--editor', metavar='NAME',      help='Stats for a specific editor')
    group.add_argument('--client', metavar='NAME',      help='Active folders for a client')
    args = parser.parse_args()

    config = load_config()
    token  = config['notion_token']

    if args.today:
        cmd_today(token)
    elif args.load:
        cmd_load(token)
    elif args.pending:
        cmd_pending(token)
    elif args.editor:
        cmd_editor(token, args.editor)
    elif args.client:
        cmd_client(token, args.client)


if __name__ == '__main__':
    main()

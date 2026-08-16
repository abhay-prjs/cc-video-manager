"""One-off cleanup: close out ops-assign Discord messages whose folder was
already assigned/delivered through some other path (Notion directly, etc.),
so they don't sit in the #assignments channel accepting clicks that would
re-assign an already-handled folder.

Found 2026-07-31: pending_ops_assigns.json had 75 open dropdown messages but
only 43 folders were still Status=Raw in Notion. The bot re-registers every
entry's dropdown by custom_id on every restart regardless of current Notion
status, so all 75 were still clickable — the 35 stale ones were a live
double-assign risk, not just visual clutter.

For each stale entry: PATCH the Discord message to a neutral embed (no
dropdown) noting the folder's real current state, then drop it from
pending_ops_assigns.json. Rate limited to one edit per
DISCORD_EDIT_DELAY_SECONDS (Discord rate-limits edits on messages >1h old —
see CLAUDE.md bulk-assign gotcha); requires the DiscordBot User-Agent header
or Cloudflare 403s the request before it reaches Discord.

Usage:
    python3 test/cleanup_stale_ops_assigns.py          # dry run (default)
    python3 test/cleanup_stale_ops_assigns.py --live   # actually edit + clean up
"""
import argparse
import json
import os
import re
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_OPS_ASSIGNS_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'

DISCORD_EDIT_DELAY_SECONDS = 12

STATUS_COLOR = {
    'In Progress': 0x3498db,
    'Delivered':   0x2ecc71,
    'Revision':    0xe67e22,
}
STATUS_LABEL = {
    'In Progress': '⏳ Already assigned elsewhere',
    'Delivered':   '✅ Already delivered',
    'Revision':    '🔁 Already in revision',
}


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def fetch_active_queue_status_map(token):
    """{folder_id: (status, editor_name)} for every Active Queue row."""
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    result = {}
    cursor = None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=body, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get('results', []):
            props = page['properties']
            link = props.get('Drive Link', {}).get('url') or ''
            m = re.search(r'/folders/([a-zA-Z0-9_-]+)', link)
            if not m:
                continue
            fid = m.group(1)
            status = (props.get('Status', {}).get('select') or {}).get('name', '')
            editor = (props.get('Editor', {}).get('select') or {}).get('name', '')
            result[fid] = (status, editor)
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return result


def load_pending_ops_assigns():
    if not os.path.exists(PENDING_OPS_ASSIGNS_FILE):
        return {}
    with open(PENDING_OPS_ASSIGNS_FILE) as f:
        return json.load(f)


def save_pending_ops_assigns(data):
    with open(PENDING_OPS_ASSIGNS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def close_message(bot_token, channel_id, msg_id, entry, status, editor):
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}'
    headers = {
        'Authorization': f'Bot {bot_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
    }
    label = STATUS_LABEL.get(status, f'ℹ️ No longer unassigned ({status or "not found in Notion"})')
    color = STATUS_COLOR.get(status, 0x95a5a6)
    fields = [
        {'name': 'Client', 'value': entry.get('client_name', ''), 'inline': True},
        {'name': 'Folder', 'value': entry.get('folder_name', ''), 'inline': True},
    ]
    if editor:
        fields.append({'name': 'Editor', 'value': editor, 'inline': True})
    body = {
        'embeds': [{'title': label, 'color': color, 'fields': fields}],
        'components': [],
    }
    return requests.patch(url, headers=headers, json=body, timeout=10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='actually edit + clean up (default is dry run)')
    args = ap.parse_args()
    dry_run = not args.live

    config = load_config()
    token = config.get('notion_token')
    bot_token = config.get('discord_bot_token')
    if not bot_token:
        print("ABORT: config.json has no 'discord_bot_token' key.")
        return

    status_map = fetch_active_queue_status_map(token)
    pending = load_pending_ops_assigns()

    stale = {mid: e for mid, e in pending.items()
             if status_map.get(e.get('folder_id'), ('', ''))[0] != 'Raw'}

    print(f'{len(pending)} open ops-assign message(s), {len(stale)} stale (folder no longer Raw).')
    print(f'Mode: {"LIVE" if not dry_run else "DRY RUN (no writes)"}\n')

    fixed = 0
    for mid, entry in stale.items():
        status, editor = status_map.get(entry.get('folder_id'), ('', ''))
        label = f"{entry.get('client_name', '')}/{entry.get('folder_name', '')} " \
                f"(folder_id={entry.get('folder_id', '')}) -> {status or 'not found in Notion'}"
        if dry_run:
            print(f'  DRY RUN would close: {label}')
            continue

        channel_id = entry.get('channel_id')
        if not channel_id:
            print(f'  SKIP (no channel_id in entry): {label}')
            continue

        resp = close_message(bot_token, channel_id, mid, entry, status, editor)
        if resp.status_code == 200:
            pending.pop(mid, None)
            save_pending_ops_assigns(pending)
            print(f'  CLOSED: {label}')
            fixed += 1
        elif resp.status_code == 404:
            # Message already deleted — just drop the stale entry, nothing to edit.
            pending.pop(mid, None)
            save_pending_ops_assigns(pending)
            print(f'  MESSAGE GONE (dropped entry only): {label}')
            fixed += 1
        else:
            print(f'  FAILED ({resp.status_code}): {label} :: {resp.text[:200]}')

        time.sleep(DISCORD_EDIT_DELAY_SECONDS)

    print('\n=== Summary ===')
    if dry_run:
        print(f'{len(stale)} would be closed. Re-run with --live to actually do it.')
        return
    print(f'closed: {fixed}/{len(stale)}')


if __name__ == '__main__':
    main()

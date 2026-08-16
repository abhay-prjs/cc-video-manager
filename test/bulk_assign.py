"""Bulk-assign Raw folders to editors from the CLI, going through the exact
same real pipeline a Discord /assign or ops-assign dropdown would use — not a
shortcut that bypasses it.

Why this exists: a prior bulk-assign (2026-06-25, see CLAUDE.md) wrote
straight into discord_queue.json without patching Notion's Editor select
property or cleaning up the ops-assign message, leaving rows that looked
assigned in Discord but unassigned everywhere else. This script always does,
per folder:
  1. PATCH Notion Active Queue: Editor select + Status=In Progress
     (same as _assign_raw_to_editor in discord_bot.py)
  2. Enqueue an assign item + a creator_notify item into discord_queue.json —
     picked up within ~3s by the running discord-bot service's
     process_queue_loop, which calls assign_folder() exactly as the dropdown
     does: Discord embed, deadline reset, ops-channel post, AND the dashboard
     mirror push (folder_created_at included, since assign_folder does that
     unconditionally now).
  3. If the folder has an open ops-assign message (pending_ops_assigns.json),
     edit it to a green "Assigned" embed with no dropdown and drop the entry —
     the same cleanup the docs call "required" after any bulk assign.

Requires discord-bot to actually be running (steps 2 is IPC via a JSON file
it polls) — this script does not send anything to Discord/Notion for the
Discord embed itself, only Notion PATCHes and the ops-assign edit are done
directly from here.

Usage:
    python3 test/bulk_assign.py "Betty/Higgsfield 3=Whitney" "INVO 6=Lewis"
        # dry run (default) — resolves + validates, sends nothing

    python3 test/bulk_assign.py --live "Betty/Higgsfield 3=Whitney" "INVO 6=Lewis"
        # actually does it

Each argument is FOLDER=EDITOR, where FOLDER is either just the folder name
or CLIENT/FOLDER name (use CLIENT/ prefix to disambiguate if two clients have
a folder with the same name). Matched case-insensitively against Active Queue
rows with Status=Raw. EDITOR is matched fuzzily against the active editor
roster (Editor Profiles, Active=true, Capacity>0), same matching
discord_bot.py's resolve_editor_name uses.
"""
import argparse
import json
import os
import re
import time

import requests
from filelock import FileLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
PENDING_OPS_ASSIGNS_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')
QUEUE_LOCK = FileLock(QUEUE_FILE + '.lock')
PENDING_OPS_ASSIGNS_LOCK = FileLock(PENDING_OPS_ASSIGNS_FILE + '.lock')

ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'

DISCORD_EDIT_DELAY_SECONDS = 12  # per CLAUDE.md: Discord rate-limits edits on messages >1h old


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


def extract_raw_row(page):
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


def fetch_raw_rows(token):
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    return [extract_raw_row(p) for p in pages]


def fetch_active_editors(token):
    """{name: {discord_channel_id, discord_user_id}} — Active=true, Capacity>0
    only, same filter discord_bot.py's fetch_editors_from_notion applies."""
    pages = notion_query_all(token, EDITOR_PROFILES_DB, {})
    editors = {}
    for page in pages:
        props = page['properties']
        name_rt = props.get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else ''
        capacity = props.get('Capacity', {}).get('number')
        is_active = props.get('Active', {}).get('checkbox', True)
        ch_rt = props.get('Discord Channel ID', {}).get('rich_text', [])
        channel_id = ch_rt[0].get('plain_text', '') if ch_rt else ''
        uid_rt = props.get('Discord User ID', {}).get('rich_text', [])
        user_id = uid_rt[0].get('plain_text', '') if uid_rt else ''
        if name and capacity and is_active:
            editors[name] = {'discord_channel_id': channel_id, 'discord_user_id': user_id}
    return editors


def _norm_editor_name(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').casefold())


def resolve_editor_name(raw, editors):
    """Same fuzzy match discord_bot.py's resolve_editor_name uses: exact,
    then case/whitespace-insensitive, then punctuation-squashed."""
    if not raw:
        return None
    if raw in editors:
        return raw
    target = _norm_editor_name(raw)
    hits = [k for k in editors if _norm_editor_name(k) == target]
    return hits[0] if len(hits) == 1 else None


def resolve_folder(spec, rows):
    """spec is 'FolderName' or 'Client/FolderName'. Returns (row, error_message)."""
    if '/' in spec:
        client_part, folder_part = spec.split('/', 1)
        client_part = client_part.strip().casefold()
        folder_part = folder_part.strip().casefold()
        hits = [r for r in rows
                if r['folder_name'].strip().casefold() == folder_part
                and r['client_name'].strip().casefold() == client_part]
    else:
        folder_part = spec.strip().casefold()
        hits = [r for r in rows if r['folder_name'].strip().casefold() == folder_part]

    if not hits:
        return None, f'no Raw folder matches {spec!r}'
    if len(hits) > 1:
        options = ', '.join(f"{r['client_name']}/{r['folder_name']}" for r in hits)
        return None, f'{spec!r} is ambiguous across clients — specify Client/Folder: {options}'
    return hits[0], None


def assign_raw_to_editor(token, folder_id, editor):
    """Mirrors discord_bot.py's _assign_raw_to_editor: PATCHes every Active
    Queue row for this Drive folder to Status=In Progress + Editor=editor.
    Returns the first page_id."""
    url = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Drive Link', 'url': {'contains': folder_id}}}
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    resp.raise_for_status()
    pages = resp.json().get('results', [])
    first_pid = None
    for page in pages:
        pid = page['id']
        if first_pid is None:
            first_pid = pid
        patch_resp = requests.patch(
            f'https://api.notion.com/v1/pages/{pid}',
            headers=notion_headers(token),
            json={'properties': {
                'Status': {'select': {'name': 'In Progress'}},
                'Editor': {'select': {'name': editor}},
            }},
            timeout=15,
        )
        if not patch_resp.ok:
            print(f'    WARN: Notion PATCH failed for page {pid}: '
                  f'{patch_resp.status_code} {patch_resp.text[:200]}')
    return first_pid


def enqueue_assignment(row, editor_name):
    """Drops an assign item + a creator_notify item into discord_queue.json —
    process_queue_loop (running in discord-bot) picks these up within ~3s and
    calls assign_folder() / handle_creator_notify() exactly as a real Discord
    assignment would, dashboard push included."""
    items = [
        {
            'client_name': row['client_name'],
            'folder_name': row['folder_name'],
            'video_count': row['video_count'],
            'folder_id': row['folder_id'],
            'editor_name': editor_name,
            'notion_queue_page_id': row['notion_page_id'],
            'project_number': row['project_number'],
            'is_reassign': False,
        },
        {
            'type': 'creator_notify',
            'client_name': row['client_name'],
            'folder_name': row['folder_name'],
            'editor_name': editor_name,
            'video_count': row['video_count'],
            'folder_id': row['folder_id'],
            'project_number': row['project_number'],
        },
    ]
    with QUEUE_LOCK:
        existing = []
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f:
                existing = json.load(f)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(existing + items, f, indent=2)


def find_pending_ops_assign(folder_id):
    """Returns (msg_id, entry) for folder_id's open ops-assign message, or
    (None, None) if there isn't one."""
    with PENDING_OPS_ASSIGNS_LOCK:
        if not os.path.exists(PENDING_OPS_ASSIGNS_FILE):
            return None, None
        with open(PENDING_OPS_ASSIGNS_FILE) as f:
            data = json.load(f)
    for msg_id, entry in data.items():
        if entry.get('folder_id') == folder_id:
            return msg_id, entry
    return None, None


def remove_pending_ops_assign(msg_id):
    with PENDING_OPS_ASSIGNS_LOCK:
        if not os.path.exists(PENDING_OPS_ASSIGNS_FILE):
            return
        with open(PENDING_OPS_ASSIGNS_FILE) as f:
            data = json.load(f)
        data.pop(str(msg_id), None)
        with open(PENDING_OPS_ASSIGNS_FILE, 'w') as f:
            json.dump(data, f, indent=2)


def close_ops_assign_message(bot_token, channel_id, msg_id, client_name, folder_name,
                              video_count, editor_name):
    """PATCHes the open ops-assign Discord message to a green 'Assigned' embed
    with the dropdown removed. Requires a User-Agent header — Cloudflare 403s
    raw HTTP calls to discord.com without one (see CLAUDE.md gotcha, hit
    2026-07-12)."""
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}'
    headers = {
        'Authorization': f'Bot {bot_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
    }
    body = {
        'embeds': [{
            'title': f'✅ Assigned to {editor_name}',
            'color': 0x2ecc71,
            'fields': [
                {'name': 'Client', 'value': client_name, 'inline': True},
                {'name': 'Folder', 'value': folder_name, 'inline': True},
                {'name': 'Videos', 'value': str(video_count), 'inline': True},
            ],
        }],
        'components': [],
    }
    resp = requests.patch(url, headers=headers, json=body, timeout=10)
    return resp


def parse_arg(arg):
    if '=' not in arg:
        return None, None, f'{arg!r} is not FOLDER=EDITOR'
    folder_spec, editor_spec = arg.rsplit('=', 1)
    return folder_spec.strip(), editor_spec.strip(), None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('assignments', nargs='+', help='FOLDER=EDITOR or CLIENT/FOLDER=EDITOR, space-separated')
    ap.add_argument('--live', action='store_true', help='actually assign (default is dry run)')
    args = ap.parse_args()
    dry_run = not args.live

    config = load_config()
    token = config.get('notion_token')
    bot_token = config.get('discord_bot_token')

    raw_rows = fetch_raw_rows(token)
    editors = fetch_active_editors(token)

    print(f'{len(raw_rows)} Raw folder(s) in Active Queue, {len(editors)} active editor(s).')
    print(f'Mode: {"LIVE" if not dry_run else "DRY RUN (no writes)"}\n')

    resolved = []
    for arg in args.assignments:
        folder_spec, editor_spec, err = parse_arg(arg)
        if err:
            print(f'  SKIP: {err}')
            continue
        row, err = resolve_folder(folder_spec, raw_rows)
        if err:
            print(f'  SKIP {arg!r}: {err}')
            continue
        editor_name = resolve_editor_name(editor_spec, editors)
        if not editor_name:
            print(f'  SKIP {arg!r}: {editor_spec!r} does not match any active editor '
                  f'({", ".join(sorted(editors))})')
            continue
        resolved.append((row, editor_name))
        print(f"  RESOLVED: {row['client_name']}/{row['folder_name']} "
              f"(folder_id={row['folder_id']}, videos={row['video_count']}) -> {editor_name}")

    if dry_run:
        print(f'\n{len(resolved)}/{len(args.assignments)} would be assigned. Re-run with --live to actually do it.')
        return

    print()
    assigned = 0
    for row, editor_name in resolved:
        label = f"{row['client_name']}/{row['folder_name']} -> {editor_name}"
        try:
            assign_raw_to_editor(token, row['folder_id'], editor_name)
        except Exception as e:
            print(f'  FAILED (Notion PATCH) {label}: {e}')
            continue

        enqueue_assignment(row, editor_name)
        print(f'  ASSIGNED: {label} (queued for discord-bot to pick up within ~3s)')
        assigned += 1

        msg_id, entry = find_pending_ops_assign(row['folder_id'])
        if msg_id and bot_token:
            resp = close_ops_assign_message(
                bot_token, entry['channel_id'], msg_id,
                row['client_name'], row['folder_name'], row['video_count'], editor_name,
            )
            if resp.status_code == 200:
                remove_pending_ops_assign(msg_id)
                print(f'    closed ops-assign message {msg_id}')
            else:
                print(f'    WARN: ops-assign message edit failed ({resp.status_code}): {resp.text[:200]}')
            time.sleep(DISCORD_EDIT_DELAY_SECONDS)
        elif msg_id and not bot_token:
            print(f"    WARN: found ops-assign message {msg_id} but no discord_bot_token in config — left open")

    print(f'\n=== Summary: {assigned}/{len(resolved)} assigned ===')
    if assigned:
        print('discord-bot will send the Discord embed, reset the deadline, and push to the '
              'dashboard (folder_created_at included) within a few seconds — check its logs '
              'if anything looks off.')


if __name__ == '__main__':
    main()

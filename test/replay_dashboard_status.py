"""One-off backfill: replay historical delivered/revisions status onto the
Creator Collective dashboard.

Why this exists: POST /api/discord/editing-status 404'd on the dashboard side
until 2026-07-31. discord_bot.py's _dashboard_post() treats 404 as terminal
(a data problem, not worth retrying), so it logged and discarded every status
push instead of parking it in pending_dashboard_pushes.json. Every dashboard
ticket for a folder we delivered or sent to revision is stuck at `assigned`.

Why this doesn't read deadlines.json / delivery_meta.json / editor_state_history.jsonl:
none of them carry the Drive folder_id (the dashboard's join key) for a folder
once it's been delivered:
  - deadlines.json only holds *active* folders — the entry is popped the
    moment finalize_delivery() runs, so delivered folders aren't in it.
  - delivery_meta.json is keyed by notion_page_id and only ever recorded
    assigned_at/turnaround/editor_name — no folder_id was ever written to it.
  - editor_state_history.jsonl only snapshots In Progress/Review/Revision
    folders and likewise never carried a Drive folder_id.
project_numbers.json maps folder_id -> project number but has no status/editor/
notion_page_id, so it can't anchor a status either.

What actually has everything: Notion Active Queue itself. Its rows are never
deleted on delivery (Status just flips to Delivered), and 'Drive Link' is a
URL property discord_bot.py already parses into folder_id elsewhere (see
fetch_removable_folders / _assign_raw_to_editor). So we query Active Queue
directly for Status in {Delivered, Revision} and post each folder's *current*
status once — this is authoritative, unlike stitching partial local files.

Usage:
    python3 test/replay_dashboard_status.py            # dry run (default)
    python3 test/replay_dashboard_status.py --live      # actually POST

Dry run prints exactly what would be sent, in order, with a summary. Live mode
sleeps between posts and never retries in-process — a 500/timeout gets parked
in pending_dashboard_pushes.json so the bot's own flush_dashboard_pushes()
retries it on the normal poll loop, instead of this script hammering the
endpoint on its own retry loop.
"""
import argparse
import json
import os
import re
import sys
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
PENDING_DASHBOARD_PUSHES_FILE = os.path.join(BASE_DIR, 'pending_dashboard_pushes.json')

POST_DELAY_SECONDS = 1.5

STATUS_MAP = {
    'Delivered': 'delivered',
    'Revision':  'revisions',
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
    editor_sel = props.get('Editor', {}).get('select') or {}
    editor_name = editor_sel.get('name', '')
    status_sel = props.get('Status', {}).get('select') or {}
    status = status_sel.get('name', '')
    drive_link = props.get('Drive Link', {}).get('url') or ''
    m = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
    folder_id = m.group(1) if m else ''
    video_count = props.get('Videos Completed', {}).get('number')
    if video_count is None:
        notes_rt = props.get('Notes', {}).get('rich_text', [])
        notes = notes_rt[0].get('plain_text', '') if notes_rt else ''
        vm = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(vm.group(1)) if vm else None
    delivered_date = (props.get('Delivered', {}).get('date') or {}).get('start', '')
    edited_folder = props.get('Edited Folder Name', {}).get('rich_text', [])
    edited_folder_name = edited_folder[0].get('plain_text', '') if edited_folder else ''

    # No per-row "entered Revision" timestamp exists in this schema — Notion's
    # own last_edited_time is the best available proxy for Revision rows (and
    # doubles as the cutoff/sort field for everything: delivery date when we
    # have one, last edit otherwise).
    last_edited = page.get('last_edited_time', '')
    filter_date = delivered_date or (last_edited[:10] if last_edited else '')

    return {
        'notion_page_id': page['id'],
        'folder_id': folder_id,
        'folder_name': folder_name,
        'client_name': client_name,
        'editor_name': editor_name,
        'status': STATUS_MAP.get(status, ''),
        'video_count': video_count,
        'edited_folder_name': edited_folder_name,
        'filter_date': filter_date,
    }


def fetch_target_rows(token, since=None):
    """Returns (rows_in_scope, skipped_count). `since` is a 'YYYY-MM-DD' string —
    rows with filter_date < since are dropped before ever reaching the network,
    since they predate the ticket join key existing on the site at all."""
    body = {
        'filter': {'or': [
            {'property': 'Status', 'select': {'equals': 'Delivered'}},
            {'property': 'Status', 'select': {'equals': 'Revision'}},
        ]}
    }
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)
    rows = [extract_row(p) for p in pages]
    skipped_old = 0
    if since:
        in_scope = []
        for r in rows:
            if r['filter_date'] and r['filter_date'] < since:
                skipped_old += 1
            else:
                in_scope.append(r)
        rows = in_scope
    return sorted(rows, key=lambda r: r['filter_date'] or ''), skipped_old


def queue_dashboard_push(kind, payload):
    """Mirrors discord_bot.py's _queue_dashboard_push so a parked entry here
    is picked up by the bot's normal flush_dashboard_pushes() poll loop."""
    items = []
    if os.path.exists(PENDING_DASHBOARD_PUSHES_FILE):
        try:
            with open(PENDING_DASHBOARD_PUSHES_FILE) as f:
                items = json.load(f)
        except Exception:
            items = []
    items = [
        i for i in items
        if not (i.get('kind') == kind and i.get('payload', {}).get('folder_id') == payload.get('folder_id'))
    ]
    items.append({'kind': kind, 'payload': payload})
    with open(PENDING_DASHBOARD_PUSHES_FILE, 'w') as f:
        json.dump(items[-50:], f, indent=2)


def post_status(url, secret, row):
    payload = {
        'folder_id': row['folder_id'],
        'status': row['status'],
        'editor_name': row['editor_name'] or '',
        'note': 'backfilled from Active Queue (dashboard status endpoint was 404 before 2026-07-31)',
    }
    if row['video_count'] is not None:
        payload['video_count'] = row['video_count']
    if row['edited_folder_name']:
        payload['note'] += f" | edited folder: {row['edited_folder_name']}"

    resp = requests.post(
        url,
        headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=10,
    )
    return resp, payload


DEFAULT_SINCE = '2026-07-29'  # assign-mirror bridge actually started flowing this day
                               # (2026-07-25 was when external_ref shipped, not when tickets
                               # started being created — confirmed on-site 2026-07-31: all 34
                               # mirrored tickets were created 2026-07-29T20:41Z or later)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='actually POST (default is dry run)')
    ap.add_argument('--since', default=DEFAULT_SINCE,
                     help=f"YYYY-MM-DD; rows delivered/last-edited before this are skipped "
                          f"without a network call (default {DEFAULT_SINCE}, the day the "
                          f"assign-mirror bridge actually started creating tickets — nothing "
                          f"older can have a ticket to update). Pass '' to disable the cutoff.")
    ap.add_argument('--limit', type=int, default=None,
                     help='Only process the first N rows, applied after --since filtering '
                          'and oldest-first sorting.')
    ap.add_argument('--folder-id', action='append', default=None,
                     help='Restrict to this folder_id (repeatable). For hand-picking a canary '
                          'batch. Combined with --limit if both are given (folder-id filter '
                          'applies first, then the limit).')
    args = ap.parse_args()
    dry_run = not args.live
    since = args.since or None

    config = load_config()
    token = config.get('notion_token')
    url = config.get('dashboard_status_url')
    secret = config.get('dashboard_secret')

    if not url:
        print("ABORT: config.json has no 'dashboard_status_url' key — nothing to post to.")
        sys.exit(1)
    if not secret:
        print("ABORT: config.json has no 'dashboard_secret' key.")
        sys.exit(1)

    rows, skipped_old = fetch_target_rows(token, since=since)
    skip_no_folder_id = [r for r in rows if not r['folder_id']]
    rows = [r for r in rows if r['folder_id']]

    delivered_n = sum(1 for r in rows if r['status'] == 'delivered')
    revisions_n = sum(1 for r in rows if r['status'] == 'revisions')
    print(f"Cutoff: --since {since or '(none)'}")
    print(f"in scope: {len(rows)}   delivered: {delivered_n}   revisions: {revisions_n}   "
          f"skipped(cutoff): {skipped_old}   skipped(no folder_id): {len(skip_no_folder_id)}")
    assert delivered_n + revisions_n == len(rows), 'status counts do not sum to in-scope total'

    if args.folder_id:
        wanted = set(args.folder_id)
        rows = [r for r in rows if r['folder_id'] in wanted]
        found = {r['folder_id'] for r in rows}
        missing = wanted - found
        if missing:
            print(f'WARNING: --folder-id not found in scope: {sorted(missing)}')
    if args.limit is not None:
        rows = rows[:args.limit]

    print(f'Selected for this run: {len(rows)} folder(s) (oldest first).')
    print(f'Mode: {"LIVE POST" if not dry_run else "DRY RUN (no requests sent)"}\n')

    counts = {'applied': 0, 'skipped_noop': 0, 'not_found_404': 0, 'bad_request_400': 0, 'parked_retry': 0}

    for r in rows:
        label = f"{r['client_name']} / {r['folder_name']} [{r['editor_name'] or 'unknown'}] -> {r['status']}"
        if dry_run:
            print(f"  DRY RUN would POST: {label} (folder_id={r['folder_id']}, videos={r['video_count']})")
            continue

        resp, payload = post_status(url, secret, r)
        if resp.status_code == 200:
            body = resp.json() if resp.content else {}
            skipped = body.get('skipped')
            if skipped:
                print(f'  SKIPPED ({skipped}): {label}')
                counts['skipped_noop'] += 1
            else:
                print(f'  APPLIED: {label} -> ticket {body.get("ticket_id")}')
                counts['applied'] += 1
        elif resp.status_code == 404:
            print(f'  404 (never mirrored, skipping): {label}')
            counts['not_found_404'] += 1
        elif resp.status_code == 400:
            print(f'  400 (bad payload — bug, skipping): {label} :: {resp.text[:200]}')
            counts['bad_request_400'] += 1
        else:
            print(f'  {resp.status_code} (retryable, parking): {label} :: {resp.text[:200]}')
            queue_dashboard_push('status', payload)
            counts['parked_retry'] += 1

        time.sleep(POST_DELAY_SECONDS)

    print('\n=== Summary ===')
    if dry_run:
        print(f'{len(rows)} would be posted. Re-run with --live to actually send.')
    else:
        for k, v in counts.items():
            print(f'{k}: {v}')


if __name__ == '__main__':
    main()

"""One-off catch-up: mirror every currently-unassigned (Active Queue Status=Raw)
folder into the Creator Collective dashboard, unassigned, so Vex can see and
assign them there without touching Discord first.

Why this exists: detection pushes only started firing going forward (see the
handle_ops_assign_request hook in discord_bot.py) — the folders already sitting
in Status=Raw before that landed were never mirrored. This is a one-time
backfill for those.

Why Active Queue Status=Raw and not pending_folders.json: pending_folders.json
only gets written after a *successful* Telegram send, and Telegram has been
unreachable since 2026-06-18 — that file is stale history (933 old entries),
not a live unassigned list. Active Queue Status=Raw is the actual source of
truth (confirmed: 35 rows, matching the expected count).

POST /api/discord/editing-assign with editor_name/editor_discord_id both
omitted creates an unassigned ticket (status `submitted`). Expect real 422s
here: creator_name is matched against dashboard profiles by normalized full
name, and these 35 folders' creators may not all have a profile yet. Those are
real data (creators missing from the dashboard), not script noise — printed
as their own list.

Also expect 200 {"skipped":"no_editor_in_payload"} on any re-run — that's a
no-op on an existing ticket, not an error, so running this script twice is
safe.

Usage:
    python3 test/backfill_dashboard_unassigned.py          # dry run (default)
    python3 test/backfill_dashboard_unassigned.py --live   # actually POST
"""
import argparse
import json
import os
import re
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
CREATOR_ASSIGNMENTS_DB = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'
PENDING_DASHBOARD_PUSHES_FILE = os.path.join(BASE_DIR, 'pending_dashboard_pushes.json')

POST_DELAY_SECONDS = 1.5


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
    return [extract_row(p) for p in pages]


def fetch_creator_discord_map(token):
    """{normalized client_name: (channel_id, user_id)} from Creator Assignments —
    same DB/matching logic as discord_bot.py's fetch_creator_discord_info, just
    fetched once for all rows instead of once per row."""
    url = f'https://api.notion.com/v1/databases/{CREATOR_ASSIGNMENTS_DB}/query'
    result = {}
    cursor = None
    while True:
        body = {'page_size': 100}
        if cursor:
            body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for page in data.get('results', []):
            props = page['properties']
            name_rt = props.get('Creator/Folder', {}).get('title', [])
            name = name_rt[0].get('plain_text', '') if name_rt else ''
            ch_rt = props.get('Discord Channel ID', {}).get('rich_text', [])
            uid_rt = props.get('Discord User ID', {}).get('rich_text', [])
            ch_id = ch_rt[0].get('plain_text', '') if ch_rt else ''
            u_id = uid_rt[0].get('plain_text', '') if uid_rt else ''
            if name:
                result[name.strip().lower()] = (ch_id, u_id)
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return result


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


def post_assignment(url, secret, row, creator_map):
    payload = {
        'folder_id': row['folder_id'],
        'creator_name': row['client_name'],
        'folder_name': row['folder_name'],
        'video_count': row['video_count'],
        'raw_footage_link': f"https://drive.google.com/drive/folders/{row['folder_id']}",
        'project_number': row['project_number'],
    }
    ch_id, u_id = creator_map.get(row['client_name'].strip().lower(), ('', ''))
    if ch_id:
        payload['creator_channel_id'] = ch_id
    if u_id:
        payload['creator_discord_id'] = u_id
    resp = requests.post(
        url,
        headers={'Authorization': f'Bearer {secret}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=10,
    )
    return resp, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='actually POST (default is dry run)')
    args = ap.parse_args()
    dry_run = not args.live

    config = load_config()
    token = config.get('notion_token')
    url = config.get('dashboard_url')
    secret = config.get('dashboard_secret')

    if not url:
        print("ABORT: config.json has no 'dashboard_url' key — nothing to post to.")
        return
    if not secret:
        print("ABORT: config.json has no 'dashboard_secret' key.")
        return

    rows = fetch_raw_rows(token)
    skip_no_fid = [r for r in rows if not r['folder_id']]
    rows = [r for r in rows if r['folder_id']]
    creator_map = fetch_creator_discord_map(token)

    print(f'Active Queue Status=Raw: {len(rows)} folder(s) with a resolvable folder_id, '
          f'{len(skip_no_fid)} skipped for no folder_id.')
    print(f'Mode: {"LIVE POST" if not dry_run else "DRY RUN (no requests sent)"}\n')

    successes = []
    student_not_found = []
    other_failures = []

    for r in rows:
        label = f"{r['client_name']} / {r['folder_name']} (folder_id={r['folder_id']}, videos={r['video_count']})"
        ch_id, u_id = creator_map.get(r['client_name'].strip().lower(), ('', ''))
        ids_note = f"channel_id={ch_id or 'none'}" + (f', user_id={u_id}' if u_id else '')
        if dry_run:
            print(f"  DRY RUN would POST: {label}  creator_name={r['client_name']!r}  {ids_note}")
            continue

        resp, payload = post_assignment(url, secret, r, creator_map)
        if resp.status_code == 200:
            body = resp.json() if resp.content else {}
            skipped = body.get('skipped')
            if skipped:
                print(f'  SKIPPED ({skipped}, already has a ticket — treated as success): {label}')
            else:
                print(f'  APPLIED: {label} -> ticket {body.get("ticket_id")}')
            successes.append(r)
        elif resp.status_code == 422:
            body = resp.json() if resp.content else {}
            had_ids = body.get('had_ids')
            print(f'  422 student_not_found (had_ids={had_ids}): {label} :: {resp.text[:200]}')
            student_not_found.append({**r, 'had_ids': had_ids})
        else:
            print(f'  {resp.status_code} (retryable, parking): {label} :: {resp.text[:200]}')
            queue_dashboard_push('assign', payload)
            other_failures.append(r)

        time.sleep(POST_DELAY_SECONDS)

    print('\n=== Summary ===')
    if dry_run:
        print(f'{len(rows)} would be posted. Re-run with --live to actually send.')
        return

    still_fixable = [r for r in student_not_found if r.get('had_ids') is False]
    genuinely_missing = [r for r in student_not_found if r.get('had_ids') is True]
    unknown_had_ids = [r for r in student_not_found if r.get('had_ids') not in (True, False)]

    print(f'successes (applied or already-existing): {len(successes)}')
    print(f'422 student_not_found: {len(student_not_found)} '
          f'(had_ids=false/still fixable: {len(still_fixable)}, '
          f'had_ids=true/genuinely missing: {len(genuinely_missing)}, '
          f'had_ids missing from body: {len(unknown_had_ids)})')
    print(f'other/parked: {len(other_failures)}')

    if still_fixable:
        print('\n=== had_ids=false — we had no creator_channel_id/creator_discord_id to send '
              '(Creator Assignments DB is missing Discord IDs for these; add them and re-run) ===')
        for r in still_fixable:
            print(f"  {r['client_name']!r} — {r['folder_name']} (folder_id={r['folder_id']})")

    if genuinely_missing:
        print('\n=== had_ids=true — id was sent, creator genuinely has no dashboard profile (needs a human) ===')
        for r in genuinely_missing:
            print(f"  {r['client_name']!r} — {r['folder_name']} (folder_id={r['folder_id']})")

    if unknown_had_ids:
        print('\n=== 422s with no had_ids in the response body ===')
        for r in unknown_had_ids:
            print(f"  {r['client_name']!r} — {r['folder_name']} (folder_id={r['folder_id']})")


if __name__ == '__main__':
    main()

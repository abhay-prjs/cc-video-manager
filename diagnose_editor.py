"""
diagnose_editor.py
Diagnostic script for a specific editor — checks Editor Profiles, Active Queue,
Delivery History, discord-bot logs, and cross-validates totals.

Usage: python3 diagnose_editor.py --editor Naomi
"""

import argparse
import json
import os
import re
import subprocess
import requests

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
BOT_FILE    = os.path.join(BASE_DIR, 'discord_bot.py')

ACTIVE_QUEUE_DB      = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB   = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB  = '733883073ccf48f2a83953ba2d5ad36d'

DIVIDER = '-' * 70


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def notion_query(token, db_id, body=None):
    resp = requests.post(
        f'https://api.notion.com/v1/databases/{db_id}/query',
        headers=notion_headers(token),
        json=body or {},
        timeout=20,
    )
    if not resp.ok:
        # Retry without sorts if sort property caused the failure
        body_no_sort = {k: v for k, v in (body or {}).items() if k != 'sorts'}
        if 'sorts' in (body or {}) and body_no_sort != body:
            print(f'  [WARN] Sort failed ({resp.status_code}), retrying without sort...')
            resp2 = requests.post(
                f'https://api.notion.com/v1/databases/{db_id}/query',
                headers=notion_headers(token),
                json=body_no_sort,
                timeout=20,
            )
            if resp2.ok:
                return resp2.json().get('results', [])
        print(f'  [ERROR] Notion query failed ({resp.status_code}): {resp.text[:200]}')
        return []
    return resp.json().get('results', [])


def rt(props, key):
    """Extract plain text from rich_text property."""
    items = props.get(key, {}).get('rich_text', [])
    return items[0].get('plain_text', '') if items else ''


def title(props, key):
    items = props.get(key, {}).get('title', [])
    return items[0].get('plain_text', '') if items else ''


def sel(props, key):
    s = props.get(key, {}).get('select') or {}
    return s.get('name', '')


def num(props, key):
    return props.get(key, {}).get('number') or 0


def date_start(props, key):
    d = props.get(key, {}).get('date') or {}
    return d.get('start', '')


# ── A. Editor Profiles ────────────────────────────────────────────────────────

def check_editor_profiles(token, editor_name):
    print(f'\n{"=" * 70}')
    print(f'A. EDITOR PROFILES — {editor_name}')
    print(DIVIDER)

    pages = notion_query(token, EDITOR_PROFILES_DB)
    found = None
    for page in pages:
        props = page['properties']
        name = title(props, 'Editor')
        if name.lower() == editor_name.lower():
            found = props
            break

    if not found:
        print(f'  ⚠️  Editor "{editor_name}" not found in Editor Profiles DB.')
        return None

    active    = num(found, 'Active Videos')
    capacity  = num(found, 'Capacity') or 70
    week      = num(found, 'Delivered This Week')
    month     = num(found, 'Delivered This Month')
    total     = num(found, 'Total Videos Delivered')
    avg       = num(found, 'Avg Turnaround Days')

    print(f'  Active Videos:         {active}')
    print(f'  Capacity:              {capacity}')
    print(f'  Load:                  {round((active / capacity) * 100) if capacity else 0}%')
    print(f'  Delivered This Week:   {week}')
    print(f'  Delivered This Month:  {month}')
    print(f'  Total Videos Delivered:{total}')
    print(f'  Avg Turnaround Days:   {avg}')

    return {'active': active, 'week': week, 'month': month, 'total': total}


# ── B. Active Queue ───────────────────────────────────────────────────────────

def check_active_queue(token, editor_name):
    print(f'\n{"=" * 70}')
    print(f'B. ACTIVE QUEUE — rows for {editor_name}')
    print(DIVIDER)

    pages = notion_query(token, ACTIVE_QUEUE_DB, {
        'filter': {'property': 'Editor', 'select': {'equals': editor_name}},
        'sorts':  [{'property': 'Submitted', 'direction': 'descending'}],
    })

    if not pages:
        print(f'  No Active Queue rows found for {editor_name}.')
        return

    for page in pages:
        props     = page['properties']
        folder    = title(props, 'Video')
        client    = rt(props, 'Creator')
        status    = sel(props, 'Status')
        submitted = date_start(props, 'Submitted')
        delivered = date_start(props, 'Delivered')
        vid_done  = num(props, 'Videos Completed')
        notes_rt  = props.get('Notes', {}).get('rich_text', [])
        notes     = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m         = re.search(r'Videos:\s*(\d+)', notes)
        vid_count = int(m.group(1)) if m else '?'
        print(f'  Folder:     {folder}')
        print(f'  Client:     {client}')
        print(f'  Status:     {status}')
        print(f'  Submitted:  {submitted or "—"}')
        print(f'  Delivered:  {delivered or "—"}')
        print(f'  Videos:     {vid_count} (assigned) / {vid_done} (completed)')
        print()


# ── C. Delivery History ───────────────────────────────────────────────────────

def check_delivery_history(token, editor_name):
    print(f'\n{"=" * 70}')
    print(f'C. DELIVERY HISTORY — all rows for {editor_name}')
    print(DIVIDER)

    pages = notion_query(token, DELIVERY_HISTORY_DB, {
        'filter': {'property': 'Editor', 'select': {'equals': editor_name}},
        'sorts':  [{'property': 'date:Delivered Date:start', 'direction': 'descending'}],
    })

    if not pages:
        print(f'  No Delivery History rows found for {editor_name}.')
        return 0

    total_videos = 0
    for page in pages:
        props       = page['properties']
        folder      = title(props, 'Folder')
        client_text = rt(props, 'Client')
        videos      = num(props, 'Videos Completed')
        delivered   = date_start(props, 'date:Delivered Date:start')
        total_videos += videos
        print(f'  {delivered or "?":12}  {client_text:20}  {folder:30}  {videos} videos')

    print(f'\n  TOTAL from Delivery History: {total_videos} videos across {len(pages)} rows')
    return total_videos


# ── D. discord-bot logs ───────────────────────────────────────────────────────

def check_logs(editor_name):
    print(f'\n{"=" * 70}')
    print(f'D. DISCORD-BOT LOGS — completions since 2026-05-01')
    print(DIVIDER)

    cmd = (
        f'journalctl -u discord-bot --no-pager --since "2026-05-01 00:00:00" | '
        f'grep -i "{editor_name.lower()}\\|finalize" | tail -30'
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    output = result.stdout.strip()
    if output:
        for line in output.splitlines():
            print(f'  {line}')
    else:
        print(f'  (no matching log lines found)')


# ── E. Cross-check ────────────────────────────────────────────────────────────

def cross_check(profile_stats, history_total, editor_name):
    print(f'\n{"=" * 70}')
    print(f'E. CROSS-CHECK — Delivery History vs Editor Profiles')
    print(DIVIDER)

    if profile_stats is None:
        print('  Skipped — Editor Profiles not found.')
        return

    profile_total = profile_stats['total']
    print(f'  Delivery History total:  {history_total}')
    print(f'  Editor Profiles total:   {profile_total}')

    if history_total != profile_total:
        diff = history_total - profile_total
        print(f'\n  ⚠️  MISMATCH: Delivery History shows {history_total} but Editor Profiles shows {profile_total} (diff: {diff:+d})')
    else:
        print(f'\n  ✅ Totals match.')


# ── F. finalize_delivery() code section ───────────────────────────────────────

def check_finalize_code():
    print(f'\n{"=" * 70}')
    print('F. finalize_delivery() — relevant Editor Profiles update code')
    print(DIVIDER)

    try:
        with open(BOT_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f'  discord_bot.py not found at {BOT_FILE}')
        return

    in_block = False
    start_line = 0
    block_lines = []

    for i, line in enumerate(lines, 1):
        if 'async def finalize_delivery(' in line:
            in_block = True
            start_line = i

        if in_block:
            block_lines.append((i, line))
            # Stop after the editor update patch block (look for send_telegram call)
            if 'send_telegram' in line and len(block_lines) > 10:
                break

    if not block_lines:
        print('  finalize_delivery() not found in discord_bot.py')
        return

    # Print only the editor-profile update section
    printing = False
    for lineno, line in block_lines:
        if 'editor_page_id' in line and 'if' in line:
            printing = True
        if printing:
            print(f'  {lineno:4}: {line}', end='')
        if printing and '_notion_patch' in line and 'Avg Turnaround' in line:
            break



# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Diagnose editor state across Notion and logs.')
    parser.add_argument('--editor', required=True, help='Editor name (e.g. Naomi)')
    args = parser.parse_args()

    editor_name = args.editor
    config = load_config()
    token  = config['notion_token']

    print(f'\n{"#" * 70}')
    print(f'  EDITOR DIAGNOSTIC REPORT — {editor_name}')
    print(f'{"#" * 70}')

    profile_stats = check_editor_profiles(token, editor_name)
    check_active_queue(token, editor_name)
    history_total = check_delivery_history(token, editor_name)
    check_logs(editor_name)
    cross_check(profile_stats, history_total, editor_name)
    check_finalize_code()

    print(f'\n{"#" * 70}')
    print('  END OF REPORT')
    print(f'{"#" * 70}\n')


if __name__ == '__main__':
    main()

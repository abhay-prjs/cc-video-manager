"""One-off: reconcile Editor Profiles' 'Delivered This Week' cached counter
against a live Delivery History query for the current Mon-Sun (EDT) week,
setting each editor's cached counter to whichever is higher.

Why this exists: the /leaderboard fix on 2026-08-08 (fetch_all_editor_stats_for_range)
takes max(live, cached) at display time, so the leaderboard itself is already
correct. But the underlying 'Delivered This Week' field on Editor Profiles was
still left at its drifted value for editors where live > cached (e.g. Gabo:
cached 56 vs live 170 the week this was found) — anything else that reads that
field directly (not through the leaderboard's max() fallback) would still see
the stale number. This script fixes the field itself so it's consistent
everywhere, not just papered over at read time.

Never lowers a cached counter (cached > live is expected/normal — cached also
picks up website-native deliveries that never create a Delivery History row,
see handle_cc_dashboard_delivered in discord_bot.py). Only raises it when the
live query found more than the counter currently reflects.

Usage:
    python3 test/fix_weekly_counter_drift.py            # dry run (default)
    python3 test/fix_weekly_counter_drift.py --live      # actually PATCH
"""
import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'
DATE_PROP = 'date:Delivered Date:start'
EDT = timezone(timedelta(hours=-4))

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def query_all(token, db_id, body):
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results = []
    cursor = None
    while True:
        req_body = dict(body)
        req_body['page_size'] = 100
        if cursor:
            req_body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=req_body, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Actually PATCH Notion (default is dry run)')
    args = parser.parse_args()

    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token = config['notion_token']

    today = datetime.now(EDT)
    monday_str = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
    tomorrow_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')
    logger.info(f'Reconciling week {monday_str} -> {tomorrow_str} (EDT)')

    editors = {}
    for page in query_all(token, EDITOR_PROFILES_DB, {}):
        props = page['properties']
        name_rt = props.get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else ''
        capacity = props.get('Capacity', {}).get('number')
        if not name or not capacity:
            continue
        cached = props.get('Delivered This Week', {}).get('number') or 0
        editors[name] = {'page_id': page['id'], 'cached': cached, 'live': 0}

    filt = {'filter': {'and': [
        {'property': DATE_PROP, 'date': {'on_or_after': monday_str}},
        {'property': DATE_PROP, 'date': {'before': tomorrow_str}},
    ]}}
    for page in query_all(token, DELIVERY_HISTORY_DB, filt):
        props = page['properties']
        videos = props.get('Videos Completed', {}).get('number') or 0
        editor_sel = props.get('Editor', {}).get('select') or {}
        name = editor_sel.get('name', '')
        if name in editors:
            editors[name]['live'] += videos

    changes = []
    for name, d in sorted(editors.items()):
        target = max(d['cached'], d['live'])
        if target > d['cached']:
            changes.append((name, d['page_id'], d['cached'], target))

    if not changes:
        logger.info('No drift found — all cached counters already >= live query. Nothing to do.')
        return

    for name, page_id, old, new in changes:
        logger.info(f'{"[DRY RUN] " if not args.live else ""}{name}: {old} -> {new}')
        if args.live:
            resp = requests.patch(
                f'https://api.notion.com/v1/pages/{page_id}',
                headers=notion_headers(token),
                json={'properties': {'Delivered This Week': {'number': new}}},
                timeout=15,
            )
            if not resp.ok:
                logger.error(f'PATCH failed for {name}: {resp.status_code} {resp.text[:200]}')

    logger.info(f'{len(changes)} editor(s) {"updated" if args.live else "would be updated"}.')


if __name__ == '__main__':
    main()

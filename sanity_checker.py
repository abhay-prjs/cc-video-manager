"""
sanity_checker.py
Nightly consistency audit across Notion and local state. Alerts the Discord
ops channel only when something is wrong. Catches the silent-drift failures
that have bitten before:
  - an editor with active folders whose Editor Profiles row is archived/missing
    (Kaye 2026-07-02: archived profile silently ate every stats update)
  - duplicate Delivery History rows (double /complete or double approve)
  - Active Queue rows In Progress with no Editor set
  - deadlines.json entries pointing at delivered/archived Notion pages
  - Delivered This Week > Delivered This Month outside the month-boundary
    overlap window (a weekly/monthly reset misfired)
Intended to run nightly via cron.
"""

import json
import logging
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE    = os.path.join(BASE_DIR, 'config.json')
DEADLINES_FILE = os.path.join(BASE_DIR, 'deadlines.json')

ACTIVE_QUEUE_DB     = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB  = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'
DELIVERY_DATE_PROP  = 'date:Delivered Date:start'

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }


def query_all(token, db_id, body=None):
    """Paginated Notion DB query."""
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results, cursor = [], None
    while True:
        payload = dict(body or {})
        payload['page_size'] = 100
        if cursor:
            payload['start_cursor'] = cursor
        r = requests.post(url, headers=notion_headers(token), json=payload, timeout=20)
        if not r.ok:
            logger.error(f'query failed for {db_id}: {r.status_code} {r.text[:200]}')
            return results
        data = r.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            return results
        cursor = data.get('next_cursor')


def _title(props, name):
    rt = props.get(name, {}).get('title', [])
    return rt[0].get('plain_text', '') if rt else ''


def _text(props, name):
    rt = props.get(name, {}).get('rich_text', [])
    return rt[0].get('plain_text', '') if rt else ''


def _select(props, name):
    return (props.get(name, {}).get('select') or {}).get('name', '')


def main():
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token = config['notion_token']
    issues = []

    # ── Active Queue: In Progress/Revision rows ────────────────────────────────
    active_rows = query_all(token, ACTIVE_QUEUE_DB, {
        'filter': {'or': [
            {'property': 'Status', 'select': {'equals': 'In Progress'}},
            {'property': 'Status', 'select': {'equals': 'Revision'}},
        ]}
    })
    active_editors = set()
    for page in active_rows:
        props = page['properties']
        editor = _select(props, 'Editor')
        if editor:
            active_editors.add(editor)
        else:
            issues.append(f"❓ In Progress with no Editor: {_text(props, 'Creator')} / {_title(props, 'Video')}")

    # ── Editor Profiles: archived/missing profiles for active editors ─────────
    profiles = query_all(token, EDITOR_PROFILES_DB)
    profile_names = {_title(p['properties'], 'Editor') for p in profiles}
    for editor in sorted(active_editors - profile_names):
        issues.append(f"🗄️ Editor **{editor}** has active folders but their Editor Profiles row is archived or missing — stats updates are silently failing")

    # ── Delivery History: duplicate rows in the last 3 days ───────────────────
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime('%Y-%m-%d')
    recent = query_all(token, DELIVERY_HISTORY_DB, {
        'filter': {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after': since}}
    })
    keys = Counter()
    for page in recent:
        props = page['properties']
        date = (props.get(DELIVERY_DATE_PROP, {}).get('date') or {}).get('start', '')
        keys[(_title(props, 'Folder'), _text(props, 'Client'), _select(props, 'Editor'), date)] += 1
    for (folder, client, editor, date), n in keys.items():
        if n > 1:
            issues.append(f"👯 Duplicate Delivery History: {client} / {folder} ({editor}, {date}) ×{n} — stats likely double-counted")

    # ── deadlines.json drift ───────────────────────────────────────────────────
    try:
        with open(DEADLINES_FILE) as f:
            deadlines = json.load(f)
    except Exception:
        deadlines = {}
    active_page_ids = {p['id'].replace('-', '') for p in active_rows}
    raw_rows = query_all(token, ACTIVE_QUEUE_DB, {
        'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}
    })
    raw_page_ids = {p['id'].replace('-', '') for p in raw_rows}
    drift = 0
    for d in deadlines.values():
        pid = (d.get('notion_page_id') or '').replace('-', '')
        if pid and pid not in active_page_ids and pid not in raw_page_ids:
            drift += 1
    if drift:
        issues.append(f"🧹 deadlines.json has {drift} entr{'y' if drift == 1 else 'ies'} for folders no longer active in Notion")

    # ── Counter anomalies (skip the first week of the month: the weekly window
    #    can legitimately straddle the month boundary) ──────────────────────────
    if datetime.now(timezone.utc).day >= 8:
        for p in profiles:
            props = p['properties']
            name  = _title(props, 'Editor')
            week  = props.get('Delivered This Week', {}).get('number') or 0
            month = props.get('Delivered This Month', {}).get('number') or 0
            if week > month:
                issues.append(f"📉 {name}: Delivered This Week ({int(week)}) > This Month ({int(month)}) — a weekly/monthly reset likely misfired")

    if not issues:
        logger.info('all checks passed')
        return

    for i in issues:
        logger.warning(i)

    val = '\n'.join(f'• {i}' for i in issues)
    embed = {
        'title': f'🩺 Sanity Check — {len(issues)} issue(s)',
        'description': val[:4000],
        'color': 0xe74c3c,
    }
    r = requests.post(
        f"https://discord.com/api/v10/channels/{config['ops_channel_id']}/messages",
        headers={'Authorization': f"Bot {config['discord_bot_token']}", 'Content-Type': 'application/json'},
        json={'embeds': [embed]}, timeout=15)
    logger.info(f'alert sent: {r.status_code}')


if __name__ == '__main__':
    main()

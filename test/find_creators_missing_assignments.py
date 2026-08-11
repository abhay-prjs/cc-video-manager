"""
find_creators_missing_assignments.py

One-off audit: which Active Queue `Creator` names (every row originates from a
Drive folder detection under the "In-House Editor" root) have NO matching row
in the Creator Assignments DB (title property `Creator/Folder`)?

Note: Active Queue's client-name field is a rich_text property called
`Creator` (verified against the live DB schema), not `Client` — don't assume
the property name from other code that queries a different DB.

This matters for the Creator Collective Dashboard Bridge —
`fetch_creator_discord_info(client_name)` in discord_bot.py looks up Creator
Assignments by exact case-insensitive name match on `Creator/Folder` to
resolve `creator_channel_id`/`creator_discord_id`. A client with no Creator
Assignments row can't be disambiguated/mirrored to the dashboard (see
CLAUDE.md's Creator Collective Dashboard Bridge section, and the Chris/Chris
collision noted in test/backfill_dashboard_unassigned.py).

Read-only. Nothing is written to Notion or any state file.

Run:
    python3 test/find_creators_missing_assignments.py
"""

import os
import re
import sys
import json

import requests

ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
CREATOR_ASSIGNMENTS_DB = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'

# test/ scripts resolve BASE_DIR as the parent dir (see CLAUDE.md convention)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    with open(os.path.join(BASE_DIR, 'config.json')) as f:
        return json.load(f)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }


def notion_query_all(token, db_id):
    headers = notion_headers(token)
    results, cursor = [], None
    while True:
        body = {'start_cursor': cursor} if cursor else {}
        res = requests.post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            headers=headers, json=body, timeout=30,
        )
        res.raise_for_status()
        data = res.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            return results
        cursor = data.get('next_cursor')


def prop_text(props, key):
    """Pull a plain string out of a title / rich_text / select property."""
    p = props.get(key) or {}
    for kind in ('title', 'rich_text'):
        if p.get(kind):
            return p[kind][0].get('plain_text', '')
    if p.get('select'):
        return p['select'].get('name', '')
    return ''


def name_key(s):
    """Normalize for matching — collapses whitespace/casing differences
    (see CLAUDE.md gotcha on 'Edited ' trailing-space mismatches)."""
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def main():
    config = load_config()
    token = config.get('notion_token', '')
    if not token:
        sys.exit('notion_token missing from config.json')

    try:
        aq_pages = notion_query_all(token, ACTIVE_QUEUE_DB)
    except requests.exceptions.RequestException as e:
        sys.exit(f'could not reach Notion (Active Queue DB): {e}')

    try:
        ca_pages = notion_query_all(token, CREATOR_ASSIGNMENTS_DB)
    except requests.exceptions.RequestException as e:
        sys.exit(f'could not reach Notion (Creator Assignments DB): {e}')

    aq_clients = {}
    for p in aq_pages:
        name = prop_text(p['properties'], 'Creator')
        if name:
            aq_clients.setdefault(name_key(name), name)

    ca_names = {}
    for p in ca_pages:
        name = prop_text(p['properties'], 'Creator/Folder')
        if name:
            ca_names.setdefault(name_key(name), name)

    missing_keys = set(aq_clients) - set(ca_names)
    missing = sorted(aq_clients[k] for k in missing_keys)

    print(f'Active Queue distinct clients: {len(aq_clients)}')
    print(f'Creator Assignments rows:      {len(ca_names)}')
    print()
    if missing:
        print(f'Clients with NO Creator Assignments row ({len(missing)}):')
        for n in missing:
            print(f'  - {n}')
    else:
        print('None — every Active Queue client has a matching Creator Assignments row.')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
daily_summary.py
Sends a daily Notion summary to Telegram (Vex's OpenClaw chat).
Scheduled: 17:30 UTC = 11:00 PM IST
"""

import json
import os
import requests
from datetime import date

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

ACTIVE_QUEUE_DB    = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def query_notion(token, db_id, body=None):
    """Query a Notion database with automatic pagination; returns all results."""
    url     = f'https://api.notion.com/v1/databases/{db_id}/query'
    results = []
    cursor  = None
    while True:
        payload = (body.copy() if body else {})
        if cursor:
            payload['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=payload, timeout=15)
        if not resp.ok:
            print(f'Notion query error {resp.status_code}: {resp.text}')
            break
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


# ── Property helpers ──────────────────────────────────────────────────────────

def prop_text(props, key, ptype='rich_text'):
    rt = props.get(key, {}).get(ptype, [])
    return rt[0].get('plain_text', '') if rt else ''


def prop_select(props, key):
    sel = props.get(key, {}).get('select') or {}
    return sel.get('name', '')


def prop_number(props, key):
    return props.get(key, {}).get('number') or 0


# ── Section builders ──────────────────────────────────────────────────────────

def section_assigned(token, today):
    rows = query_notion(token, ACTIVE_QUEUE_DB, {'filter': {
        'and': [
            {'property': 'Submitted', 'date':   {'equals': today}},
            {'property': 'Status',    'select': {'does_not_equal': 'Raw'}},
        ]
    }})
    lines = [f'📁 <b>Assigned Today ({len(rows)})</b>']
    if rows:
        for p in rows:
            pr     = p['properties']
            folder = prop_text(pr, 'Video', 'title')
            client = prop_text(pr, 'Creator')
            editor = prop_select(pr, 'Editor')
            lines.append(f'  • {client} / {folder} → {editor or "—"}')
    else:
        lines.append('  None')
    return lines


def section_unassigned(token, today):
    rows = query_notion(token, ACTIVE_QUEUE_DB, {'filter': {
        'and': [
            {'property': 'Submitted', 'date':   {'equals': today}},
            {'property': 'Status',    'select': {'equals': 'Raw'}},
        ]
    }})
    lines = [f'⚠️ <b>Unassigned Today ({len(rows)})</b>']
    if rows:
        for p in rows:
            pr     = p['properties']
            folder = prop_text(pr, 'Video', 'title')
            client = prop_text(pr, 'Creator')
            lines.append(f'  • {client} / {folder}')
    else:
        lines.append('  None ✅')
    return lines


def section_completed(token, today):
    rows = query_notion(token, ACTIVE_QUEUE_DB, {'filter': {
        'property': 'Delivered',
        'date': {'equals': today},
    }})
    total_videos = sum(prop_number(p['properties'], 'Videos Completed') for p in rows)
    lines = [f'✅ <b>Completed Today ({len(rows)} folders · {total_videos} videos)</b>']
    if rows:
        for p in rows:
            pr     = p['properties']
            folder = prop_text(pr, 'Video', 'title')
            client = prop_text(pr, 'Creator')
            editor = prop_select(pr, 'Editor')
            count  = prop_number(pr, 'Videos Completed')
            lines.append(f'  • {editor} — {count} videos | {client} / {folder}')
    else:
        lines.append('  None')
    return lines


def section_editor_load(token):
    rows  = query_notion(token, EDITOR_PROFILES_DB)
    lines = ['📊 <b>Editor Load</b>']
    for p in rows:
        pr       = p['properties']
        name     = prop_text(pr, 'Editor', 'title')
        active   = prop_number(pr, 'Active Videos')
        capacity = prop_number(pr, 'Capacity') or 70
        pct      = active / capacity if capacity else 0
        dot      = '🔴' if pct >= 0.85 else '🟡' if pct >= 0.6 else '🟢'
        bar      = '█' * int(pct * 10) + '░' * (10 - int(pct * 10))
        lines.append(f'  {dot} <b>{name}</b>: {active}/{capacity} [{bar}]')
    return lines


def section_flagged(token):
    rows  = query_notion(token, ACTIVE_QUEUE_DB, {'filter': {
        'property': 'Status',
        'select':   {'equals': 'Review'},
    }})
    lines = [f'🔍 <b>Flagged for Review ({len(rows)})</b>']
    if rows:
        for p in rows:
            pr     = p['properties']
            folder = prop_text(pr, 'Video', 'title')
            client = prop_text(pr, 'Creator')
            editor = prop_select(pr, 'Editor')
            lines.append(f'  • {client} / {folder} — {editor or "unassigned"}')
    else:
        lines.append('  None ✅')
    return lines


# ── Main ──────────────────────────────────────────────────────────────────────

def build_summary(token, today):
    heading = f'📊 <b>CC Video Manager Daily Summary</b> — {date.today().strftime("%B %d, %Y")}\n'
    sections = [
        section_assigned(token, today),
        section_unassigned(token, today),
        section_completed(token, today),
        section_editor_load(token),
        section_flagged(token),
    ]
    return heading + '\n\n'.join('\n'.join(s) for s in sections)


def _html_to_discord(text):
    """Convert basic HTML tags to Discord markdown."""
    import re
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text


def send_discord(token, channel_id, text):
    url  = f'https://discord.com/api/v10/channels/{channel_id}/messages'
    resp = requests.post(
        url,
        headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json'},
        json={'content': _html_to_discord(text)},
        timeout=10,
    )
    if not resp.ok:
        print(f'Discord error {resp.status_code}: {resp.text}')
    return resp.ok


def main():
    config    = load_config()
    today     = date.today().isoformat()
    print(f'Building daily summary for {today}...')

    summary = build_summary(config['notion_token'], today)
    print(summary)
    print()

    ok = send_discord(config['discord_bot_token'], config['ops_channel_id'], summary)
    print('Sent.' if ok else 'Failed to send.')


if __name__ == '__main__':
    main()

"""
daily_digest.py
Posts a morning "needs your attention" digest to the Discord ops channel:
  - completion reviews pending more than 24h
  - assigned folders past their deadline
  - unassigned (Status=Raw) folders
Intended to run once a day via cron (09:00 IST = 03:30 UTC).
Sends nothing for sections that are empty; skips the message entirely
if there is nothing to report.
"""

import json
import logging
import os
import time
import requests
from datetime import datetime, timezone

BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE           = os.path.join(BASE_DIR, 'config.json')
PENDING_REVIEWS_FILE  = os.path.join(BASE_DIR, 'pending_reviews.json')
DEADLINES_FILE        = os.path.join(BASE_DIR, 'deadlines.json')
IGNORED_FOLDERS_FILE  = os.path.join(BASE_DIR, 'ignored_folders.json')

ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }


def stale_reviews():
    """Pending reviews older than 24h, oldest first."""
    now = datetime.now(timezone.utc)
    rows = []
    for rd in load_json(PENDING_REVIEWS_FILE, {}).values():
        if rd.get('status') != 'pending':
            continue
        try:
            created = datetime.fromisoformat(str(rd.get('created_at')))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        age_h = (now - created).total_seconds() / 3600
        if age_h >= 24:
            rows.append((age_h, rd))
    rows.sort(reverse=True)
    return rows


def overdue_folders(token):
    """Deadline entries past due whose Notion status is still not Delivered."""
    now = time.time()
    rows = []
    for d in load_json(DEADLINES_FILE, {}).values():
        due = d.get('due_ts')
        if d.get('indefinite') or not due or now <= due:
            continue
        pid = d.get('notion_page_id')
        if pid:
            try:
                r = requests.get(f'https://api.notion.com/v1/pages/{pid}',
                                 headers=notion_headers(token), timeout=15)
                status = (r.json().get('properties', {}).get('Status', {}).get('select') or {}).get('name', '')
                if status == 'Delivered':
                    continue
            except Exception as e:
                logger.warning(f'overdue status check failed for {pid}: {e}')
        rows.append(((now - due) / 3600, d))
    rows.sort(reverse=True)
    return rows


def unassigned_folders(token):
    """Active Queue rows with Status=Raw, skipping ignored folder IDs."""
    ignored = set(load_json(IGNORED_FOLDERS_FILE, []))
    r = requests.post(f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query',
                      headers=notion_headers(token),
                      json={'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}},
                      timeout=15)
    rows = []
    for page in r.json().get('results', []):
        props = page['properties']
        title_rt = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
        creator_rt = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
        drive_link = props.get('Drive Link', {}).get('url') or ''
        import re as _re
        m = _re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        if m and m.group(1) in ignored:
            continue
        rows.append((client_name, folder_name))
    return rows


def main():
    config = load_json(CONFIG_FILE, {})
    token = config.get('notion_token')
    channel_id = config.get('ops_channel_id')
    bot_token = config.get('discord_bot_token')
    if not (token and channel_id and bot_token):
        logger.error('missing notion_token / ops_channel_id / discord_bot_token in config')
        return

    reviews = stale_reviews()
    overdue = overdue_folders(token)
    raw = unassigned_folders(token)

    if not (reviews or overdue or raw):
        logger.info('nothing to report — skipping digest')
        return

    fields = []
    if reviews:
        lines = [f"• {rd['client_name']} / {rd['folder_name']} — {rd['editor_name']} · {int(h // 24)}d {int(h % 24)}h old"
                 for h, rd in reviews]
        val = '\n'.join(lines)
        fields.append({'name': f'⚠️ Reviews pending >24h ({len(reviews)}) — use /reviews',
                       'value': val[:1020] + ('…' if len(val) > 1020 else ''), 'inline': False})
    if overdue:
        lines = [f"• {d.get('client_name')} / {d.get('folder_name')} — {d.get('editor_name') or 'unassigned'} · {int(h)}h overdue"
                 for h, d in overdue]
        val = '\n'.join(lines)
        fields.append({'name': f'🚨 Overdue folders ({len(overdue)})',
                       'value': val[:1020] + ('…' if len(val) > 1020 else ''), 'inline': False})
    if raw:
        lines = [f'• {c} / {f}' for c, f in raw]
        val = '\n'.join(lines)
        fields.append({'name': f'⏳ Unassigned folders ({len(raw)})',
                       'value': val[:1020] + ('…' if len(val) > 1020 else ''), 'inline': False})

    embed = {
        'title': '☀️ Morning Ops Digest',
        'description': datetime.now(timezone.utc).strftime('%A, %b %d'),
        'color': 0xf1c40f,
        'fields': fields,
    }
    r = requests.post(f'https://discord.com/api/v10/channels/{channel_id}/messages',
                      headers={'Authorization': f'Bot {bot_token}', 'Content-Type': 'application/json'},
                      json={'embeds': [embed]}, timeout=15)
    logger.info(f'digest sent: {r.status_code} — {len(reviews)} reviews, {len(overdue)} overdue, {len(raw)} unassigned')


if __name__ == '__main__':
    main()

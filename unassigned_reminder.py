"""
unassigned_reminder.py
Checks Notion Active Queue for folders with Status == "Raw"
submitted 5+ hours ago, then sends a Telegram reminder to config['chat_id'].
Folders with a stored Telegram message_id in pending_folders.json get a
[Check Original 🔗] inline button linking directly to that message.
Intended to run every hour via cron.
"""

import json
import logging
import os
import re
import requests
from datetime import datetime, timezone

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE          = os.path.join(BASE_DIR, 'config.json')
PENDING_FOLDERS_FILE = os.path.join(BASE_DIR, 'pending_folders.json')

ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
THRESHOLD_HOURS = 5

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_pending_folders():
    if os.path.exists(PENDING_FOLDERS_FILE):
        with open(PENDING_FOLDERS_FILE) as f:
            return json.load(f)
    return {}


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def fetch_stale_folders(token):
    """
    Returns list of dicts for Active Queue rows where Status is Raw
    AND Submitted is more than THRESHOLD_HOURS ago.
    Each dict: client_name, folder_name, video_count, status, editor_name,
               folder_id, hours_ago, submitted_dt.
    """
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    if not resp.ok:
        logger.error(f'Notion query failed: {resp.status_code} {resp.text}')
        return []

    rows    = []
    now_utc = datetime.now(timezone.utc)

    for page in resp.json().get('results', []):
        props = page['properties']

        title_rt    = props.get('Video', {}).get('title', [])
        folder_name = title_rt[0].get('plain_text', '') if title_rt else ''

        creator_rt  = props.get('Creator', {}).get('rich_text', [])
        client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''

        status_sel  = props.get('Status', {}).get('select') or {}
        status      = status_sel.get('name', '')

        editor_sel  = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '')

        notes_rt    = props.get('Notes', {}).get('rich_text', [])
        notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m           = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0

        # Extract folder_id from Drive Link for pending_folders lookup
        drive_link = props.get('Drive Link', {}).get('url') or ''
        m2         = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        folder_id  = m2.group(1) if m2 else ''

        submitted_prop = (props.get('Submitted', {}).get('date') or {}).get('start', '')
        if not submitted_prop:
            continue

        try:
            if 'T' in submitted_prop:
                dt = datetime.fromisoformat(submitted_prop)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(submitted_prop).replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(f'Could not parse Submitted date: {submitted_prop!r}')
            continue

        hours_ago = (now_utc - dt).total_seconds() / 3600
        if hours_ago >= THRESHOLD_HOURS:
            rows.append({
                'client_name': client_name,
                'folder_name': folder_name,
                'video_count': video_count,
                'status':      status,
                'editor_name': editor_name,
                'folder_id':   folder_id,
                'hours_ago':   hours_ago,
                'submitted_dt': dt,
            })

    return rows


def format_time_ago(hours_ago):
    if hours_ago < 24:
        h = int(hours_ago)
        return f'{h}h ago'
    days = int(hours_ago // 24)
    h    = int(hours_ago % 24)
    return f'{days}d {h}h ago' if h else f'{days}d ago'


def tme_chat_id(chat_id_str):
    """Convert a Telegram supergroup chat_id to the numeric ID used in t.me/c/ links."""
    s = str(chat_id_str)
    if s.startswith('-100'):
        return s[4:]
    return s.lstrip('-')


def main():
    config   = load_config()
    token    = config['notion_token']
    tg_token = config['notion_bridge_token']
    chat_id  = config['notion_bridge_chat_id']

    # Both reminder and original assignment messages are in the same chat
    bridge_tme_id = tme_chat_id(chat_id)

    folders = fetch_stale_folders(token)
    if not folders:
        logger.info('No stale Raw folders (5+ hours) — nothing to send.')
        return

    folders.sort(key=lambda r: r['submitted_dt'])

    pending_folders = load_pending_folders()

    lines   = []
    buttons = []  # inline keyboard rows for folders that have a stored message_id

    for r in folders:
        time_str = format_time_ago(r['hours_ago'])
        base     = f"• {r['client_name']} / {r['folder_name']} — {r['video_count']} videos — submitted {time_str}"

        if r['status'] == 'Raw' and not r['editor_name']:
            line = base + ' ⚠️ Not assigned'
        else:
            editor_hint = f' assigned to {r["editor_name"]}' if r['editor_name'] else ' — no editor set'
            line = base + f' ⏳ Waiting{editor_hint}'

        lines.append(line)

        # Add a button if we have the original Telegram message_id
        folder_data = pending_folders.get(r['folder_id'], {})
        tg_msg_id   = folder_data.get('telegram_message_id')
        if tg_msg_id:
            url = f"https://t.me/c/{bridge_tme_id}/{tg_msg_id}"
            buttons.append([{
                'text': f"Check {r['client_name']} / {r['folder_name']} 🔗",
                'url':  url,
            }])

    message = (
        "⚠️ Unassigned Folders Reminder\n\n"
        "The following folders have been waiting 5+ hours:\n"
        + '\n'.join(lines)
        + "\n\nTap the original messages to assign or use OpenClaw to reassign."
    )

    payload = {'chat_id': chat_id, 'text': message}
    if buttons:
        payload['reply_markup'] = json.dumps({'inline_keyboard': buttons})

    url  = f'https://api.telegram.org/bot{tg_token}/sendMessage'
    resp = requests.post(url, json=payload, timeout=10)
    if resp.ok:
        logger.info(f'Reminder sent for {len(folders)} folder(s) ({len(buttons)} with links).')
    else:
        logger.error(f'Telegram send failed: {resp.status_code} {resp.text}')


if __name__ == '__main__':
    main()

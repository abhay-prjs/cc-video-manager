"""
unassigned_reminder.py
Checks Notion Active Queue for folders with Status == "Raw"
submitted 5+ hours ago, then sends a Telegram reminder per folder with
an inline editor keyboard so Vex can assign directly from the reminder.
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
PROJECT_NUMBERS_FILE = os.path.join(BASE_DIR, 'project_numbers.json')

ACTIVE_QUEUE_DB    = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
THRESHOLD_HOURS    = 5

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_project_number(folder_id):
    if not folder_id or not os.path.exists(PROJECT_NUMBERS_FILE):
        return ''
    try:
        with open(PROJECT_NUMBERS_FILE) as f:
            data = json.load(f)
        n = data.get(folder_id)
        return f'#{n}' if n else ''
    except Exception:
        return ''


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def fetch_editors(token):
    """Returns list of active editor names (Capacity > 0) from Editor Profiles DB."""
    url  = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=15)
    editors = []
    if resp.ok:
        for page in resp.json().get('results', []):
            props    = page['properties']
            name_rt  = props.get('Editor', {}).get('title', [])
            name     = name_rt[0].get('plain_text', '') if name_rt else ''
            capacity = props.get('Capacity', {}).get('number')
            if name and capacity:
                editors.append(name)
    return editors


def fetch_stale_folders(token):
    """
    Returns list of dicts for Active Queue rows where Status is Raw
    AND Submitted is more than THRESHOLD_HOURS ago.
    """
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
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

        editor_sel  = props.get('Editor', {}).get('select') or {}
        editor_name = editor_sel.get('name', '')

        notes_rt    = props.get('Notes', {}).get('rich_text', [])
        notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
        m           = re.search(r'Videos:\s*(\d+)', notes)
        video_count = int(m.group(1)) if m else 0

        drive_link = props.get('Drive Link', {}).get('url') or ''
        m2         = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link)
        folder_id  = m2.group(1) if m2 else ''

        submitted_prop = (props.get('Submitted', {}).get('date') or {}).get('start', '')
        if not submitted_prop:
            continue

        try:
            dt = datetime.fromisoformat(submitted_prop)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(f'Could not parse Submitted date: {submitted_prop!r}')
            continue

        hours_ago = (now_utc - dt).total_seconds() / 3600
        if hours_ago >= THRESHOLD_HOURS:
            rows.append({
                'client_name': client_name,
                'folder_name': folder_name,
                'video_count': video_count,
                'editor_name': editor_name,
                'folder_id':   folder_id,
                'hours_ago':   hours_ago,
                'submitted_dt': dt,
            })

    return rows


def format_time_ago(hours_ago):
    if hours_ago < 24:
        return f'{int(hours_ago)}h ago'
    days = int(hours_ago // 24)
    h    = int(hours_ago % 24)
    return f'{days}d {h}h ago' if h else f'{days}d ago'


def main():
    config   = load_config()
    token    = config['notion_token']
    tg_token = config['notion_bridge_token']
    chat_id  = config['notion_bridge_chat_id']

    folders = fetch_stale_folders(token)
    if not folders:
        logger.info('No stale Raw folders (5+ hours) — nothing to send.')
        return

    folders.sort(key=lambda r: r['submitted_dt'])
    editors = fetch_editors(token)

    tg_url = f'https://api.telegram.org/bot{tg_token}/sendMessage'

    for r in folders:
        time_str    = format_time_ago(r['hours_ago'])
        folder_id   = r['folder_id']
        client_name = r['client_name']
        folder_name = r['folder_name']
        video_count = r['video_count']

        pnum = get_project_number(folder_id)
        pnum_suffix = f' <b>{pnum}</b>' if pnum else ''

        if r['editor_name']:
            status_line = f"⏳ Waiting — assigned to {r['editor_name']}"
        else:
            status_line = '⚠️ Not assigned'

        text = (
            f"🔔 <b>Reminder: {client_name} / {folder_name}</b>{pnum_suffix}\n"
            f"{video_count} videos — submitted {time_str}\n"
            f"{status_line}"
        )

        keyboard = None
        if editors and folder_id:
            keyboard = {
                'inline_keyboard': [
                    [{'text': e, 'callback_data': f'assign:{e}:{folder_id}:{client_name}:{video_count}'}]
                    for e in editors
                ]
            }

        payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML'}
        if keyboard:
            payload['reply_markup'] = json.dumps(keyboard)

        resp = requests.post(tg_url, json=payload, timeout=10)
        if resp.ok:
            logger.info(f'Reminder sent: {client_name}/{folder_name}')
        else:
            logger.error(f'Telegram send failed for {folder_name}: {resp.status_code} {resp.text}')


if __name__ == '__main__':
    main()

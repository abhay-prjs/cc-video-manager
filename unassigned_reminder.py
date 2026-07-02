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

BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE           = os.path.join(BASE_DIR, 'config.json')
PROJECT_NUMBERS_FILE  = os.path.join(BASE_DIR, 'project_numbers.json')
IGNORED_FOLDERS_FILE  = os.path.join(BASE_DIR, 'ignored_folders.json')

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


def notion_query_all(token, db_id, body=None):
    """Queries a Notion database, following has_more/next_cursor until all rows are fetched."""
    url     = f'https://api.notion.com/v1/databases/{db_id}/query'
    results = []
    cursor  = None
    while True:
        req_body = dict(body or {})
        req_body['page_size'] = 100
        if cursor:
            req_body['start_cursor'] = cursor
        resp = requests.post(url, headers=notion_headers(token), json=req_body, timeout=15)
        if not resp.ok:
            logger.error(f'notion_query_all failed for {db_id}: {resp.status_code} {resp.text[:200]}')
            break
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def load_ignored_folders():
    try:
        with open(IGNORED_FOLDERS_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def fetch_stale_folders(token):
    """
    Returns list of dicts for Active Queue rows where Status is Raw
    AND Submitted is more than THRESHOLD_HOURS ago.
    Skips folders present in ignored_folders.json.
    """
    ignored = load_ignored_folders()
    body = {'filter': {'property': 'Status', 'select': {'equals': 'Raw'}}}
    pages = notion_query_all(token, ACTIVE_QUEUE_DB, body)

    rows    = []
    now_utc = datetime.now(timezone.utc)

    for page in pages:
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
            if not folder_id:
                logger.info(f'Skipping folder with no Drive Link ID ({folder_name}) — cannot verify ignore status')
                continue
            if folder_id in ignored:
                logger.info(f'Skipping ignored folder {folder_id} ({folder_name})')
                continue
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


def send_discord_ops_embed(config, embed):
    channel_id = config.get('ops_channel_id')
    token = config.get('discord_bot_token')
    if not channel_id or not token:
        logger.error('ops_channel_id or discord_bot_token missing in config')
        return False
    try:
        resp = requests.post(
            f'https://discord.com/api/v10/channels/{channel_id}/messages',
            headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json'},
            json={'embeds': [embed]},
            timeout=10,
        )
        return resp.ok
    except Exception as e:
        logger.error(f'Discord ops channel error: {e}')
        return False


def main():
    config = load_config()
    token  = config['notion_token']

    folders = fetch_stale_folders(token)
    if not folders:
        logger.info('No stale Raw folders (5+ hours) — nothing to send.')
        return

    folders.sort(key=lambda r: r['submitted_dt'])

    lines = []
    for r in folders:
        time_str    = format_time_ago(r['hours_ago'])
        folder_id   = r['folder_id']
        client_name = r['client_name']
        folder_name = r['folder_name']
        video_count = r['video_count']

        pnum = get_project_number(folder_id)
        pnum_suffix = f' **{pnum}**' if pnum else ''

        if r['editor_name']:
            status_line = f"⏳ Assigned to {r['editor_name']}"
        else:
            status_line = '⚠️ Not assigned'

        lines.append(
            f"• **{client_name} / {folder_name}**{pnum_suffix}\n"
            f"  {video_count} videos — {time_str} — {status_line}"
        )

    desc = '\n'.join(lines)
    if len(desc) > 4000:
        desc = desc[:4000] + '…'
    embed = {
        'title': f'🔔 Unassigned Reminder ({len(folders)} folder{"s" if len(folders) != 1 else ""})',
        'description': desc,
        'color': 0xf1c40f,
    }
    ok = send_discord_ops_embed(config, embed)
    if ok:
        logger.info(f'Reminder sent: {len(folders)} folder(s)')
    else:
        logger.error('Discord send failed')


if __name__ == '__main__':
    main()

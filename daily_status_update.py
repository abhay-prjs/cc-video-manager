"""
daily_status_update.py
Posts the end-of-day report to the #bot-status channel (11PM IST via cron).
Replaces the old plain-text daily_summary.py in the ops channel.

Design goals (per Vex, 2026-07-02):
  - each day is instantly distinguishable: weekday in the title + a fixed
    per-weekday embed color
  - every item is a hyperlink that jumps straight to the thing: Drive folder
    for deliveries/unassigned, Notion row for revisions, the Discord review
    message for pending reviews
  - sections: delivered today, revisions received today, submissions awaiting
    review, unassigned folders
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta

import requests

BASE_DIR                = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE             = os.path.join(BASE_DIR, 'config.json')
PENDING_REVIEWS_FILE    = os.path.join(BASE_DIR, 'pending_reviews.json')
PENDING_OPS_ASSIGNS_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')

BOT_STATUS_CHANNEL  = '1503993880717299723'
MAIN_GUILD_ID       = '1356655526515310835'

ACTIVE_QUEUE_DB     = '44593fbf-4276-47f0-bd12-27289dcb78fd'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'
REVISION_LOG_DB     = 'a05a523e-2489-45f4-ae69-4aaf3178aca7'
DELIVERY_DATE_PROP  = 'date:Delivered Date:start'

IST = timezone(timedelta(hours=5, minutes=30))
# Deliveries/revisions are date-stamped with the EDT date by the bots
# (finalize_delivery uses now_edt) — query with EDT so a run near IST midnight
# still reports the correct business day.
EDT = timezone(timedelta(hours=-4))

# One fixed color per weekday so the embed itself tells you which day it is.
WEEKDAY_COLORS = {
    0: 0x3498db,  # Monday    — blue
    1: 0x2ecc71,  # Tuesday   — green
    2: 0x9b59b6,  # Wednesday — purple
    3: 0xe67e22,  # Thursday  — orange
    4: 0x1abc9c,  # Friday    — teal
    5: 0xfd79a8,  # Saturday  — pink
    6: 0xf1c40f,  # Sunday    — yellow
}

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }


def query(token, db_id, body):
    r = requests.post(f'https://api.notion.com/v1/databases/{db_id}/query',
                      headers=notion_headers(token), json=body, timeout=20)
    return r.json().get('results', []) if r.ok else []


def _title(props, name):
    rt = props.get(name, {}).get('title', [])
    return rt[0].get('plain_text', '') if rt else ''


def _text(props, name):
    rt = props.get(name, {}).get('rich_text', [])
    return rt[0].get('plain_text', '') if rt else ''


def _select(props, name):
    return (props.get(name, {}).get('select') or {}).get('name', '')


def _clamp(lines, limit=1020):
    val = '\n'.join(lines)
    return val[:limit] + '…' if len(val) > limit else val


def section_delivered(token, today_str):
    rows = query(token, DELIVERY_HISTORY_DB, {
        'filter': {'property': DELIVERY_DATE_PROP, 'date': {'equals': today_str}},
        'sorts':  [{'property': 'Editor', 'direction': 'ascending'}],
        'page_size': 100,
    })
    lines, total = [], 0
    for p in rows:
        pr     = p['properties']
        folder = _title(pr, 'Folder')
        client = _text(pr, 'Client')
        editor = _select(pr, 'Editor')
        vids   = pr.get('Videos Completed', {}).get('number') or 0
        link   = pr.get('Drive Link', {}).get('url') or ''
        total += int(vids)
        label  = f'{client} / {folder}'
        label  = f'[{label}]({link})' if link else f'**{label}**'
        lines.append(f'• {label} — {editor} — {int(vids)} vids')
    return lines, total


def section_revisions(token, today_str):
    rows = query(token, REVISION_LOG_DB, {
        'filter': {'property': 'Date', 'date': {'equals': today_str}},
        'page_size': 50,
    })
    lines = []
    for p in rows:
        pr     = p['properties']
        folder = _title(pr, 'Folder Name')
        client = _text(pr, 'Creator')
        editor = _select(pr, 'Editor')
        link   = (pr.get('Active Queue Link', {}).get('url')
                  or pr.get('Edited Folder', {}).get('url') or '')
        label  = f'{client} / {folder}'
        label  = f'[{label}]({link})' if link else f'**{label}**'
        lines.append(f'• {label} → {editor}')
    return lines


def section_pending_reviews():
    try:
        with open(PENDING_REVIEWS_FILE) as f:
            reviews = json.load(f)
    except Exception:
        return []
    lines = []
    for rd in reviews.values():
        if rd.get('status') != 'pending':
            continue
        label = f"{rd.get('client_name')} / {rd.get('folder_name')}"
        msg_id, ch_id = rd.get('review_message_id'), rd.get('review_channel_id')
        if msg_id and ch_id:
            jump = f'https://discord.com/channels/{MAIN_GUILD_ID}/{ch_id}/{msg_id}'
            label = f'[{label}]({jump})'
        else:
            label = f'**{label}**'
        lines.append(f"• {label} — {rd.get('editor_name')} — {rd.get('videos_done')} vids")
    return lines


def section_unassigned(token):
    rows = query(token, ACTIVE_QUEUE_DB, {
        'filter': {'property': 'Status', 'select': {'equals': 'Raw'}},
        'page_size': 50,
    })
    # Map folder_id -> its ops-assign message (the "choose editor" dropdown in the
    # assignments channel) so the link jumps straight to where Vex can assign it.
    ops_by_folder = {}
    try:
        with open(PENDING_OPS_ASSIGNS_FILE) as f:
            for msg_id, v in json.load(f).items():
                if v.get('folder_id'):
                    ops_by_folder[v['folder_id']] = (v.get('channel_id'), msg_id)
    except Exception:
        pass

    import re
    lines = []
    for p in rows:
        pr     = p['properties']
        folder = _title(pr, 'Video')
        client = _text(pr, 'Creator')
        drive  = pr.get('Drive Link', {}).get('url') or ''
        m = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive)
        folder_id = m.group(1) if m else ''
        label = f'{client} / {folder}'
        if folder_id in ops_by_folder:
            ch_id, msg_id = ops_by_folder[folder_id]
            jump = f'https://discord.com/channels/{MAIN_GUILD_ID}/{ch_id}/{msg_id}'
            label = f'[{label}]({jump})'
        elif drive:
            label = f'[{label}]({drive})'
        else:
            label = f'**{label}**'
        lines.append(f'• {label}')
    return lines


def main():
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token = config['notion_token']

    now_ist   = datetime.now(IST)
    now_edt   = datetime.now(EDT)
    today_str = now_edt.strftime('%Y-%m-%d')

    delivered, total_vids = section_delivered(token, today_str)
    revisions  = section_revisions(token, today_str)
    reviews    = section_pending_reviews()
    unassigned = section_unassigned(token)

    fields = []
    fields.append({
        'name': f'✅ Delivered Today ({len(delivered)} folders · {total_vids} videos)',
        'value': _clamp(delivered) if delivered else 'None',
        'inline': False,
    })
    fields.append({
        'name': f'🔄 Revisions Received ({len(revisions)})',
        'value': _clamp(revisions) if revisions else 'None ✅',
        'inline': False,
    })
    if reviews:
        fields.append({
            'name': f'📤 Submitted, Awaiting Approval ({len(reviews)})',
            'value': _clamp(reviews),
            'inline': False,
        })
    fields.append({
        'name': f'⏳ Not Yet Assigned ({len(unassigned)})',
        'value': _clamp(unassigned) if unassigned else 'All assigned ✅',
        'inline': False,
    })

    weekday = now_edt.weekday()
    embed = {
        'title': f"📅 {now_edt.strftime('%A')} Update — {now_edt.strftime('%B %d, %Y')}",
        'color': WEEKDAY_COLORS[weekday],
        'fields': fields,
        'footer': {'text': now_ist.strftime('Sent %I:%M %p IST')},
    }
    r = requests.post(
        f'https://discord.com/api/v10/channels/{BOT_STATUS_CHANNEL}/messages',
        headers={'Authorization': f"Bot {config['discord_bot_token']}", 'Content-Type': 'application/json'},
        json={'embeds': [embed]}, timeout=15)
    logger.info(f'daily status update sent: {r.status_code} — delivered={len(delivered)} '
                f'revisions={len(revisions)} reviews={len(reviews)} unassigned={len(unassigned)}')
    if not r.ok:
        logger.error(r.text[:300])


if __name__ == '__main__':
    main()

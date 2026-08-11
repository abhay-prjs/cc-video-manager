"""Cron (Saturday 15:30 UTC = 11:30 PM PHT): posts the final weekly (Sun-Sat)
leaderboard numbers to the leaderboard channel, pinging @Editors, so Vex has
a number to run bonus calculations off before the Sunday reset zeroes the
Editor Profiles counters.

Replaced the old Monday-00:00-UTC auto-post (discord_bot.py's leaderboard_loop,
WEEKLY_LEADERBOARD_AUTOPOST_ENABLED) on 2026-08-08 — Vex wants the weekly
number posted the night before reset, not the morning after. leaderboard_loop's
weekly branch is disabled again to avoid a duplicate post; its monthly
branch/posting logic is untouched.

Week cadence changed from Mon-Sun to Sun-Sat on 2026-08-08 (same day) — Vex
wanted weeks to end Saturday night instead of Sunday night. reset_weekly.py's
cron moved from Monday 00:00 UTC to Sunday 00:00 UTC to match; this script's
post moved from Sunday night to Saturday night to stay "night before reset."

'week' per editor is max(live Delivery History sum, cached 'Delivered This
Week' Editor Profiles field) — same reconciliation logic as
fetch_all_editor_stats_for_range() in discord_bot.py, duplicated here rather
than imported (see test/replay_dashboard_status.py's docstring for why
discord_bot.py — a ~7000 line module with a live discord.Client — isn't
imported into standalone scripts).

Usage:
    python3 weekly_leaderboard_post.py            # posts for real
    python3 weekly_leaderboard_post.py --dry-run   # prints, doesn't post
"""
import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'
DATE_PROP = 'date:Delivered Date:start'
LEADERBOARD_CHANNEL_ID = 1499407261381038242
EDITORS_ROLE_ID = 1498943182296190977
EDT = timezone(timedelta(hours=-4))

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def notion_headers(token):
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Notion-Version': '2022-06-28'}


def query_all(token, db_id, body):
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results, cursor = [], None
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


def fetch_week_numbers(token):
    today = datetime.now(EDT)
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)  # most recent Sunday
    week_start_str = week_start.strftime('%Y-%m-%d')
    tomorrow_str = (today + timedelta(days=1)).strftime('%Y-%m-%d')

    editors = {}
    for page in query_all(token, EDITOR_PROFILES_DB, {}):
        props = page['properties']
        name_rt = props.get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else ''
        capacity = props.get('Capacity', {}).get('number')
        if not name or not capacity:
            continue
        editors[name] = props.get('Delivered This Week', {}).get('number') or 0

    filt = {'filter': {'and': [
        {'property': DATE_PROP, 'date': {'on_or_after': week_start_str}},
        {'property': DATE_PROP, 'date': {'before': tomorrow_str}},
    ]}}
    live = {name: 0 for name in editors}
    for page in query_all(token, DELIVERY_HISTORY_DB, filt):
        props = page['properties']
        videos = props.get('Videos Completed', {}).get('number') or 0
        sel = props.get('Editor', {}).get('select') or {}
        name = sel.get('name', '')
        if name in live:
            live[name] += videos

    return {name: max(cached, live[name]) for name, cached in editors.items()}, week_start, today


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    with open(CONFIG_FILE) as f:
        config = json.load(f)
    notion_token = config['notion_token']
    bot_token = config['discord_bot_token']

    stats, week_start, today = fetch_week_numbers(notion_token)
    ranked = sorted(stats.items(), key=lambda kv: kv[1], reverse=True)

    medals = ['🥇', '🥈', '🥉']
    lines = []
    for i, (name, count) in enumerate(ranked):
        medal = medals[i] if i < 3 else ''
        prefix = f'{i + 1}. {medal}' if medal else f'{i + 1}.'
        lines.append(f'{prefix} {name} — {count} videos')

    description = '\n'.join(lines) if lines else 'No data.'
    footer = f"Week of {week_start.strftime('%b %-d')} — {today.strftime('%b %-d')} (as of 11:30 PM PHT)"

    if args.dry_run:
        logger.info(f'[DRY RUN] Would post:\n{description}\n{footer}')
        return

    headers = {
        'Authorization': f'Bot {bot_token}',
        'Content-Type': 'application/json',
        'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
    }
    payload = {
        'content': f'<@&{EDITORS_ROLE_ID}>',
        'embeds': [{
            'title': '🏆 Final Weekly Leaderboard — Bonus Calc',
            'description': description,
            'footer': {'text': footer},
            'color': 15844367,
        }],
        'allowed_mentions': {'parse': [], 'roles': [str(EDITORS_ROLE_ID)]},
    }
    resp = requests.post(
        f'https://discord.com/api/v10/channels/{LEADERBOARD_CHANNEL_ID}/messages',
        headers=headers, json=payload, timeout=15,
    )
    if resp.ok:
        logger.info(f'Posted final weekly numbers: {stats}')
    else:
        logger.error(f'Failed to post: {resp.status_code} {resp.text[:300]}')


if __name__ == '__main__':
    main()

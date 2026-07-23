"""
Reset Delivered This Month to 0 for all editors in Notion Editor Profiles,
and roll over editor_counters.json (revisions, missed_deadlines, slow_pickups)
from all-time to monthly: last month's final tally is archived into
editor_counters_history.json keyed by "YYYY-MM", then the live counters
reset to 0. This is what /stats and /editorstats read, so their
📈 Performance field becomes "this month" instead of all-time — a rough
month no longer follows an editor forever, and a good month doesn't hide
one. Added 2026-07-22.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
EDITOR_COUNTERS_FILE = os.path.join(BASE_DIR, 'editor_counters.json')
EDITOR_COUNTERS_HISTORY_FILE = os.path.join(BASE_DIR, 'editor_counters_history.json')
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def rollover_editor_counters():
    """Archive the just-finished month's counters, then zero out the live file."""
    if not os.path.exists(EDITOR_COUNTERS_FILE):
        logger.info('No editor_counters.json — nothing to roll over.')
        return

    with open(EDITOR_COUNTERS_FILE) as f:
        counters = json.load(f)

    if not counters:
        logger.info('editor_counters.json empty — nothing to roll over.')
        return

    # reset_monthly.py runs at 00:00 UTC on the 1st — "last month" is yesterday's month.
    last_month_key = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m')

    history = {}
    if os.path.exists(EDITOR_COUNTERS_HISTORY_FILE):
        try:
            with open(EDITOR_COUNTERS_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            history = {}
    history[last_month_key] = counters
    with open(EDITOR_COUNTERS_HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)
    logger.info(f'Archived editor_counters.json → editor_counters_history.json[{last_month_key}]')

    reset = {name: {k: 0 for k in fields} for name, fields in counters.items()}
    with open(EDITOR_COUNTERS_FILE, 'w') as f:
        json.dump(reset, f, indent=2)
    logger.info(f'Reset editor_counters.json for {len(reset)} editor(s)')


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def main():
    rollover_editor_counters()

    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token = config['notion_token']

    resp = requests.post(
        f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query',
        headers=notion_headers(token),
        json={},
        timeout=15,
    )
    if not resp.ok:
        logger.error(f'Failed to fetch editors: {resp.text}')
        return

    pages = resp.json().get('results', [])
    for page in pages:
        page_id = page['id']
        name_rt = page['properties'].get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else page_id
        requests.patch(
            f'https://api.notion.com/v1/pages/{page_id}',
            headers=notion_headers(token),
            json={'properties': {'Delivered This Month': {'number': 0}}},
            timeout=15,
        )
        logger.info(f'Reset monthly for {name}')


if __name__ == '__main__':
    main()

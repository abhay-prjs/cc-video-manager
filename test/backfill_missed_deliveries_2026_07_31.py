"""
One-off backfill: same pattern as backfill_jewel_4_deliveries.py — folders
marked Status=Delivered directly in Notion Active Queue, bypassing
finalize_delivery(), so they never got a Delivery History row or credited
Editor Profiles stats.

Found by cross-referencing all Active Queue Status=Delivered rows from the
last 30 days against Delivery History (fuzzy match on editor + edited folder
name) and delivery_meta.json (keyed by notion_page_id) on 2026-07-31.

Video counts verified against live Drive contents before running:
  Storm / Ivan   / PB6      -> 4 videos   (Notion said 4, matches)
  Danie / Niki   / Invo 7   -> 108 videos (Notion said 108, matches)
  Aki   / Aiden  / cupie 5  -> 12 videos  (Notion said 12, matches)

NOT included: Aki / Yuki / "Lovable 4" (Notion claimed 2 videos, but the
matching Edited/Lovable 4 Drive folder is empty — 0 files). That one is not
a detection miss, it's a real discrepancy, and is intentionally skipped here
rather than crediting videos that aren't in Drive. Flag to Vex separately.

Run once. Not wired into cron/systemd.
"""
import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord_bot as db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DELIVERIES = [
    {
        'editor_name': 'Storm',
        'editor_page_id': '396e637c-317d-802f-8963-f4e998e12ba9',
        'folder_name': 'PB6',
        'client_name': 'Ivan',
        'edited_folder': 'PB6',
        'count': 4,
        'drive_link': 'https://drive.google.com/drive/folders/1bWH3hXUJYGhry2t5ehi1DAp3bXMWveKY',
        'folder_id': '1bWH3hXUJYGhry2t5ehi1DAp3bXMWveKY',  # has a stale deadlines.json entry
    },
    {
        'editor_name': 'Danie',
        'editor_page_id': '390e637c-317d-8002-ac09-dc297a8b9d6e',
        'folder_name': 'Invo 7',
        'client_name': 'Niki',
        'edited_folder': 'Invo 7',
        'count': 108,
        'drive_link': 'https://drive.google.com/drive/folders/18-WbTWFPyh98ae00chJ9oSvrBYOxxi-l',
        'folder_id': None,
    },
    {
        'editor_name': 'Aki',
        'editor_page_id': '37ee637c-317d-81b5-bf70-fb94d64879db',
        'folder_name': 'cupie 5',
        'client_name': 'Aiden',
        'edited_folder': 'cupie 5',
        'count': 12,
        'drive_link': 'https://drive.google.com/drive/folders/1iinEEm8MbGXQvX7A5ZID45P8ZywLGHA5',
        'folder_id': None,
    },
]


def main():
    config = db.load_config()
    token = config['notion_token']
    today_str = datetime.now(db.EDT).strftime('%Y-%m-%d')

    deadlines_path = os.path.join(BASE_DIR, 'deadlines.json')
    with open(deadlines_path) as f:
        deadlines = json.load(f)

    # Group by editor so each editor's stats are only patched once.
    by_editor = {}
    for d in DELIVERIES:
        by_editor.setdefault(d['editor_name'], []).append(d)

    for d in DELIVERIES:
        db.create_delivery_history_row(
            token,
            d['folder_name'],
            d['client_name'],
            d['editor_name'],
            d['count'],
            today_str,
            d['edited_folder'],
            d['drive_link'],
        )
        print(f"Delivery History row created: {d['editor_name']} / {d['client_name']} / {d['edited_folder']} ({d['count']} videos)")

    for editor_name, items in by_editor.items():
        editor_page_id = items[0]['editor_page_id']
        total_count = sum(i['count'] for i in items)
        page = db._notion_get(token, editor_page_id)
        props = page['properties']
        week = props.get('Delivered This Week', {}).get('number') or 0
        month = props.get('Delivered This Month', {}).get('number') or 0
        total = props.get('Total Videos Delivered', {}).get('number') or 0
        new_week, new_month, new_total = week + total_count, month + total_count, total + total_count
        print(f"Before update — {editor_name} This Week: {week}, This Month: {month}, Total: {total}")
        resp = db._notion_patch(token, editor_page_id, {
            'Delivered This Week': {'number': new_week},
            'Delivered This Month': {'number': new_month},
            'Total Videos Delivered': {'number': new_total},
        })
        if resp.ok:
            print(f"After update — {editor_name} This Week: {new_week}, This Month: {new_month}, Total: {new_total}")
        else:
            print(f"FAILED Editor Profiles PATCH for {editor_name}: {resp.status_code} {resp.text}")
        db.recalculate_active_videos(token, editor_name)

    for d in DELIVERIES:
        if d['folder_id'] and d['folder_id'] in deadlines:
            deadlines.pop(d['folder_id'], None)
            print(f"Cleared stale deadlines.json entry for {d['editor_name']}/{d['folder_name']} (folder_id={d['folder_id']})")

    with open(deadlines_path, 'w') as f:
        json.dump(deadlines, f, indent=2)


if __name__ == '__main__':
    main()

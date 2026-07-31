"""
One-off backfill: 4 Jewel deliveries were marked Status=Delivered directly in
Notion Active Queue, bypassing finalize_delivery() — so they never got a
Delivery History row, never incremented Jewel's Editor Profiles stats, and
(for the Chris folder) never popped their deadlines.json entry.

Verified against live Drive contents (ground truth per CLAUDE.md — Notion's
Videos Completed field drifts) on 2026-07-31 before running this:
  Judy    / Launchpoint 4          -> 10 videos
  Jauseff / Manus 3                -> 9 videos
  Henry   / Cookiy AI 7/28/26      -> 7 videos
  Chris   / July 27th Composio     -> 7 videos

Run once. Not wired into cron/systemd.
"""
import os
import sys
import time
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord_bot as db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EDITOR_NAME = 'Jewel'
EDITOR_PAGE_ID = '396e637c-317d-801c-ad3a-e75d939bf3cf'

DELIVERIES = [
    {
        'folder_name': 'Launchpoint 4',
        'client_name': 'Judy',
        'edited_folder': 'Launchpoint 4',
        'count': 10,
        'drive_link': 'https://drive.google.com/drive/folders/1dk2sYQfRyeda5Qxh_XrDri1OuNrg5Lnv',
    },
    {
        'folder_name': 'manus 3',
        'client_name': 'Jauseff',
        'edited_folder': 'Manus 3',
        'count': 9,
        'drive_link': 'https://drive.google.com/drive/folders/1Q2l-pYHAsRHWP1q2XUNQY_nduAaD9tCf',
    },
    {
        'folder_name': 'Cookiy AI 7/28/26',
        'client_name': 'Henry',
        'edited_folder': 'Cookiy AI 7/28/26',
        'count': 7,
        'drive_link': 'https://drive.google.com/drive/folders/1bEU3-0s2dN7vv6fWeeuHa0Ntnw65Rka9',
    },
    {
        'folder_name': 'July 27th composio',
        'client_name': 'Chris',
        'edited_folder': 'July 27th Composio',
        'count': 7,
        'drive_link': 'https://drive.google.com/drive/folders/1wgi3rkyCTXYP1t1SGvc6xwaiIXv55zHM',
        'notion_page_id': '3aae637c-317d-8184-9dc9-dab17cb045c7',
        'folder_id': '1wgi3rkyCTXYP1t1SGvc6xwaiIXv55zHM',  # matches stale deadlines.json entry
    },
]


def main():
    config = db.load_config()
    token = config['notion_token']
    today_str = datetime.now(db.EDT).strftime('%Y-%m-%d')

    total_count = 0
    for d in DELIVERIES:
        db.create_delivery_history_row(
            token,
            d['folder_name'],
            d['client_name'],
            EDITOR_NAME,
            d['count'],
            today_str,
            d['edited_folder'],
            d['drive_link'],
        )
        total_count += d['count']
        print(f"Delivery History row created: {d['client_name']} / {d['edited_folder']} ({d['count']} videos)")

    # Increment Jewel's Editor Profiles stats once, by the combined count.
    page = db._notion_get(token, EDITOR_PAGE_ID)
    props = page['properties']
    week = props.get('Delivered This Week', {}).get('number') or 0
    month = props.get('Delivered This Month', {}).get('number') or 0
    total = props.get('Total Videos Delivered', {}).get('number') or 0
    new_week, new_month, new_total = week + total_count, month + total_count, total + total_count
    print(f"Before update — Jewel This Week: {week}, This Month: {month}, Total: {total}")
    resp = db._notion_patch(token, EDITOR_PAGE_ID, {
        'Delivered This Week': {'number': new_week},
        'Delivered This Month': {'number': new_month},
        'Total Videos Delivered': {'number': new_total},
    })
    if resp.ok:
        print(f"After update — Jewel This Week: {new_week}, This Month: {new_month}, Total: {new_total}")
    else:
        print(f"FAILED Editor Profiles PATCH: {resp.status_code} {resp.text}")

    db.recalculate_active_videos(token, EDITOR_NAME)

    # Clear the stale deadlines.json entry for Chris's folder (already Delivered
    # in Notion but never popped since it skipped finalize_delivery()).
    deadlines_path = os.path.join(BASE_DIR, 'deadlines.json')
    with open(deadlines_path) as f:
        deadlines = json.load(f)
    chris_fid = DELIVERIES[3]['folder_id']
    if chris_fid in deadlines:
        deadlines.pop(chris_fid, None)
        with open(deadlines_path, 'w') as f:
            json.dump(deadlines, f, indent=2)
        print(f"Cleared stale deadlines.json entry for folder_id={chris_fid}")
    else:
        print("No stale deadlines.json entry found for Chris's folder")


if __name__ == '__main__':
    main()

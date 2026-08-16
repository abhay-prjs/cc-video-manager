"""
One-off backfill: Chris/"July 4th lovable format 2" and Chris/"July 12th lovable
format 2" were assigned to Jill and marked Status=Delivered in Active Queue
(Videos Completed=60 each, Edited Folder Name="All lovables to ship" — the
shared bucket folder), but never went through finalize_delivery(), so no
Delivery History row was created and Jill's stats were never credited.

Confirmed 2026-07-31: both pages exist, both assigned to Jill, both Delivered,
counts (60 each) taken from Notion's own Videos Completed field.

Run once. Not wired into cron/systemd.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import discord_bot as db

EDITOR_NAME = 'Jill'
EDITOR_PAGE_ID = '350e637c-317d-80c7-8cbf-ff25c9078983'

DELIVERIES = [
    {
        'folder_name': 'July 4th lovable format 2',
        'client_name': 'Chris',
        'edited_folder': 'All lovables to ship',
        'count': 60,
        'drive_link': 'https://drive.google.com/drive/folders/1L07TfexZQ4aXEnCSyMuL16UibZK9Q4B6',
    },
    {
        'folder_name': 'July 12th lovable format 2',
        'client_name': 'Chris',
        'edited_folder': 'All lovables to ship',
        'count': 60,
        'drive_link': 'https://drive.google.com/drive/folders/1CcivayJ-cu3FSTKpOYHbstOmw9FYVWzi',
    },
]


def main():
    config = db.load_config()
    token = config['notion_token']
    today_str = datetime.now(db.EDT).strftime('%Y-%m-%d')

    total_count = 0
    for d in DELIVERIES:
        db.create_delivery_history_row(
            token, d['folder_name'], d['client_name'], EDITOR_NAME,
            d['count'], today_str, d['edited_folder'], d['drive_link'],
        )
        total_count += d['count']
        print(f"Delivery History row created: {d['client_name']} / {d['edited_folder']} ({d['count']} videos)")

    page = db._notion_get(token, EDITOR_PAGE_ID)
    props = page['properties']
    week = props.get('Delivered This Week', {}).get('number') or 0
    month = props.get('Delivered This Month', {}).get('number') or 0
    total = props.get('Total Videos Delivered', {}).get('number') or 0
    new_week, new_month, new_total = week + total_count, month + total_count, total + total_count
    print(f"Before update — Jill This Week: {week}, This Month: {month}, Total: {total}")
    resp = db._notion_patch(token, EDITOR_PAGE_ID, {
        'Delivered This Week': {'number': new_week},
        'Delivered This Month': {'number': new_month},
        'Total Videos Delivered': {'number': new_total},
    })
    if resp.ok:
        print(f"After update — Jill This Week: {new_week}, This Month: {new_month}, Total: {new_total}")
    else:
        print(f"FAILED Editor Profiles PATCH: {resp.status_code} {resp.text}")

    db.recalculate_active_videos(token, EDITOR_NAME)


if __name__ == '__main__':
    main()

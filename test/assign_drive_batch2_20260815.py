"""One-off: assign the 17 Drive/Notion Raw folders that are >5h old, per the
plan Vex approved in chat (2026-08-15, second pass after the earlier
bulk_assign_20260815.py / rollback_bulk_assign_20260815.py mistake+retract on
the "Ivan / Ape 4" title collision). This run avoids that bug by matching
every folder to its Notion page_id directly (baked into
/tmp/drive_assign_plan_20260815.json) instead of matching on title, and
explicitly pins the new "Ape 4" to the Raw page_id
3bbe637c-317d-8192-af6a-d4c82da33ea4 — verified distinct from Josh's existing
In-Progress "Ape 4" (different Drive folder id, different page_id, created
12 min apart).

Zyon (off today), Jewel (158v hidden in active website batches), Josh (light
touch, 2 only) and 5 folders <5h old (Noiz 3, Invo 4, invo 34, invo 5,
invo 35) are intentionally excluded from this pass.

Follows the real /assign path: enqueues a plain (type-less) discord_queue.json
item per folder so the running discord-bot service's poll loop (every 3s)
runs the actual assign_folder() — Discord embed, deadlines.json entry,
recalculate_active_videos, dashboard push — exactly like the slash command.

Dry-run by default; pass --live to actually write.
"""
import argparse
import json
import os
import time

from filelock import FileLock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
QUEUE_LOCK = FileLock(QUEUE_FILE + '.lock')

with open('/tmp/drive_assign_plan_20260815.json') as f:
    PLAN = json.load(f)


def enqueue(item):
    with QUEUE_LOCK:
        existing = []
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f:
                existing = json.load(f)
        existing.append(item)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(existing, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()

    print(f'Plan: {len(PLAN)} folders.')
    for row in PLAN:
        print(f"  {row['client_name']:10} / {row['folder_name']:35} ({row['video_count']:>3}v) -> {row['editor']}")

    if not args.live:
        print('\nDry run — no writes. Re-run with --live to execute.')
        return

    print()
    for row in PLAN:
        enqueue({
            'client_name':          row['client_name'],
            'folder_name':          row['folder_name'],
            'video_count':          row['video_count'],
            'folder_id':            row['folder_id'],
            'editor_name':          row['editor'],
            'notion_queue_page_id': row['notion_page_id'],
            'project_number':       row.get('project_number', ''),
        })
        print(f"  Queued: {row['client_name']} / {row['folder_name']} -> {row['editor']}")
        time.sleep(0.3)

    print(f'\nQueued {len(PLAN)} assignments for discord-bot to pick up (polls every 3s).')


if __name__ == '__main__':
    main()

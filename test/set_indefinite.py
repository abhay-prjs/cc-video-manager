"""One-off: mark specific deadlines.json entries indefinite, replicating exactly
what /extend -> 0 hours does in discord_bot.py's ExtendHoursModal.on_submit
(discord_bot.py:7348-7379). Not queue-based (no IPC item type for this exists)
so ideally run while discord-bot traffic on these folders is quiet.

Usage: python3 test/set_indefinite.py FOLDER_ID [FOLDER_ID ...]
"""
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEADLINES_FILE = os.path.join(BASE_DIR, 'deadlines.json')


def main():
    folder_ids = sys.argv[1:]
    if not folder_ids:
        print('Usage: python3 test/set_indefinite.py FOLDER_ID [FOLDER_ID ...]')
        sys.exit(1)

    with open(DEADLINES_FILE) as f:
        deadlines = json.load(f)

    for fid in folder_ids:
        entry = deadlines.get(fid)
        if not entry:
            print(f'  SKIP {fid}: no deadlines.json entry')
            continue
        entry['missed_deadline_logged'] = False
        if entry.get('pending_start'):
            entry['pending_start'] = False
            entry['started_at'] = entry.get('started_at') or time.time()
        entry['indefinite'] = True
        entry['due_ts'] = None
        entry['warned_6h'] = False
        print(f"  SET INDEFINITE: {fid} ({entry.get('client_name')}/{entry.get('folder_name')}, {entry.get('editor_name')})")

    with open(DEADLINES_FILE, 'w') as f:
        json.dump(deadlines, f, indent=2)


if __name__ == '__main__':
    main()

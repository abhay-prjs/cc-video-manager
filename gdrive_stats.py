import os
import json
import argparse
from datetime import datetime, timedelta
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, 'watched_files.json')
CLIENTS_FILE = os.path.join(BASE_DIR, 'clients.json')
EDITED_FILE = os.path.join(BASE_DIR, 'edited_files.json')

VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v', '.mts', '.wmv', '.flv', '.3gp'}


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def load_all_clients():
    if os.path.exists(CLIENTS_FILE):
        with open(CLIENTS_FILE) as f:
            return json.load(f)
    return []


def normalize_name(name):
    name = os.path.splitext(name)[0].lower()
    for suffix in ['_edited', '_done', '_final', '_export', '_v2', '_v3', ' edited', ' done', ' final']:
        name = name.replace(suffix, '')
    return name.strip()


def fuzzy_match(a, b, threshold=0.75):
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio() >= threshold


def get_pending(state):
    return {fid: f for fid, f in state.items()
            if f.get('status') != 'completed' and is_video(f.get('name', ''))}


def get_completed(state):
    return {fid: f for fid, f in state.items()
            if f.get('status') == 'completed' and is_video(f.get('name', ''))}


def stats(client=None, days=None):
    state = load_state()
    pending = get_pending(state)
    completed = get_completed(state)

    # Filter by days — uses created_at (Drive upload date) or detected_at (watcher saw it)
    if days:
        cutoff = datetime.now() - timedelta(days=int(days))
        pending = {
            fid: f for fid, f in pending.items()
            if (f.get('created_at') and f['created_at'][:10] >= cutoff.strftime('%Y-%m-%d'))
            or (f.get('detected_at') and datetime.fromisoformat(f['detected_at']) >= cutoff)
        }

    # Filter by client
    if client and client.lower() != 'all':
        pending = {fid: f for fid, f in pending.items() if f['client'].lower() == client.lower()}
        completed = {fid: f for fid, f in completed.items() if f['client'].lower() == client.lower()}

    # Group pending by client
    clients = {}
    for fid, f in pending.items():
        c = f['client']
        if c not in clients:
            clients[c] = {'pending': [], 'folders': {}}
        clients[c]['pending'].append(f['name'])
        folder = f.get('folder', 'Unknown')
        clients[c]['folders'][folder] = clients[c]['folders'].get(folder, 0) + 1

    lines = []
    if client and client.lower() != 'all':
        # Detailed single client view
        c = client
        if c not in clients:
            lines.append(f"✅ No pending videos for {c}")
        else:
            data = clients[c]
            lines.append(f"📊 <b>{c}</b> — {len(data['pending'])} pending\n")
            for folder, count in data['folders'].items():
                lines.append(f"  📁 {folder}: {count} video(s)")
            lines.append(f"\n🎬 Files:")
            for i, name in enumerate(data['pending'], 1):
                lines.append(f"  {i}. {name}")
            done_count = len([f for f in completed.values() if f['client'].lower() == c.lower()])
            lines.append(f"\n✅ Completed: {done_count}")
    else:
        # Overview — show ALL clients including zeros
        total_pending = len(pending)
        lines.append(f"📊 <b>Pending Videos Overview</b>\n")

        all_clients = load_all_clients()
        if not all_clients:
            all_clients = sorted(set(f['client'] for f in state.values()))

        for c in all_clients:
            if c in clients:
                data = clients[c]
                folder_breakdown = ', '.join([f"{folder}: {count}" for folder, count in data['folders'].items()])
                lines.append(f"👤 <b>{c}</b> — {len(data['pending'])} pending ({folder_breakdown})")
            else:
                lines.append(f"👤 <b>{c}</b> — 0 pending ✅")

        lines.append(f"\n📦 Total pending: {total_pending}")
        lines.append(f"✅ Total completed: {len(completed)}")

    print('\n'.join(lines))


def list_edited(client):
    if not os.path.exists(EDITED_FILE):
        print("No edited files data yet. Run the watcher first.")
        return
    with open(EDITED_FILE) as f:
        data = json.load(f)
    client_files = [f for f in data if f['client'].lower() == client.lower()]
    if not client_files:
        print(f"No edited videos found for {client}")
        return
    lines = [f"✂️ <b>{client}</b> — Edited folder ({len(client_files)} file(s))\n"]
    for i, f in enumerate(client_files, 1):
        link = f" — <a href='{f['link']}'>Open</a>" if f.get('link') else ""
        lines.append(f"  {i}. {f['name']}{link}")
    print('\n'.join(lines))


def mark_done(client=None, filename=None, all_client=False):
    state = load_state()
    marked = 0

    for fid, f in state.items():
        if f.get('status') == 'completed':
            continue

        match = False
        if all_client and client and f['client'].lower() == client.lower():
            match = True
        elif client and filename:
            if f['client'].lower() == client.lower() and fuzzy_match(f['name'], filename):
                match = True
        elif filename and not client:
            if fuzzy_match(f['name'], filename):
                match = True

        if match:
            state[fid]['status'] = 'completed'
            state[fid]['completed_at'] = datetime.now().isoformat()
            marked += 1
            print(f"✅ Marked done: {f['client']} — {f['name']}")

    save_state(state)
    if marked == 0:
        print("No matching files found to mark as done.")
    else:
        print(f"\nMarked {marked} file(s) as completed.")


def auto_detect_completed(edited_files):
    state = load_state()
    pending = get_pending(state)
    auto_marked = 0

    for edited_name in edited_files:
        for fid, f in pending.items():
            if fuzzy_match(f['name'], edited_name):
                state[fid]['status'] = 'completed'
                state[fid]['completed_at'] = datetime.now().isoformat()
                state[fid]['completed_by'] = 'auto'
                state[fid]['matched_edit'] = edited_name
                auto_marked += 1
                print(f"🤖 Auto-completed: {f['client']} — {f['name']} (matched: {edited_name})")

    save_state(state)
    return auto_marked


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='command')

    stats_parser = subparsers.add_parser('stats')
    stats_parser.add_argument('--client', default=None)
    stats_parser.add_argument('--days', default=None)

    done_parser = subparsers.add_parser('done')
    done_parser.add_argument('--client', default=None)
    done_parser.add_argument('--file', default=None)
    done_parser.add_argument('--all', action='store_true', dest='all_client')

    edited_parser = subparsers.add_parser('edited')
    edited_parser.add_argument('--client', required=True)

    args = parser.parse_args()

    if args.command == 'stats':
        stats(client=args.client, days=args.days)
    elif args.command == 'done':
        mark_done(client=args.client, filename=args.file, all_client=args.all_client)
    elif args.command == 'edited':
        list_edited(client=args.client)
    else:
        stats()

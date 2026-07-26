import argparse
import os
import json
import requests
from logger_setup import get_logger
logger = get_logger('gdrive_watcher')
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

EDT = timezone(timedelta(hours=-4))
IST = timezone(timedelta(hours=5, minutes=30))
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/drive']
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.avi'}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
CREDS_FILE = os.path.join(BASE_DIR, 'credentials.json')
STATE_FILE = os.path.join(BASE_DIR, 'watched_files.json')
CLIENTS_FILE = os.path.join(BASE_DIR, 'clients.json')
EDITED_FILE = os.path.join(BASE_DIR, 'edited_files.json')


def is_video(filename):
    return os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS


def is_video_item(item):
    # Extension OR mimeType — some clients upload videos with extension-less names.
    return (item.get('mimeType') != 'application/vnd.google-apps.folder'
            and (is_video(item.get('name', ''))
                 or item.get('mimeType', '').startswith('video/')))


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            flow.redirect_uri = 'urn:ietf:wg:oauth:2.0:oob'
            auth_url, _ = flow.authorization_url(prompt='consent')
            print(f"\nOpen this URL in your browser:\n{auth_url}\n")
            code = input("Paste the code here: ")
            flow.fetch_token(code=code)
            creds = flow.credentials
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('drive', 'v3', credentials=creds)


def _thread_service():
    """Build a Drive service for worker threads (assumes token is already fresh)."""
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    return build('drive', 'v3', credentials=creds)


def send_discord_ops_channel(config, message=None, embed=None):
    import re
    channel_id = config.get('ops_channel_id')
    token = config.get('discord_bot_token')
    if not channel_id or not token:
        print('ops_channel_id or discord_bot_token missing in config')
        return
    payload = {}
    if message:
        text = re.sub(r'<b>(.*?)</b>', r'**\1**', message)
        text = re.sub(r'<i>(.*?)</i>', r'*\1*', text)
        text = re.sub(r'<[^>]+>', '', text)
        payload['content'] = text
    if embed:
        payload['embeds'] = [embed]
    try:
        requests.post(
            f'https://discord.com/api/v10/channels/{channel_id}/messages',
            headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json'},
            json=payload,
            timeout=10,
        )
    except Exception as e:
        print(f'Discord ops channel error: {e}')


def list_folder_contents(service, folder_id):
    results = []
    page_token = None
    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, createdTime, webViewLink)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        results.extend(response.get('files', []))
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    return results


def find_folder_by_name(service, name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    results = service.files().list(q=q, fields="files(id, name)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    files = results.get('files', [])
    return files[0] if files else None


def get_subfolder_video_info(service, folder_id):
    """Returns (video_count, video_names) for video files directly inside folder_id."""
    items = list_folder_contents(service, folder_id)
    names = [
        item['name'] for item in items
        if is_video_item(item)
    ]
    return len(names), names


def get_folder_video_tree(service, folder_id, folder_name):
    """
    Returns (total_count, video_tree, flat_names) for folder_id.
    video_tree maps section label → [video filenames].
    Recurses to arbitrary depth — some clients nest videos 3+ levels deep
    (e.g. Karol's 'lovable 2' → channel → format → files), and a fixed
    depth limit silently drops those folders from new-folder detection.
    """
    video_tree = {}
    flat_names = []

    def walk(fid, label, is_root):
        items = list_folder_contents(service, fid)
        videos = [item['name'] for item in items if is_video_item(item)]
        if videos:
            key = f'{label} (root)' if is_root else label
            video_tree[key] = videos
            flat_names.extend(videos)
        for item in items:
            if item['mimeType'] != 'application/vnd.google-apps.folder':
                continue
            child_label = item['name'] if is_root else f'{label} / {item["name"]}'
            walk(item['id'], child_label, False)

    walk(folder_id, folder_name, True)
    return len(flat_names), video_tree, flat_names


def get_all_files_recursive(service, folder_id, client_name, folder_name):
    """Recursively scans for video files — used for edited files scan."""
    all_files = []
    items = list_folder_contents(service, folder_id)
    for item in items:
        if item['mimeType'] == 'application/vnd.google-apps.folder':
            all_files.extend(
                get_all_files_recursive(service, item['id'], client_name, item['name'])
            )
        else:
            all_files.append({
                'id': item['id'],
                'name': item['name'],
                'mimeType': item.get('mimeType', ''),
                'client': client_name,
                'folder': folder_name,
                'created_at': item.get('createdTime', ''),
                'link': item.get('webViewLink', '')
            })
    return all_files


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def find_clients_for_parent_ids(service, clients, parent_ids):
    """Return the subset of clients that have activity in the given parent folder IDs."""
    parent_id_set = set(parent_ids)
    matching = []
    for client in clients:
        raw_footage = find_folder_by_name(service, 'Raw Footage', client['id'])
        if not raw_footage:
            continue
        if raw_footage['id'] in parent_id_set:
            matching.append(client)
            continue
        items = list_folder_contents(service, raw_footage['id'])
        subfolder_ids = {
            item['id'] for item in items
            if item['mimeType'] == 'application/vnd.google-apps.folder'
        }
        if subfolder_ids & parent_id_set:
            matching.append(client)
    return matching


def scan_client(client):
    """Scan one client folder in a worker thread. Returns (folder_dict, edited_list)."""
    service = _thread_service()
    local_folders = {}
    local_edited = []

    raw_footage = find_folder_by_name(service, 'Raw Footage', client['id'])
    if raw_footage:
        items = list_folder_contents(service, raw_footage['id'])
        subfolders = [item for item in items if item['mimeType'] == 'application/vnd.google-apps.folder']
        for sf in subfolders:
            total_count, video_tree, flat_names = get_folder_video_tree(service, sf['id'], sf['name'])
            if total_count == 0:
                continue
            local_folders[sf['id']] = {
                'folder_id':   sf['id'],
                'folder_name': sf['name'],
                'client':      client['name'],
                'video_count': total_count,
                'video_names': flat_names,
                'video_tree':  video_tree,
            }

        print(f"  {client['name']}: {len(subfolders)} subfolder(s) in Raw Footage")
    else:
        print(f"  No Raw Footage folder for {client['name']}, skipping")

    edited_folder = find_folder_by_name(service, 'Edited', client['id'])
    if edited_folder:
        files = get_all_files_recursive(service, edited_folder['id'], client['name'], 'Edited')
        local_edited = [f for f in files if is_video_item(f)]

    return local_folders, local_edited


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent-ids', default=None,
                        help='Comma-separated parent folder IDs from changes.list — limits scan to matching clients only')
    args = parser.parse_args()

    parent_ids = [pid.strip() for pid in args.parent_ids.split(',')] if args.parent_ids else None

    config = load_config()
    service = get_drive_service()

    # Find root folder
    root = find_folder_by_name(service, config['root_folder_name'])
    if not root:
        q = f"name='{config['root_folder_name']}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(
            q=q,
            fields="files(id, name)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True
        ).execute()
        files = results.get('files', [])
        if not files:
            print(f"Could not find folder: {config['root_folder_name']}")
            return
        root = files[0]

    print(f"Found root folder: {root['name']} ({root['id']})")

    # Determine which client folders to scan
    target_clients = None
    if parent_ids:
        all_clients = [
            f for f in list_folder_contents(service, root['id'])
            if f['mimeType'] == 'application/vnd.google-apps.folder'
        ]
        matched = find_clients_for_parent_ids(service, all_clients, parent_ids)
        if matched:
            print(f"Targeted scan: {len(matched)} client(s) matched parent IDs {parent_ids}")
            target_clients = matched
        else:
            print(f"No clients matched parent IDs {parent_ids} — falling back to full scan")

    if target_clients is None:
        all_folders = list_folder_contents(service, root['id'])
        target_clients = [
            f for f in all_folders
            if f['mimeType'] == 'application/vnd.google-apps.folder'
        ]
        print(f"Full scan: {len(target_clients)} client folders")

        client_names = sorted([c['name'] for c in target_clients])
        with open(CLIENTS_FILE, 'w') as f:
            json.dump(client_names, f)

    # Scan selected clients in parallel
    all_current_folders = {}
    edited_data = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(scan_client, client): client for client in target_clients}
        for future in as_completed(futures):
            client = futures[future]
            try:
                folders, edited = future.result()
                all_current_folders.update(folders)
                edited_data.extend(edited)
            except Exception as e:
                print(f"  Error scanning {client['name']}: {e}")

    if parent_ids is None:
        with open(EDITED_FILE, 'w') as f:
            json.dump(edited_data, f, indent=2)
        print(f"Saved {len(edited_data)} edited video(s) to edited_files.json")

    state = load_state()

    # Migrate from old file-based state format
    if state and any('name' in v for v in state.values() if isinstance(v, dict)):
        print("\nMigrating state from file-based to folder-based tracking. No alerts this run.")
        save_state(all_current_folders)
        return

    # First run — save state, no alerts
    if not state:
        print(f"\nFirst run — saving {len(all_current_folders)} existing folders. No alerts sent.")
        save_state(all_current_folders)
        return

    # Detect new subfolders
    new_folders = {fid: f for fid, f in all_current_folders.items() if fid not in state}
    print(f"\n{len(new_folders)} new folder(s) detected")

    if new_folders:
        try:
            from notion_bridge import send_new_folder_notification, is_folder_ignored
        except Exception as e:
            print(f"notion_bridge import error ({e}), using plain notification for all new folders")
            send_new_folder_notification, is_folder_ignored = None, None

        for fid, f in new_folders.items():
            if is_folder_ignored and is_folder_ignored(fid):
                print(f"Skipping ignored folder: {f['client']} / {f['folder_name']}")
                state[fid] = {**f, 'detected_at': datetime.now().isoformat()}
                continue
            try:
                if send_new_folder_notification is None:
                    raise RuntimeError('notion_bridge unavailable')
                send_new_folder_notification(config, f)
                print(f"Assignment notification: {f['client']} / {f['folder_name']} — {f['video_count']} video(s)")
                state[fid] = {**f, 'detected_at': datetime.now().isoformat()}
            except Exception as e:
                # Don't mark as handled — a failure here means no Active Queue row was
                # created, so this folder must be retried on the next run, not silently
                # dropped. Fall back to a plain Discord ping only as a heads-up.
                print(f"notion_bridge error for {f['client']}/{f['folder_name']} ({e}), using plain notification")
                now = datetime.now(EDT).astimezone(IST).strftime("%b %d, %Y · %I:%M %p IST")
                send_discord_ops_channel(config, embed={
                    'title': '📁 New Folder Detected',
                    'description': f"**{f['client']} / {f['folder_name']}**\n"
                                   f"🎬 {f['video_count']} videos\n🕐 {now}",
                    'color': 0x3498db,
                })
                print(f"Alert sent: {f['client']} — {f['folder_name']}")
    else:
        print("No new folders detected.")

    # Detect video count increases in already-known folders
    updated_folders = {
        fid: f for fid, f in all_current_folders.items()
        if fid in state and f['video_count'] > state[fid].get('video_count', 0)
    }
    print(f"{len(updated_folders)} folder(s) with increased video count")

    if updated_folders:
        try:
            from notion_bridge import send_folder_update_notification
            for fid, f in updated_folders.items():
                prev_count = state[fid].get('video_count', 0)
                send_folder_update_notification(config, f, f['video_count'], prev_count)
                print(f"Update notification: {f['client']} / {f['folder_name']} — {prev_count} → {f['video_count']}")
                state[fid]['video_count'] = f['video_count']
                state[fid]['video_names'] = f.get('video_names', [])
        except Exception as e:
            print(f"notion_bridge update error ({e})")
            for fid, f in updated_folders.items():
                prev_count = state[fid].get('video_count', 0)
                now = datetime.now(EDT).astimezone(IST).strftime("%b %d, %Y · %I:%M %p IST")
                send_discord_ops_channel(config, embed={
                    'title': '📥 Folder Updated',
                    'description': f"**{f['client']} / {f['folder_name']}**\n"
                                   f"🎬 {prev_count} → {f['video_count']} (+{f['video_count'] - prev_count})\n🕐 {now}",
                    'color': 0x3498db,
                })
                state[fid]['video_count'] = f['video_count']
                state[fid]['video_names'] = f.get('video_names', [])

    save_state(state)


if __name__ == '__main__':
    main()

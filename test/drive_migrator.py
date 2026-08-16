"""
drive_migrator.py — Migrate a Drive folder into the In-House Editor Shared Drive.

Usage:
    python3 drive_migrator.py --old-folder-id OLD_ID [--new-folder-name "Razeen"]

Steps:
    1. Copies all files + subfolders from the old folder into a new folder
       created inside the In-House Editor Shared Drive root.
    2. Shows a summary of everything copied.
    3. Prompts you to confirm before deleting the originals.
"""

import os
import sys
import argparse
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
SCOPES = ['https://www.googleapis.com/auth/drive']

SHARED_DRIVE_ROOT_ID = '1hKXUhKZZo1WN-B5h309CEiSgZbogUoum'


def get_drive_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build('drive', 'v3', credentials=creds)


def list_folder_contents(service, folder_id):
    """Return all files and folders directly inside folder_id."""
    items = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields='nextPageToken, files(id, name, mimeType)',
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        items.extend(resp.get('files', []))
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return items


def create_folder(service, name, parent_id):
    """Create a folder inside parent_id on the Shared Drive."""
    meta = {
        'name': name,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [parent_id],
    }
    folder = service.files().create(
        body=meta,
        fields='id, name',
        supportsAllDrives=True,
    ).execute()
    return folder['id']


def copy_file(service, file_id, file_name, dest_folder_id):
    """Copy a single file into dest_folder_id."""
    body = {'name': file_name, 'parents': [dest_folder_id]}
    copied = service.files().copy(
        fileId=file_id,
        body=body,
        fields='id, name',
        supportsAllDrives=True,
    ).execute()
    return copied['id']


def delete_item(service, item_id):
    service.files().delete(
        fileId=item_id,
        supportsAllDrives=True,
    ).execute()


def migrate_recursive(service, src_folder_id, dest_folder_id, indent=0, copied_files=None, src_ids=None):
    """
    Recursively copy src_folder_id contents into dest_folder_id.
    Tracks copied file IDs and original IDs for later cleanup.
    """
    if copied_files is None:
        copied_files = []
    if src_ids is None:
        src_ids = []

    prefix = '  ' * indent
    items = list_folder_contents(service, src_folder_id)

    if not items:
        print(f"{prefix}  (empty folder)")
        return copied_files, src_ids

    for item in items:
        is_folder = item['mimeType'] == 'application/vnd.google-apps.folder'
        if is_folder:
            print(f"{prefix}  [folder] {item['name']}")
            new_subfolder_id = create_folder(service, item['name'], dest_folder_id)
            src_ids.append(('folder', item['id'], item['name']))
            migrate_recursive(service, item['id'], new_subfolder_id, indent + 1, copied_files, src_ids)
        else:
            print(f"{prefix}  [file]   {item['name']}")
            new_id = copy_file(service, item['id'], item['name'], dest_folder_id)
            copied_files.append((new_id, item['name']))
            src_ids.append(('file', item['id'], item['name']))

    return copied_files, src_ids


def get_folder_name(service, folder_id):
    try:
        f = service.files().get(
            fileId=folder_id,
            fields='name',
            supportsAllDrives=True,
        ).execute()
        return f['name']
    except Exception:
        return folder_id


def main():
    parser = argparse.ArgumentParser(description='Migrate a Drive folder into the In-House Editor Shared Drive.')
    parser.add_argument('--old-folder-id', required=True, help='Drive ID of the old folder to migrate')
    parser.add_argument('--new-folder-name', default=None, help='Name for the new folder (defaults to old folder name)')
    args = parser.parse_args()

    service = get_drive_service()

    old_id = args.old_folder_id
    old_name = get_folder_name(service, old_id)
    new_name = args.new_folder_name or old_name

    print(f"\nSource folder : {old_name} ({old_id})")
    print(f"Destination   : In-House Editor / {new_name}")
    print(f"Shared Drive  : {SHARED_DRIVE_ROOT_ID}\n")
    print("Creating new folder...")
    new_folder_id = create_folder(service, new_name, SHARED_DRIVE_ROOT_ID)
    print(f"New folder created: {new_name} ({new_folder_id})\n")

    print("Copying contents...\n")
    copied_files, src_ids = migrate_recursive(service, old_id, new_folder_id)

    print(f"\n{'='*50}")
    print(f"Migration complete.")
    print(f"  Files copied : {sum(1 for t, _, _ in src_ids if t == 'file')}")
    print(f"  Folders made : {sum(1 for t, _, _ in src_ids if t == 'folder')}")
    print(f"  New folder ID: {new_folder_id}")
    print(f"{'='*50}\n")

    print("VERIFY: Open the new folder in Drive and confirm everything looks correct.")
    print(f"  https://drive.google.com/drive/folders/{new_folder_id}\n")

    answer = input("Delete the originals from the old folder? [yes/no]: ").strip().lower()
    if answer != 'yes':
        print("Skipped deletion. Old folder is untouched.")
        print(f"Old folder: https://drive.google.com/drive/folders/{old_id}")
        sys.exit(0)

    print("\nDeleting originals...")
    files_to_delete = [(fid, name) for t, fid, name in src_ids if t == 'file']
    folders_to_delete = [(fid, name) for t, fid, name in src_ids if t == 'folder']

    for fid, name in files_to_delete:
        try:
            delete_item(service, fid)
            print(f"  deleted file   : {name}")
        except Exception as e:
            print(f"  FAILED to delete file {name} ({fid}): {e}")

    # Delete folders deepest-first (they were appended top-down, so reverse)
    for fid, name in reversed(folders_to_delete):
        try:
            delete_item(service, fid)
            print(f"  deleted folder : {name}")
        except Exception as e:
            print(f"  FAILED to delete folder {name} ({fid}): {e}")

    print("\nDone. Old folder contents removed.")
    print("You can now delete the old root folder manually if it's empty:")
    print(f"  https://drive.google.com/drive/folders/{old_id}")


if __name__ == '__main__':
    main()

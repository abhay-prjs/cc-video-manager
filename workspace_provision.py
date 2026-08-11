"""
Creator workspace provisioning: Drive folder + Notion row.

A creator's workspace is three things — their Discord edits channel, a Drive
folder holding "Raw Footage" and "Edited", and a Creator Assignments row so the
notification path can find them. The channel half already exists in
discord_bot.provision_create_pass; this module is the other two.

FORWARD ONLY. Every entry point takes a `since` cutoff and refuses to touch a
creator who joined before it. The roster has ~80 people who predate this, a
dozen of whom are duplicate profiles, abandoned signups or not people at all
("bolt hackathon"), and back-filling them is a human decision per row — not
something a pass should do because it happened to run.

DRY RUN BY DEFAULT. Nothing is created unless dry_run=False is passed
explicitly. Creating Drive folders and Notion rows is outward-facing and
awkward to undo, so the default has to be the harmless one.
"""


import logging
import re

import requests

from gdrive_watcher import find_folder_by_name, load_config

logger = logging.getLogger(__name__)

# What every creator's folder contains. Order matters only for readability.
SUBFOLDERS = ["Raw Footage", "Edited"]

FOLDER_MIME = "application/vnd.google-apps.folder"

# Creator Assignments. The live schema is Creator/Folder (title), Folder,
# Active, Discord User ID, Discord Channel ID, Backup Editor, Notes, Vids/Month.
#
# Only the first and the channel id are ever populated in practice: of 83 rows,
# 82 carry a Discord Channel ID and ZERO carry Backup Editor, Folder or Notes.
# `Primary Editor` is read by notion_bridge.get_folder_assignment and
# dashboard.py but does not exist on the database at all, so that lookup has
# always returned empty. Nothing here writes those dead fields.
ASSIGNMENTS_DB = "cead1699-21dc-4b0c-b0b6-00cf31c5fa29"
NOTION_VERSION = "2022-06-28"


def workspace_name(full_name):
    """First name plus one more — "Daniella Angela De Guzman" -> "Daniella
    Guzman".

    Not the whole name: a four-part name makes a folder nobody types. Not the
    first name alone either, which is what the roster does today and is exactly
    why there are already TWO folders called "Brian" and why "chris" resolves to
    two different people.
    """
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1]}"


def _child_folder(service, parent_id, name):
    """An immediate subfolder by exact stripped name, or None."""
    q = (f"'{parent_id}' in parents and mimeType='{FOLDER_MIME}' "
         "and trashed=false")
    page = None
    target = name.strip()
    while True:
        res = service.files().list(
            q=q, fields="nextPageToken, files(id, name)", pageSize=1000,
            pageToken=page, supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        for f in res.get("files", []):
            if f["name"].strip() == target:
                return f
        page = res.get("nextPageToken")
        if not page:
            return None


def _create_folder(service, name, parent_id):
    return service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    ).execute()


def ensure_drive_workspace(service, full_name, dry_run=True, root_id=None):
    """Make sure <root>/<Name>/{Raw Footage, Edited} exists.

    Returns {'name', 'folderId', 'folderUrl', 'created': [...], 'dryRun'} or
    {'error': ...}. Idempotent: an existing folder is adopted, never duplicated,
    and a half-made workspace (folder but no subfolders) is completed rather
    than remade.
    """
    name = workspace_name(full_name)
    if not name:
        return {"error": "no usable name to build a folder from"}

    if root_id is None:
        cfg = load_config()
        root = find_folder_by_name(service, cfg["root_folder_name"])
        if not root:
            return {"error": f"root folder {cfg['root_folder_name']!r} not found"}
        root_id = root["id"]

    created = []
    existing = _child_folder(service, root_id, name)
    if existing:
        folder_id = existing["id"]
    elif dry_run:
        # Nothing exists yet and we are not allowed to make it, so there is no
        # id to hang the subfolder checks off. Report the whole intent instead.
        return {
            "name": name,
            "folderId": None,
            "folderUrl": None,
            "created": [name] + [f"{name}/{s}" for s in SUBFOLDERS],
            "dryRun": True,
        }
    else:
        made = _create_folder(service, name, root_id)
        folder_id = made["id"]
        created.append(name)

    for sub in SUBFOLDERS:
        if _child_folder(service, folder_id, sub):
            continue
        if dry_run:
            created.append(f"{name}/{sub}")
        else:
            _create_folder(service, sub, folder_id)
            created.append(f"{name}/{sub}")

    return {
        "name": name,
        "folderId": folder_id,
        "folderUrl": f"https://drive.google.com/drive/folders/{folder_id}",
        "created": created,
        "dryRun": dry_run,
    }


def _notion_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def find_assignment_row(token, creator_name):
    """Existing Creator Assignments row for this creator, or None.

    Matched on the title, case-insensitively and on the FIRST name, because the
    rows are nicknames ("Cat" for Catherine Vuong, "Chris Li") while profiles
    carry full names. A too-strict match here would create a second row for
    someone who already has one, which is the duplicate problem this is
    supposed to avoid.
    """
    first = (creator_name or "").strip().split(" ")[0].lower()
    if not first:
        return None
    url = f"https://api.notion.com/v1/databases/{ASSIGNMENTS_DB}/query"
    payload = {"page_size": 100}
    while True:
        r = requests.post(url, headers=_notion_headers(token),
                          json=payload, timeout=20)
        if not r.ok:
            logger.warning("notion query failed: %s", r.status_code)
            return None
        data = r.json()
        for page in data.get("results", []):
            title = page["properties"].get("Creator/Folder", {}).get("title", [])
            text = title[0].get("plain_text", "") if title else ""
            head = text.strip().split(" ")[0].lower()
            if head and (head == first or first.startswith(head)
                         or head.startswith(first)):
                return page
        if not data.get("has_more"):
            return None
        payload["start_cursor"] = data["next_cursor"]


def ensure_assignment_row(token, creator_name, channel_id=None, dry_run=True):
    """Create the Creator Assignments row if the creator has none.

    Writes only what the database actually uses: the title, Active, and the
    Discord Channel ID that the editing bridge resolves creators on. The editor
    and Folder columns are left alone — they are empty on all 83 existing rows
    and one of them is read from a property that does not exist.
    """
    name = workspace_name(creator_name)
    if not name:
        return {"error": "no usable name for a notion row"}

    existing = find_assignment_row(token, name)
    if existing:
        return {"created": False, "pageId": existing["id"], "dryRun": dry_run}
    if dry_run:
        return {"created": True, "pageId": None, "dryRun": True}

    props = {
        "Creator/Folder": {"title": [{"text": {"content": name}}]},
        "Active": {"checkbox": True},
    }
    if channel_id:
        props["Discord Channel ID"] = {
            "rich_text": [{"text": {"content": str(channel_id)}}]
        }
    r = requests.post("https://api.notion.com/v1/pages",
                      headers=_notion_headers(token),
                      json={"parent": {"database_id": ASSIGNMENTS_DB},
                            "properties": props}, timeout=20)
    if not r.ok:
        return {"error": f"notion create failed: {r.status_code}"}
    return {"created": True, "pageId": r.json().get("id"), "dryRun": False}


def provision_new_creators(service, notion_token, pending, since,
                           dry_run=True):
    """Provision Drive + Notion for creators who joined on/after `since`.

    `pending` is the dashboard's provision feed — [{profileId, fullName,
    joinedAt, ...}]. `since` is an ISO date string and is REQUIRED: without it
    a pass would sweep the whole roster, which includes duplicate profiles,
    abandoned signups and three rows that are not people. Forward-only is the
    whole safety model here, so there is no "all" mode.

    Returns rows describing what happened (or would happen) per creator, so a
    dry run reads exactly like the real thing minus the writes.
    """
    if not since:
        raise ValueError("since is required — refusing to sweep the roster")

    cfg = load_config()
    root = find_folder_by_name(service, cfg["root_folder_name"])
    if not root:
        return [{"error": f"root folder {cfg['root_folder_name']!r} not found"}]

    out = []
    for p in pending or []:
        joined = (p.get("joinedAt") or "")[:10]
        name = p.get("fullName") or ""
        if not name.strip():
            out.append({"profileId": p.get("profileId"), "name": name,
                        "skipped": "no name on the profile"})
            continue
        if not joined or joined < since:
            out.append({"profileId": p.get("profileId"), "name": name,
                        "skipped": f"joined {joined or '?'} — before cutoff {since}"})
            continue

        drive = ensure_drive_workspace(service, name, dry_run=dry_run,
                                       root_id=root["id"])
        notion = ensure_assignment_row(notion_token, name,
                                       channel_id=p.get("channelId"),
                                       dry_run=dry_run)
        out.append({
            "profileId": p.get("profileId"),
            "name": workspace_name(name),
            "joined": joined,
            "drive": drive,
            "notion": notion,
        })
    return out

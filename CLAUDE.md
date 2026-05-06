# CC Video Manager — Codebase Guide

## What This Is
VexxeFX editing operations system. Manages 22+ clients and 5 editors via
Google Drive, Notion, Telegram, and Discord.

## Services
- `notion_bridge.py` — Telegram bot for Vex (assignment flow, review, reminders)
- `discord_bot.py` — Discord bot for editors (`/complete`, `/stats`, `/editorstats`)
- `gdrive_watcher.py` — Scans Drive for new folders, triggers notifications
- `drive_webhook.py` — Receives Google Drive push notifications on port 8081
- `dashboard.py` — Flask dashboard on port 8080
- `register_watch.py` — Registers Drive changes.watch (auto-renews every 23hrs)
- `daily_summary.py` — Sends daily ops summary at 11PM IST
- `unassigned_reminder.py` — Pings Vex for folders unassigned 5+ hours
- `query_stats.py` — CLI stats tool used by OpenClaw
- `reset_weekly.py` / `reset_monthly.py` — Resets editor stats on schedule

## Notion Databases
- Active Queue: `44593fbf-4276-47f0-bd12-27289dcb78fd`
- Editor Profiles: `a18d5c16-f359-4a2b-a620-6c837aa04232`
- Creator Assignments: `cead1699-21dc-4b0c-b0b6-00cf31c5fa29`
- Delivery History: `733883073ccf48f2a83953ba2d5ad36d`

## Drive Root Folder
- Name: `In-House Editor`
- ID: `1hKXUhKZZo1WN-B5h309CEiSgZbogUoum`

## Key Rules
- Always use `folder_id` not `folder_name` for Drive lookups
- All Drive API calls must include `supportsAllDrives=True` (and `includeItemsFromAllDrives=True` for list calls)
- **Never walk up via `files.get(fields='parents')`** — on this Shared Drive it returns `parents: []`. Always search top-down from the root folder instead
- Editor stats update in both `discord_bot` (direct complete) and `notion_bridge` (review approval)
- Never hardcode editor/client names — always pull from Notion

## Known Gotchas
- `find_edited_folder_videos()` must search **top-down** (root → client → Edited/) not walk up parents — see `_find_edited_folder_top_down()`
- Drive OAuth token scope must be `drive` not `drive.readonly`
- Delivery History date field is `Delivered Date`, not `date:Delivered Date:start`
- `files.get(fields='parents')` silently returns `[]` for all folders in this Shared Drive

## Systemd Services
- `discord-bot` — runs `discord_bot.py`
- `notion-bridge` — runs `notion_bridge.py`
- `gdrive-watcher` — runs `gdrive_watcher.py` on a timer
- `drive-webhook` — runs `drive_webhook.py`

# CC Video Manager — Codebase Guide

## What This Is
VexxeFX editing operations system. Manages 22+ clients and 5 editors via
Google Drive, Notion, Telegram, and Discord.

## Services
- `notion_bridge.py` — Telegram bot for Vex (assignment flow, review, reminders, ignore folders)
- `discord_bot.py` — Discord bot for editors (`/complete`, `/stats`, `/editorstats`, `/leaderboard`)
- `gdrive_watcher.py` — Scans Drive for new folders, triggers notifications (skips ignored folders)
- `drive_webhook.py` — Receives Google Drive push notifications on port 8081; writes `drive_webhook_last_ping.json` on every hit
- `health_monitor.py` — Cron every 30min: checks token, webhook ping, watch expiry, service health, log errors
- `reauth.py` — Interactive OOB OAuth re-auth; saves token.json, restarts services, sends Telegram confirm
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
- All `files().list()` calls must include `id` in the fields mask — omitting it silently breaks sub-folder iteration
- **Never walk up via `files.get(fields='parents')`** — on this Shared Drive it returns `parents: []`. Always search top-down from the root folder instead
- Editor stats update in both `discord_bot` (direct complete) and `notion_bridge` (review approval)
- Never hardcode editor/client names — always pull from Notion
- Notion PATCH rejects the **entire request** if any property name doesn't exist — always verify property names against the actual DB schema before adding new ones

## Known Gotchas
- `find_edited_folder_videos()` must search **top-down** (root → client → Edited/) not walk up parents — see `_find_edited_folder_top_down()`
- Drive OAuth token scope must be `drive` not `drive.readonly`
- Delivery History date field is `DELIVERY_DATE_PROP = 'date:Delivered Date:start'` (the actual Notion property name)
- `files.get(fields='parents')` silently returns `[]` for all folders in this Shared Drive
- `/stats` "delivered today" comes from a live Delivery History query (not the cached `Delivered This Week` counter in Editor Profiles)
- `Avg Turnaround Days` does **not** exist in Editor Profiles — do not include it in PATCH calls
- `_notion_patch()` in `discord_bot.py` now logs errors on non-200 responses; always check logs after stats updates

## Stats Update Flow
- `finalize_delivery()` (discord_bot) and `finalize_notion_delivery()` (notion_bridge) both:
  1. GET current Editor Profiles values fresh
  2. Add `confirmed_count` to `Delivered This Week`, `Delivered This Month`, `Total Videos Delivered`
  3. PATCH Editor Profiles
  4. Log "Before update" and "After update" with exact values
- Weekly leaderboard (`fetch_all_editor_stats()`) queries **Delivery History** for the current week (Monday→today) grouped by editor — does NOT read `Delivered This Week` from Editor Profiles (stale)
- Monthly stats still read from Editor Profiles `Delivered This Month`

## Show Contents (Telegram)
- `handle_show_callback()` always does a **fresh Drive API query** — never uses cached video_names
- `fetch_folder_video_tree()` recurses **2 levels deep**: root → subfolder → sub-subfolder
- Message format: clickable HTML drive link header + videos grouped by subfolder with `📂` section headers
- Single-section folders render as a flat numbered list (no section header)

## Ignore Folders
- Ignored folder IDs stored in `ignored_folders.json`
- Vex taps 🚫 Ignore on Telegram notification → folder skipped on all future watcher runs
- ↩️ Unignore button restores original notification with full editor keyboard
- `gdrive_watcher.py` calls `is_folder_ignored(fid)` before `send_new_folder_notification()`

## Leaderboard
- `/leaderboard` command in editors guild — weekly stats from live Delivery History query
- Auto-posts weekly every Monday 00:01 UTC and monthly on last day of month 23:00 UTC
- Posts to channel ID `1499407261381038242`
- Driven by `discord.ext.tasks` loop (hourly check in `leaderboard_loop`)

## Assignment Embed Drive Links
- `assign_folder()` does top-down search: `DRIVE_ROOT_ID` → client folder → Raw Footage → subfolder
- Caches client root in `_client_root_folder_cache`, Raw Footage in `_client_raw_footage_folder_cache`
- Both caches are in-memory (reset on bot restart)

## Health Monitor
- Runs every 30min via cron; logs to `logs/health_monitor.log`
- Alerts on: expired token, webhook ping >3h old, watch expiry <2h (auto re-registers), dead services (auto-restarts), >5 errors in last 30min in discord_bot.log or notion_bridge.log
- `drive_webhook_last_ping.json` — written by drive_webhook.py on every POST

## Systemd Services
- `discord-bot` — runs `discord_bot.py`
- `notion-bridge` — runs `notion_bridge.py`
- `gdrive-watcher` — runs `gdrive_watcher.py` on a timer
- `drive-webhook` — runs `drive_webhook.py`
- `gdrive-dashboard` — runs `dashboard.py`
- `ngrok-webhook` — ngrok tunnel for Drive webhook

# CC Video Manager — Codebase Guide

## What This Is
VexxeFX editing operations system. Manages 22+ clients and 5 editors via
Google Drive, Notion, Telegram, and Discord.

## Services
- `notion_bridge.py` — Telegram bot for Vex (assignment flow, review, reminders, ignore folders, stats commands: `/load`, `/pending`, `/today`, `/editor`, `/client`)
- `discord_bot.py` — Discord bot for editors (`/complete`, `/stats`, `/editorstats`, `/leaderboard`, `/myschedule`, `/changeschedule`)
- `gdrive_watcher.py` — Scans Drive for new folders, triggers notifications (skips ignored folders)
- `drive_webhook.py` — Receives Google Drive push notifications on port 8081; writes `drive_webhook_last_ping.json` on every hit
- `health_monitor.py` — Cron every 30min: checks token, webhook ping, watch expiry, service health, log errors
- `reauth.py` — Interactive OOB OAuth re-auth; saves token.json, restarts services, sends Telegram confirm
- `dashboard.py` — Flask dashboard on port 8080
- `register_watch.py` — Registers Drive changes.watch (auto-renews every 23hrs)
- `daily_summary.py` — Sends daily ops summary at 11PM IST
- `unassigned_reminder.py` — Pings Vex for folders unassigned 5+ hours
- `reset_weekly.py` / `reset_monthly.py` — Resets editor stats on schedule

## Notion Databases
- Active Queue: `44593fbf-4276-47f0-bd12-27289dcb78fd`
- Editor Profiles: `a18d5c16-f359-4a2b-a620-6c837aa04232`
- Creator Assignments: `cead1699-21dc-4b0c-b0b6-00cf31c5fa29`
- Delivery History: `733883073ccf48f2a83953ba2d5ad36d`
- Premium Clients: `5d29bbecf493477aa5aa4b4ba8ffe52e`
- Revision Log: `a05a523e-2489-45f4-ae69-4aaf3178aca7`

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

## Premium Server System
- Premium clients have their own Discord server (personal guild) with a VA who reviews deliveries before they're finalized
- Config: `premium_guild_ids` in `config.json` — list of Discord guild IDs for premium servers
- Notion Premium Clients DB (`5d29bbecf493477aa5aa4b4ba8ffe52e`) — one row per premium client with: `Name` (must match Creator field in Active Queue exactly), `Guild ID`, `Channel ID`, `VA User ID`, `Active`
- Delivery flow: `finalize_delivery()` calls `fetch_premium_server_for_client()` — if match found, sets Active Queue status to `Review` (not `Delivered`) and fires `premium_va_review_notify` to the premium channel
- Non-premium clients go straight to `Delivered` as before
- Premium slash commands: `/stats` (shows In Progress + Awaiting VA Approval + Revisions), `/allapproved` (VA approves → finalizes stats/history/Delivered), `/revision` (VA requests changes → reopens folder)
- `fetch_premium_client_by_channel_id()` maps the premium channel ID back to client name for slash command routing
- **Gotcha**: `Name` in Premium Clients DB must exactly match `Creator` in Active Queue — a mismatch silently skips the entire premium flow and delivers directly

## AI Ops Assistant
- `ai_ops.py` — shared module; calls local Ollama (`qwen2.5:3b` at `http://localhost:11434`) for editor recommendations and free-form queries
- No API keys or OpenClaw dependency — runs fully offline via Ollama
- `query_ai(message)` — sends system prompt + message, returns response text; 90s timeout, returns `''` on failure
- `build_context_from_ranked(ranked)` — builds editor context from `_rank_editors()` output (notion_bridge format)
- `build_context_from_editors(editors)` — builds editor context from `fetch_editors_from_notion()` output (discord_bot format)
- `ai_recommend_editor(ranked, folder_name, client, video_count)` — returns `(editor_name, reason)` or `('', '')` on failure; caller falls back to `ranked[0]`
- `ai_answer_query(context_str, question)` — answers a free-form ops question

## AI-Powered Auto-Assign
- When `/autoassign on`, new folders trigger `ai_recommend_editor()` before falling back to rank-based
- AI reasoning shown in Telegram ping: `💡 <reason>` line under the assignment
- ↩️ Override button still present — Vex can always reassign manually
- Silent fallback to rank-based if AI times out or returns an unknown editor name

## /ask Command
- **Telegram**: `/ask <question>` — live context (loads + shifts + revision/missed counts) sent to AI; reply in chat
- **Discord**: `/ask <question>` — Team role only, ephemeral; uses editor profiles context (no schedule data)
- Example queries: "who is available right now", "who can take a folder in 2 hours", "who has lightest load"

## Editor Performance Counters
- Stored in `editor_counters.json` — `{editor_name: {revisions: N, missed_deadlines: N}}`
- `revisions` incremented in `open_revision_assignment()` each time a folder is sent back
- `missed_deadlines` incremented in `deadline_checker()` when `due_ts` passes without delivery; guarded by `missed_deadline_logged` flag in `deadlines.json` to prevent double-counting
- Visible in `/stats` (📈 Performance field, Team role only) and `/editorstats` (📈 Editor Performance section)
- Also fed into AI context so assignment decisions account for editor reliability

## Reassign Flow
- Triggered via `/reassign` in Telegram (notion_bridge) or `/reassign` in Discord
- On reassign, three things happen automatically:
  1. **Creator notified** — creator's Discord channel gets `🔁 [folder] reassigned to [new editor]`
  2. **Old editor notified** — old editor's Discord channel gets `📢 [folder] reassigned away from you to [new editor]`
  3. **Stats recalculated** — `recalculate_active_videos()` called for both old and new editor
- Routed through `discord_queue.json` IPC using `type: reassign_notify` → `handle_reassign_notify()` in discord_bot
- Telegram path also enqueues via `_append_to_discord_queue()` so all notifications go through the same Discord handler
- `_enqueue_reassign_notify(client_name, folder_name, old_editor, new_editor)` is the helper in discord_bot
- New editor receives a `🔁 Reassigned to You` embed (orange) via `assign_folder(is_reassign=True)` — distinct from the normal `📁 New Assignment` (blue) so they know it was reassigned, not fresh
- `is_reassign=True` is set in `_enqueue_reassign()` and passed through the queue dispatcher to `assign_folder()`

## /ask Schedule Query — Known Issue & Fix Needed

### Current state (as of 2026-06-03)
- `/ask who is online right now` works correctly in both Telegram and Discord
- "Who is online" type questions feed a **pre-computed schedule fact** into the prompt so the model only needs to present it, not reason about it
- Load/assignment questions (`who has lightest load`, `who should I assign to`) are **unreliable** — qwen2.5:3b frequently hallucinates wrong editor names and wrong load %

### Root cause
- Schedule data lives in **Editor Profiles** DB as `Mon Schedule`…`Sun Schedule` + `Timezone` properties — NOT in the separate Editor Schedules DB
- qwen2.5:3b (1.9GB) is too small for reliable reasoning over tabular data — it ignores context and leaks chain-of-thought
- qwen2.5:7b is downloaded and available; switching the one-line `OLLAMA_MODEL` constant in `ai_ops.py` enables it

### Fix plan for morning
1. Switch `OLLAMA_MODEL = 'qwen2.5:7b'` in `ai_ops.py` — already downloaded, 4.7GB, much better instruction-following
2. Test `/ask who has lightest load` and `/ask who should I assign this to` to confirm 7b handles load queries correctly
3. If 7b is still unreliable for load queries, consider pre-computing load facts the same way schedule facts are pre-computed (see `_schedule_fact()` in `ai_ops.py` as the pattern)

### Schedule cache
- `schedule_cache.json` — populated at bot startup, refreshed every 2h via cron (`refresh_schedule_cache.py`)
- Cache stores all 7 days per editor from Editor Profiles; `fetch_schedules_from_profiles()` reads from it
- Cache TTL is `SCHEDULE_CACHE_TTL = 7200` seconds in `ai_ops.py`

## Editor Schedule Commands
- `/myschedule` — editor-channel-only; fetches Mon–Sun schedule from Editor Profiles, converts from their stored timezone (e.g. `PHT (UTC+8)`) to EST (UTC-5), displays as embed with 🟢/🔴 per day
- `/changeschedule` — editor-channel-only; opens a modal for the editor to describe the change they want; on submit forwards the request to Vex via:
  - Telegram (HTML formatted, uses `send_telegram_html()`)
  - Discord ops channel
- Schedule data lives in Editor Profiles as `Mon Schedule`…`Sun Schedule` (rich_text, format `HH:MM-HH:MM`, pipe-separated for multi-block days) + `Timezone` (rich_text, e.g. `PHT (UTC+8)`)
- `_parse_utc_offset(tz_str)` — extracts float offset from timezone string
- `_convert_schedule_to_est(raw, utc_offset)` — converts pipe-separated time blocks to EST
- `fetch_editor_schedule(editor_name)` — queries Editor Profiles and returns `{day: raw_str, timezone: str}`

## Revision Log
- Notion DB (`a05a523e-2489-45f4-ae69-4aaf3178aca7`) under VexxeFX Editing Ops — public
- Auto-logged on every `open_revision_assignment()` call via `log_revision_to_notion()` (fire-and-forget executor)
- Captures: Folder Name, Editor (select), Creator, Revision Notes, Date (UTC), Video Count, Raw Footage Folder (URL), Edited Folder (URL), Client Folder (URL), Active Queue Link (URL)
- Drive folder links always resolved: cache-warm path uses `_client_root_folder_cache` + `_client_edited_folder_cache`; cold-cache path calls `_find_edited_folder_top_down()` directly (top-down search, avoids broken `files.get(parents)` on Shared Drive)
- `REVISION_LOG_DB` constant in `discord_bot.py`

## Systemd Services
- `discord-bot` — runs `discord_bot.py`
- `notion-bridge` — runs `notion_bridge.py`
- `gdrive-watcher` — runs `gdrive_watcher.py` on a timer
- `drive-webhook` — runs `drive_webhook.py`
- `gdrive-dashboard` — runs `dashboard.py`
- `ngrok-webhook` — ngrok tunnel for Drive webhook

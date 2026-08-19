# CC Video Manager

Automated operations system for VexxeFX, managing the full video editing pipeline across 22+ clients and 5 editors. Connects Google Drive, Notion, Telegram, and Discord into a single unified workflow.

---

## What It Does

When a new raw footage folder appears in Google Drive, the system:

1. **Detects it** via Drive push webhooks + periodic scans
2. **Notifies Vex** on Telegram with folder info and video count
3. **Vex assigns an editor** via Telegram inline buttons
4. **Editor is notified** in their private Discord channel with an embed
5. **Editor marks it complete** via `/complete` Discord slash command — submits folder name + video count
6. **Bot verifies against Drive** — confirms the edited folder exists and counts match
7. **Review is sent to Vex** on Telegram with flags for any mismatches
8. **Vex approves or adjusts** — Notion is updated, editor stats increment, delivery history is logged

---

## Architecture

```
Google Drive
    │
    ├── Push Webhook ──► drive_webhook.py (port 8081)
    │                         │
    │                         └── triggers gdrive_watcher.py
    │
    └── Periodic Scan ──► gdrive_watcher.py
                               │
                               └── new folder detected
                                        │
                                        ▼
                               notion_bridge.py (Telegram bot)
                               ├── Notifies Vex
                               ├── Vex assigns editor → writes to discord_queue.json
                               └── Handles review approvals
                                        │
                                        ▼
                               discord_bot.py (Discord bot)
                               ├── Reads discord_queue.json every 3s
                               ├── Posts assignment embed to editor channel
                               ├── /complete → verifies against Drive → sends review to Telegram
                               └── /stats, /editorstats
```

---

## Services

| File | Role | Port |
|------|------|------|
| `notion_bridge.py` | Telegram bot for Vex — assignments, review, reminders | — |
| `discord_bot.py` | Discord bot for editors — `/complete`, `/stats`, `/editorstats` | — |
| `gdrive_watcher.py` | Scans Drive for new folders and video count changes | — |
| `drive_webhook.py` | Flask server receiving Google Drive push notifications | 8081 |
| `dashboard.py` | Flask ops dashboard | 8080 |
| `register_watch.py` | Registers & renews Drive changes.watch channel (every 23 hrs) | — |
| `daily_summary.py` | Sends daily summary to Telegram at 11 PM IST | — |
| `unassigned_reminder.py` | Pings Vex for folders unassigned 5+ hours (runs hourly via cron) | — |
| `query_stats.py` | CLI tool for querying editor stats | — |
| `reset_weekly.py` | Resets weekly editor stats | — |
| `reset_monthly.py` | Resets monthly editor stats | — |

---

## Discord Bot Commands

| Command | Who | What it does |
|---------|-----|-------------|
| `/complete` | Editor | Mark assignment done. Prompts for edited folder name + video count. Bot verifies against Drive and sends review to Vex on Telegram. |
| `/stats` | Editor / Creator | Shows your video stats — delivered today, this week, this month, all time. Includes in-progress assignments. |
| `/editorstats` | Vex | Full stats breakdown for all editors with current load vs capacity. |

---

## Telegram Bot (notion_bridge.py)

Vex interacts with this bot to manage the entire assignment and review flow.

**Assignment flow:**
- New folder detected → bot sends message with client name, folder name, video count, Drive link
- Inline buttons to assign to any available editor
- If editor is over capacity, shows a warning before confirming

**Review flow (after editor submits `/complete`):**
- Bot sends review message with:
  - Editor name, client, folder
  - Video count (editor-reported vs Drive-verified)
  - Link to Client Folder and Edited subfolder on Drive
  - Files found in the Edited folder
  - Flags for count mismatches or folder name mismatches
- Inline **Review** button → opens count confirmation if mismatch, else directly approves
- On approval: updates Notion Active Queue status → Delivered, increments editor stats, logs to Delivery History

**Other Telegram commands:**
- `/load` — shows current editor loads
- `/pending` — shows pending unassigned folders

---

## Notion Databases

| Database | ID | Purpose |
|----------|----|---------|
| Active Queue | `44593fbf-4276-47f0-bd12-27289dcb78fd` | All current assignments (Raw → In Progress → Delivered) |
| Editor Profiles | `a18d5c16-f359-4a2b-a620-6c837aa04232` | Editor info, Discord channel/user IDs, capacity, stats |
| Creator Assignments | `cead1699-21dc-4b0c-b0b6-00cf31c5fa29` | Creator ↔ Discord channel mapping |
| Delivery History | `733883073ccf48f2a83953ba2d5ad36d` | Permanent log of all completed deliveries |

---

## Google Drive Structure

```
In-House Editor/  (root, ID: 1hKXUhKZZo1WN-B5h309CEiSgZbogUoum)
├── ClientName/
│   ├── Raw Footage/
│   │   └── FolderName/   ← folder_id stored in Notion
│   └── Edited/
│       └── FolderName/   ← verified on /complete
├── AnotherClient/
│   └── ...
```

> **Important:** Drive API `files.get(fields='parents')` returns empty on this Shared Drive. All folder lookups must use the **top-down** approach (`files.list` from root → client → Edited/) rather than walking up via parents. See `_find_edited_folder_top_down()` in `discord_bot.py`.

---

## IPC Between Services

`discord_bot.py` and `notion_bridge.py` communicate via JSON files on disk (with file locks):

| File | Purpose |
|------|---------|
| `discord_queue.json` | notion_bridge writes assignment jobs; discord_bot polls every 3s |
| `pending_reviews.json` | discord_bot writes review data; notion_bridge reads on Telegram callback |
| `pending_folders.json` | Tracks folders awaiting assignment decision |
| `assignment_messages.json` | Maps folder_id → Discord message_id for embed updates |

---

## Config (`config.json`)

```json
{
  "telegram_token": "...",
  "chat_id": "...",
  "notion_bridge_token": "...",
  "notion_bridge_chat_id": "...",
  "notion_token": "...",
  "discord_bot_token": "...",
  "discord_guild_id": "...",
  "creator_guild_id": "...",
  "root_folder_name": "In-House Editor",
  "poll_interval_minutes": 5,

  "dashboard_url":          "https://www.trycreatorcollective.com/api/discord/editing-assign",
  "dashboard_commands_url": "https://www.trycreatorcollective.com/api/discord/editing-commands",
  "dashboard_status_url":   "https://www.trycreatorcollective.com/api/discord/editing-status",
  "dashboard_secret":       "EDITING_BRIDGE_SECRET from the website's Vercel project"
}
```

---

## Creator Collective dashboard bridge

Optional and inert without the four `dashboard_*` keys above — leave them out
and the bot behaves exactly as it did before.

| Direction | Path | When |
|-----------|------|------|
| bot → site | `dashboard_url` | every assignment, from `assign_folder` — the one choke point Telegram, `/assign` and the ops dashboard all funnel through |
| bot → site | `dashboard_status_url` | a batch is delivered (`finalize_delivery`, or `_finalize_va_approval` for premium) or a revision round opens |
| site → bot | `dashboard_commands_url` | polled every 30s by `dashboard_commands_loop` — assignments, revisions and approvals made in the website UI |

Assignments keep working from Telegram exactly as before; the website is a
second entry point into the same path, not a replacement.

**Notes**

- Everything keys on the Drive folder id. Tickets created natively in the
  dashboard have no folder, so they arrive as `notify` — the editor gets the
  embed in their channel with a link back to the dashboard, and picks up files
  and delivers there. No Notion row, no deadline entry, no `/complete` (there's
  nothing in Drive to verify against).
- Editor names are resolved with `resolve_editor_name()` — exact, then
  whitespace/case-insensitive, then punctuation-insensitive, and only ever when
  it lands on exactly one editor. A name that resolves to nobody posts an ops
  warning instead of silently vanishing.
- Failed pushes park in `pending_dashboard_pushes.json` and retry on the next
  poll. A 404/422 is a data problem (name mismatch), logged and dropped.
- Commands arriving from the website carry `from_dashboard`, which suppresses
  the push back out — otherwise every dashboard assignment would echo as a
  redundant reassign.
- Run `reconcile_dashboard_names.py` before enabling: it prints which Notion
  editor and client names have no dashboard profile. An unmatched *creator*
  blocks mirroring entirely.

---

## Setup

### Requirements
```bash
pip install discord.py python-telegram-bot requests google-api-python-client \
            google-auth-oauthlib filelock flask
```

### Google Drive Auth
```bash
python3 reauth.py                                       # OOB flow -> token.json
railway variables --set "CC_TOKEN_JSON=$(cat token.json)"
```
`register_watch.py` then re-registers the Drive push channel at boot, pointed
at `DRIVE_WEBHOOK_URL`. The OAuth client is still in **testing** in Google
Cloud, so refresh tokens expire after 7 days; publishing it stops that.

### Running it (Railway)
The Ubuntu box is gone as of 2026-08-19 — no systemd, no ssh, no ngrok. This
deploys to Railway (project `harmonious-charisma`, service `worker`) on merge
to `main`.

```bash
railway logs --service worker     # what it's doing
railway redeploy                  # restart
railway variables                 # CC_CONFIG_JSON, CC_TOKEN_JSON, CC_CREDENTIALS_JSON
```

`railway_boot.py` is the entrypoint: writes the secrets from those env vars,
symlinks the state files onto the `/data` volume, starts `drive_webhook.py`
(`CC_RUN_WEBHOOK=1`) and `cron_runner.py` (`CC_RUN_CRONS=1`), then runs the
bot. Locally, run any of them directly — an existing `config.json` always wins
over the env vars, and the crons stay off unless you ask for them.

`notion_bridge.py` and `dashboard.py` had their own units on the box and are
not running anywhere yet.

### Cron Jobs
They're `cron_runner.py`'s `JOBS` table now, not a crontab — same schedules,
running inside the bot's container so they share its state files:

| job | when (UTC) |
| --- | --- |
| `unassigned_reminder.py` | hourly |
| `snapshot_editor_state.py` | hourly |
| `health_monitor.py` | every 30 min |
| `refresh_schedule_cache.py` | every 2h, and at boot |
| `daily_digest.py`, `cantina_daily_reminder.py` | 03:30 |
| `daily_status_update.py` | 17:30 |
| `sanity_checker.py` | 20:00 |
| `weekly_leaderboard_post.py` | Sat 15:30 |
| `reset_weekly.py` | Sun 00:00 |
| `reset_monthly.py` | 1st, 18:30 |
| `register_watch.py` | 02:00, and at boot |

---

## Key Implementation Notes

- **Drive top-down search:** `files.get(parents)` returns `[]` on this Shared Drive. Use `_find_edited_folder_top_down()` which searches `root → client → Edited/` using `files.list`.
- **All Drive list calls** must include `supportsAllDrives=True` and `includeItemsFromAllDrives=True`.
- **OAuth scope** must be `drive` (not `drive.readonly`) — watcher needs to register push channels.
- **Editor/client names** are never hardcoded — always fetched from Notion at runtime.
- **Fuzzy folder matching** in `/complete`: tries exact → whitespace-stripped → substring containment, so minor typos in folder names still resolve correctly.
- **Video count verification** is done live against the actual files in Drive, not from any cache.

---

## Files Not in Git (gitignored)

| File | Why |
|------|-----|
| `config.json` | Contains all API tokens and secrets |
| `token.json` | Google OAuth token |
| `credentials.json` | Google OAuth client credentials |
| `*.log` | Runtime logs |
| `watched_files.json`, `pending_*.json`, etc. | Runtime state — regenerated automatically |

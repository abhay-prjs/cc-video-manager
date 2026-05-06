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
  "poll_interval_minutes": 5
}
```

---

## Setup

### Requirements
```bash
pip install discord.py python-telegram-bot requests google-api-python-client \
            google-auth-oauthlib filelock flask
```

### Google Drive Auth
```bash
python3 register_watch.py
```
This will open OAuth flow, save `token.json`, and register a Drive push notification channel.

### Run All Services (systemd)
Each service has a corresponding systemd unit. Example:
```bash
sudo systemctl start discord-bot
sudo systemctl start notion-bridge
sudo systemctl enable discord-bot notion-bridge
```

### Cron Jobs
```bash
# Daily summary at 11 PM IST (5:30 PM UTC)
30 17 * * * python3 /home/ubuntu/gdrive_watcher/daily_summary.py

# Unassigned reminder every hour
0 * * * * python3 /home/ubuntu/gdrive_watcher/unassigned_reminder.py

# Weekly stats reset (Monday midnight IST)
30 18 * * 0 python3 /home/ubuntu/gdrive_watcher/reset_weekly.py

# Monthly stats reset (1st of month midnight IST)
30 18 1 * * python3 /home/ubuntu/gdrive_watcher/reset_monthly.py
```

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

# CC Video Manager

Automated operations system for VexxeFX, managing the full video editing pipeline across 22+ clients and 5 editors. Connects Google Drive, Notion, Telegram, and Discord into a single unified workflow.

---

## What It Does

A batch reaches the team from the Creator Collective dashboard. (A Drive
folder used to be the other way in — detected by a watcher, announced on
Telegram and Discord. That whole intake was removed on 2026-09-03; see
"Drive intake is gone" in CLAUDE.md.) From there:

1. **An editor is assigned** — by the dashboard, or by Vex from the site or `/assign` in Discord
2. **Editor is notified** in their private Discord channel with an embed
3. **Editor marks it complete** via `/complete` Discord slash command — submits folder name + video count
4. **Bot verifies against Drive** — confirms the edited folder exists and counts match
5. **Review is sent to Vex** with flags for any mismatches
6. **Vex approves or adjusts** — Notion is updated, editor stats increment, delivery history is logged, the dashboard is told

---

## Architecture

```
Creator Collective dashboard (trycreatorcollective.com)
    │
    ├── editing-commands outbox ──► discord_bot.py polls every 30s
    │                                (assign / reassign / revision / approve /
    │                                 deliver / message / ops_alert)
    │
    ◄── editing-assign / editing-status ── discord_bot.py mirrors back
                                            (every assignment, every delivery)

discord_bot.py (Discord bot, Railway)
    ├── Posts assignment embed to editor channel
    ├── /complete → verifies against Drive → review card
    ├── /stats, /editorstats, /leaderboard, /assign, /reassign
    └── cron_runner.py alongside it: digests, resets, snapshots

notion_bridge.py (Telegram bot) — not running anywhere since the box died
    └── writes discord_queue.json, which discord_bot.py still reads every 3s
```

---

## Services

| File | Role | Port |
|------|------|------|
| `notion_bridge.py` | Telegram bot for Vex — assignments, review, reminders | — |
| `discord_bot.py` | Discord bot for editors — `/complete`, `/stats`, `/editorstats` | — |
| `dashboard.py` | Flask ops dashboard | 8080 |
| `daily_summary.py` | Sends daily summary to Telegram at 11 PM IST | — |
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
- (The new-folder message that used to start this went with Drive detection on 2026-09-03. The buttons below only exist on messages already sent.)
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
  "dashboard_ops_action_url": "https://www.trycreatorcollective.com/api/discord/editing-ops-action",
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
| bot → site | `dashboard_ops_action_url` | somebody pressed a button on an ops alert card in #assignments |

Assignments keep working from Telegram exactly as before; the website is a
second entry point into the same path, not a replacement.

### Ops alerts in #assignments

The channel used to get one assign card per batch. Auto-assign had already
routed the batch by the time the card landed, so nobody acted on them and it
read as a log. It carries **ops alerts** now: only things that need a person.

The site raises them (`editing_ops_alerts`) and pushes each one as an
`ops_alert` command. `handle_cc_dashboard_ops_alert` posts a card in
#assignments and **edits it in place** on every later push for the same
`alert_id` — a sweep that keeps finding the same overdue batch updates one card
instead of stacking twelve. A `decision` pings Vex; a `caution` or `request`
waits to be read.

| severity | means | example |
|---|---|---|
| `decision` | work is blocked until someone chooses | every editor is over their point cap |
| `caution` | something is going wrong | a batch is 6h over its 24h clock |
| `request` | someone needs to do a small thing | a creator has no `-edits` channel linked |

Buttons come from the payload's `actions` list — the site decides what exists
and what each one does. A press goes to `dashboard_ops_action_url`; a `200`
applies it and settles the card, a `422` is final and its message goes back to
the presser ephemerally with the buttons left live (`couldn't route it,
everyone is still full` stops being true later). Views are re-registered in
`on_ready` from `pending_ops_alerts.json`, so buttons survive a redeploy.

**This is gated on the site side.** `editing_settings.ops_alerts_enabled`
defaults off and must stay off until this branch is live on the box — the
command loop re-posts any kind it doesn't recognise as a real ASSIGN, so
flipping it early hands editors folders that don't exist.

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
The token is only used to read Drive now (`/complete` verification, folder
links) — nothing registers push channels any more. The OAuth client is still
in **testing** in Google Cloud, so refresh tokens expire after 7 days;
publishing it stops that.

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
symlinks the state files onto the `/data` volume, starts `cron_runner.py`
(`CC_RUN_CRONS=1`), then runs the bot. Locally, run any of them directly — an existing `config.json` always wins
over the env vars, and the crons stay off unless you ask for them.

`notion_bridge.py` and `dashboard.py` had their own units on the box and are
not running anywhere yet.

### Cron Jobs
They're `cron_runner.py`'s `JOBS` table now, not a crontab — same schedules,
running inside the bot's container so they share its state files:

| job | when (UTC) |
| --- | --- |
| `snapshot_editor_state.py` | hourly |
| `refresh_schedule_cache.py` | every 2h, and at boot |
| `daily_digest.py`, `cantina_daily_reminder.py` | 03:30 |
| `daily_status_update.py` | 17:30 |
| `sanity_checker.py` | 20:00 |
| `weekly_leaderboard_post.py` | Sat 15:30 |
| `reset_weekly.py` | Sun 00:00 |
| `reset_monthly.py` | 1st, 18:30 |

---

## Key Implementation Notes

- **Drive top-down search:** `files.get(parents)` returns `[]` on this Shared Drive. Use `_find_edited_folder_top_down()` which searches `root → client → Edited/` using `files.list`.
- **All Drive list calls** must include `supportsAllDrives=True` and `includeItemsFromAllDrives=True`.
- **OAuth scope** is `drive` — the tokens in use were minted with it and nothing registers push channels any more, but keep `reauth.py` and the bot on the same scope so a re-auth does not invalidate anything.
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
| `pending_*.json`, `dashboard_batches.json`, etc. | Runtime state — regenerated automatically |

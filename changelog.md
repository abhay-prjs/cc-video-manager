# Changelog

## 2026-08-26 — ops alert cards survive a redeploy

### Fix
- `pending_ops_alerts.json` was missing from `railway_boot.py`'s `STATE_FILES`, so it wasn't symlinked to the volume and got wiped on every deploy. `on_ready` then found nothing to re-register and every button in #assignments went dead — the exact failure the re-registration was written to prevent, undone by a one-line omission in the file right next to it.
- Its sibling `pending_ops_assigns.json` was already listed, which is what makes the gap obvious in hindsight. Any new gitignored runtime json needs adding there or it silently resets.

## 2026-08-26 — #assignments becomes the channel where things need a person

### Feature
- The channel carried one assign card per batch. Auto-assign had already routed the batch by the time the card landed, so nobody acted on any of them and it read as a log (founder: "assignments come in there for no reason lmao"). It carries ops alerts now — only things that need a person.
- New `ops_alert` command kind. The site raises alerts (`editing_ops_alerts`) and pushes each one; `handle_cc_dashboard_ops_alert` posts a card and **edits it in place** on every later push for the same `alert_id`. A sweep that keeps finding the same overdue batch updates one card instead of stacking twelve.
- Three severities: `decision` (work is blocked until someone chooses — pings Vex), `caution` (something is going wrong), `request` (someone needs to do a small thing). Five kinds so far: capacity_blocked, overdue, stranded, no_route, undelivered.
- Buttons come from the payload's `actions` list, so the site owns what exists and what each one does. New alerts and new buttons land here as new payloads, never as new handlers.
- `post_dashboard_ops_action` sends a press to `dashboard_ops_action_url`. A `200` applies it and settles the card immediately (the person is looking at it — waiting for the site's own push round the 30s poll reads as broken). A `422` is final and its message goes back ephemerally with the buttons left live, because "couldn't route it, everyone is still full" stops being true later.
- Views re-register in `on_ready` from `pending_ops_alerts.json`, so buttons survive a redeploy. Its own store, not `pending_ops_assigns.json` — `retract_pending_assign_cards` sweeps that by ticket_id and would take ops cards down with it.

### Config
- New key `dashboard_ops_action_url`. Absent, presses no-op with a message saying so — same inert-without-config rule as the rest of the bridge.

### Deploy
- Website half: https://github.com/Creator-Collective/trycreatorcollective-website/pull/1743 (merged, includes a migration that has already applied).
- That side is gated OFF (`editing_settings.ops_alerts_enabled`). Merge and restart here FIRST, then flip the switch on `/dashboard/admin/editing` — the command loop re-posts any unknown kind as a real ASSIGN, so flipping it early hands editors folders that don't exist.

## 2026-08-20 — An ops post survives a rate limit

### Fix
- `send_discord_ops_channel` treated a 429 like a 403: log it, return False, done. Discord hands back the exact wait in `retry_after` and it was thrown away, so a boot burst (the provision report and escalations landing together) lost an ops post to a 0.3s rate limit at 13:10 UTC.
- Now retries: up to 3 attempts, honouring `retry_after` on a 429 and backing off 0.5s on a 5xx or a connection error. 403/404/bad-payload stay terminal — they will fail again just as hard.
- The retry budget is deliberately small (`OPS_POST_ATTEMPTS`/`OPS_POST_BACKOFF`/`OPS_POST_MAX_WAIT`). This is a blocking call made from inside the event loop, and a rate limit asking for longer than 2s is refused outright rather than held — blocking the loop for seconds is exactly how `/stats` started failing with "Unknown interaction".
- Callers that care already treat False as "hold it, don't drop it" (`_dashboard_message_failed`), so giving up stays safe.

## 2026-08-20 — A redeploy is not a crash

### Fix
- Railway mailed "Deploy Crashed!" on every single redeploy. Nothing was crashing: Railway stops a container with SIGTERM, Python's default action for SIGTERM kills the process with status 143, and Railway reads any non-zero exit as a crash. The 12:52 UTC deploy of #53 is the one that prompted this, but the previous deployment's log ends in a clean `Stopping Container` with no traceback, and every deploy before it did the same.
- The real damage was quieter than the email: the default SIGTERM action skips `atexit`, so `cron_runner` and `drive_webhook` were never terminated by the shim — they died with the container instead, mid-write to a state file on the volume if the timing was unlucky.
- `railway_boot.py` now installs a SIGTERM handler that raises `KeyboardInterrupt`, which is the one shutdown signal the whole stack already understands: discord.py closes the gateway on it, `atexit` fires and takes the children down, and the process exits 0. Verified standalone: exit code 0 and the child reaped, where before it was 143 and the child leaked.
- The redeploy that lands this one will still send the email, since the container being stopped is the old one. Every deploy after it should be quiet.

## 2026-08-20 — Only Railway may log this bot in

### Fix
- `/stats` was failing with `404 10062 Unknown interaction` on the `defer()` itself (2026-08-19 14:41). Nothing wrong with the command: a laptop copy of the bot was logged in on the SAME token as the Railway service, so both received every interaction and the one that lost the 3-second ack race raised. The same double-login also double-consumes the dashboard outbox — that's the duplicate "Revisions on Chiara Votta's Phrasly batch" pings 30 s apart in the same log.
- `main()` now calls `refuse_duplicate_login()` before `bot.run()`. Off Railway (no `RAILWAY_ENVIRONMENT` / `RAILWAY_ENVIRONMENT_NAME` / `RAILWAY_SERVICE_ID` / `RAILWAY_PROJECT_ID` in the env) it exits with an explanation instead of connecting.
- A local run needs its own Discord application + token in `config.json` and `CC_ALLOW_LOCAL_BOT=1`. Setting that flag with the production token reproduces the exact bug, so the override logs a warning every start.

### Not fixed here
- No companion PR needed on the website — this is entirely bot-side.
- The other services (`drive_webhook.py`, `cron_runner.py`) have no such guard; they don't hold a gateway session, so they can't steal interactions, but a second `cron_runner` would still double-post.

## 2026-08-20 — Take the ack back when a message reached nobody

### Feature
- `report_dashboard_undelivered(command_id, reason)` POSTs `{undelivered:[{id, reason}]}` to `dashboard_commands_url`. We ack a command when it lands in `discord_queue.json`, not when it's delivered, so `sent` on the site has never meant a person was told — this is the correction, and it's the only signal the site can get.
- Sent from `_dashboard_message_failed` once the retries are spent AND the ops channel has the message, never before: a dashboard blip must not be what stops the escalation. The queued item carries `command_id` for it.
- Site half: https://github.com/Creator-Collective/trycreatorcollective-website/pull/1499 flips the row to `undelivered` with the reason, notes it on the ticket timeline, and emails a creator who was the target. It carries a migration.

## 2026-08-20 — An undeliverable dashboard message goes to a human, not the void

### Fix
- `handle_cc_dashboard_message` used to `return` on a warning when it couldn't resolve a channel. The dashboard acks a `message` command as soon as it lands in `discord_queue.json`, long before delivery, so the site reads `sent` regardless — a creator with a stale channel id, or one we have no route to at all, got nothing while every system involved said otherwise. That is how the footage report on Kio's "Zo Computer" batch reached nobody on 2026-08-19.
- Undeliverable now means retry, then escalate: the item raises back into the queue loop (which re-appends it, ~3 s cadence) up to `MAX_MESSAGE_DELIVERY_ATTEMPTS` = 10, then the whole message — who it was for, why it failed, title, body, dashboard link — is posted to the ops channel for someone to deliver by hand. The attempt counter rides on the queued item, so it survives a redeploy.
- A `ch.send()` that threw (403 in the channel, deleted channel) already propagated into the queue loop, which requeued it forever with no bound and no alert. It now runs through the same bounded path.
- `send_discord_ops_channel` returns True/False and checks the status code — it never looked at the response before, so a 403 on the ops channel read exactly like a success. The escalation is the last copy of a lost message, so it holds the item rather than dropping it when that post fails too.

### Not fixed here
- The site's `editing_bot_commands` row still reads `status=sent` for a message that ended up in the ops channel; there is no endpoint to tell it otherwise. Site-side change if it's worth having.

## 2026-07-25 — Reverse bridge: dashboard assignments post in Discord

### Feature
- New `dashboard_commands_loop`: polls the CC dashboard every 30 s (`dashboard_commands_url` + `dashboard_secret` in config.json; silent no-op without them) for editor assignments made in the dashboard UI, and feeds them through the normal `discord_queue.json` → `assign_folder` path — identical embed, Start button, deadline state, and ops mirror as a Telegram assignment.
- Commands ack back to the dashboard only after they're safely queued (bot crash mid-cycle retries rather than losing the assignment). Editor names not found in the Notion editor list are dropped with an ops-channel warning instead of poisoning the queue. Guarded against on_ready refires so only one poller ever runs.


## 2026-07-25 — Dashboard bridge: assignments mirror into the CC dashboard

### Feature
- Every assignment (fresh or reassign, all paths funnel through `assign_folder`) now also POSTs to the Creator Collective dashboard, which creates/updates an editing ticket for that editor from that creator. Discord pings are unchanged — the dashboard is a mirror, not a second notifier.
- Best-effort by design: no-op unless `dashboard_url` + `dashboard_secret` exist in config.json; failures only log a warning and never block the Discord flow. Payload: folder id/name, creator, editor (+ discord id), video count, drive links, project #, reassign flag.


## 2026-07-25 — /unstart: undo a misclicked Start (Team only)

### Feature
- New Team-only `/unstart` command, run in an editor's channel: reverts a started folder to the pending-start state via `reset_start_state` — deadline cleared, pickup flags reset, ▶️ Start button + pending copy restored on the assignment message (re-pinned). `assigned_at` is preserved so pickup tracking stays honest.
- Ops channel gets an "↩️ Start Undone" audit embed (editor / folder / who undid it). Idempotent: un-started folders report nothing-to-undo. Listed in the Team section of /help.


## 2026-05-14 — Fix stale pending folders in /stats (creator Discord)

### Bug: old delivered folders showing as "awaiting assignment"

`/stats` in creator Discord channels was showing already-delivered folders in
the **⏳ Pending** section. Two root causes:

1. **`pending_assignments.json` entries were never marked `assigned`** for folders
   created before the `status` field was introduced. 54 entries were backfilled
   to `"status": "assigned"`.

2. **No cross-check against Active Queue**: `fetch_pending_assignments_for_creator`
   only skipped entries with `status == "assigned"` but never verified against
   Notion. A folder could be fully delivered in Active Queue yet still appear as
   pending if its file entry was stale.

### Fix (`discord_bot.py`)

- `fetch_pending_assignments_for_creator` now returns `folder_id` in each row.
- After fetching `queue_rows` and `pending_rows` in `/stats`, any pending entry
  whose `folder_id` already appears in Active Queue (any status — Raw, In Progress,
  or Delivered) is filtered out before rendering the embed.

This permanently prevents delivered or assigned folders from ghosting in the
Pending section regardless of file state.

---

## 2026-05-14 — Schedule-aware recommendations, auto-assign, editor notes

### 1. Editor Schedules (Notion DB)

Availability is now read from a dedicated **Editor Schedules** Notion database
(`a02419d2`) instead of any columns on Editor Profiles.

Each row = one editor × one day with `Start EDT`, `End EDT` (text, 24h, supports
`>24:00` notation e.g. `26:00` = 2 AM next day), and an `Available` checkbox.
Overnight shifts (e.g. `22:00–26:00`) and cross-midnight rows from the previous
day are both handled correctly.

Manage schedules directly from Telegram:

| Command | What it does |
|---|---|
| `/schedule` | View all editors' schedules |
| `/schedule Karlo` | View one editor's schedule |
| `/setschedule Karlo Monday 09:00-23:00` | Set hours for a day (upserts Notion row) |
| `/markoff Karlo Saturday` | Mark unavailable for a day (Available = false) |

---

### 2. Recommendation engine (`/recommend`)

`/recommend` ranks all editors for the next assignment:

- **Available now** editors rank above offline ones
- Tie-broken by lowest load ratio (active ÷ capacity)
- Further tie-broken by soonest next shift start

Example output:
```
1. 🟢 Karlo — 42% load, available now  ← pick this
2. 🟡 Maya — 30% load, available in 2h 15m
3. 🔴 Naomi — 80% load, available now
```

The same ranking is used internally to pick the suggested editor shown in every
new-folder notification (replaces the old load-only min sort).

---

### 3. Auto-assign (`/autoassign on|off`)

When enabled, new folders are assigned automatically — no button tap needed.

- Picks the top-ranked editor from the recommendation engine
- Creates/updates the Notion Active Queue row to In Progress
- Pings the editor on Discord (same embed as manual assignment)
- Sends Vex a Telegram confirmation with a single **↩️ Override** button
- Tapping Override shows the editor selection keyboard to swap if needed
- 24h deadline clock is set the same as in manual mode

Toggle with `/autoassign on` or `/autoassign off`. State persists in
`autoassign.json`. Starts OFF by default.

---

### 4. Editor notes (`/note`)

Send a short announcement to editors' Discord channels directly from Telegram.

```
/note Please wrap up all open folders by EOD     ← sent to everyone
/note Karlo Can you prioritise the Julia folder  ← sent only to Karlo
```

If the first word matches a known editor name (case-insensitive), the note goes
to that editor only. Otherwise it broadcasts to all active editors.

Message appears in each editor's Discord channel as:
```
📢 Note from Vex:
<your message>
```

---

## 2026-05-06 — Fix: find_edited_folder_videos failing for all folders

### Problem
Every editor completion was failing with:
- Telegram review showed "Raw Footage Folder" link instead of "Client Folder"
- `find_edited_folder_videos()` returned `(None, [], None, None)` for every folder
- Log showed: `folder_id parents: []` then `Edited/ not found after walking up`

### Root Cause
The rewritten `find_edited_folder_videos` (added May 6 04:42) walked up the
folder hierarchy using `files.get(fileId=..., fields='parents', supportsAllDrives=True)`.
On this Google Shared Drive, that API call always returns `parents: []` — the
Shared Drive does not expose parent chains via `files.get`. This silently broke
the walk-up logic for 100% of folders.

Confirmed in journal logs:
```
2026-05-06 04:55:15 find_edited_folder_videos: client='Julia', raw_folder_id='1xHg5OvhPgLS6zqJj5ry1h9-2VTvnF8yu'
2026-05-06 04:55:16 folder_id parents: []
2026-05-06 04:55:16 [depth=0] no parents found — stopping walk
2026-05-06 04:55:16 find_edited_folder_videos: Edited/ not found after walking up
2026-05-06 04:55:16 Could not resolve client root folder for folder_id=1xHg5OvhPgLS6zqJj5ry1h9-2VTvnF8yu
```

The old code (May 4–5) used a different approach that did NOT call `files.get(parents)`,
which is why it worked. Confirmed in old logs:
```
2026-05-05 00:48:41 Drive Edited/ subfolders found: ['May 4 liftoff', 'Candle skits 2', ...]
2026-05-05 00:48:41 Searching for: 'May 4 Liftoff'
2026-05-05 00:48:41 Match result: FOUND
```

### Fix (discord_bot.py)

1. **New helper `_find_edited_folder_top_down(service, client_name)`**
   - Searches top-down: `config['root_folder_name']` → client folder → `Edited/`
   - Uses `files.list` queries (work on Shared Drives, unlike `files.get(parents)`)
   - Populates `_client_root_folder_cache[client_name]` on success

2. **`find_edited_folder_videos`** refactored:
   - Primary: calls `_find_edited_folder_top_down` (fixes the broken walk-up)
   - Fallback: original walk-up via parents (kept for edge cases)
   - Because top-down populates `_client_root_folder_cache`, the "Client Folder"
     link now resolves correctly in the Telegram review message

3. **`get_client_root_folder_id`** updated:
   - Now accepts optional `client_name` parameter
   - Uses top-down as primary, walk-up as fallback

4. **Call site updated** (`CompleteModal.on_submit`):
   - Passes `client_name` to `get_client_root_folder_id`
   - Removed `folder_id` guard (client_name lookup doesn't need a folder_id)

5. **Log messages restored** to match original format:
   - `"Drive Edited/ subfolders found: [...]"`
   - `"Match result: FOUND / NOT FOUND"`

# CC Video Manager — Codebase Guide

## TODO
- [ ] Verify the `/stats` website-batches fix (2026-08-05, commit `6e37e07`) actually shows a `🌐 Website Batches` field for editors with active dashboard tickets — check on real Discord once the bot has restarted with the new code.
- [ ] `trycreatorcollective-website` repo needs to start sending a `kind: 'delivered'` command (with `ticket_id`, `editor_name`/`editor_discord_id`, `video_count`) on its outbound dashboard-commands feed — until then, website batches show correctly as *active* in `/stats` but delivered counts never move. See "Website-native batches in `/stats`" under Creator Collective Dashboard Bridge below.

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
- `daily_status_update.py` — Cron 17:30 UTC: posts the daily ops status update (replaces the old `daily_summary.py`, moved to `test/` as dead code — see below)
- `unassigned_reminder.py` — Pings Vex for folders unassigned 5+ hours
- `reset_weekly.py` / `reset_monthly.py` — Resets editor stats on schedule
- `daily_digest.py` — Cron 03:30 UTC (9AM IST): posts "needs your attention" digest to ops channel (reviews pending >24h, overdue folders, unassigned Raw)
- `sanity_checker.py` — Cron 20:00 UTC nightly: consistency audit (archived profiles with active folders, duplicate Delivery History rows, In Progress w/o Editor, deadlines.json drift, week>month counter anomalies); alerts ops channel only when issues found
- `snapshot_editor_state.py` — Cron hourly: appends a timestamped snapshot of every editor's In Progress/Review/Revision folders to `editor_state_history.jsonl` (append-only, one JSON line per run). Exists because Delivery History and `delivery_meta.json` only capture state at completion time — there was no way to answer "what was in an editor's queue during their lowest-delivery week" after the fact. Added 2026-07-22.
- `cantina_daily_reminder.py` — Cron 03:30 UTC
- `refresh_schedule_cache.py` — Cron every 2h: refreshes `schedule_cache.json` from Editor Profiles
- `ai_ops.py` — shared module (not run directly), see AI Ops Assistant section below
- `logger_setup.py` — shared logging config module (not run directly)

## `test/` — One-off Scripts and Manual Tests

**New files in `test/` are gitignored (`test/*`); the 45 already committed stay committed.** Ignore rules only decide whether git starts tracking something NEW, so an ignore pattern over a folder of already-tracked files is safe and changes nothing for them.

**Do not "clean this up" by untracking the existing ones.** That was tried on 2026-08-16 and reverted. `git rm --cached` does not untrack in any shared sense — it records a DELETION that every other clone applies on pull. It wiped all 45 off the dev machine and would have done the same to the bot box, taking the recovery scripts (`restore_ops_assign_msgs`, `rollback_bulk_assign`) with them. If files genuinely must leave a repo, copy them somewhere outside it FIRST; the backup is the safety net, not the git history.

Write new one-offs here as before — they just won't be committed. A script only graduates to the repo root (next to the services) if it's genuinely worth re-running, with a comment saying what it's for and what runs it.
Every script here is either a **one-time fix/migration** for a specific past incident (e.g. `fix_naomi_stats.py`, `restore_editors_active.py`, `sync_project_numbers.py`) or a **manual diagnostic/test** (`diagnose_editor.py`, `test_complete_flow.py`, `gdrive_stats.py`, `reconcile_dashboard_names.py`, `drive_migrator.py`). None of these are wired into the service or the cron table — they're run by hand when needed.
- **Any new one-off script or manual test you write goes in `test/`, not the repo root.** The root is reserved for files actually wired into the Railway service or `cron_runner.py`'s `JOBS` table (see Services above) plus shared modules they import.
- Scripts in `test/` resolve `BASE_DIR` as the **parent** directory (`os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`) so `config.json`/`token.json`/state files still resolve correctly from one level down — follow that pattern in new scripts rather than assuming `test/` and the repo root are the same directory.
- `daily_summary.py` lives here as dead code — superseded by `daily_status_update.py`, kept only for reference.

## Notion Databases
- Active Queue: `44593fbf-4276-47f0-bd12-27289dcb78fd`
- Editor Profiles: `a18d5c16-f359-4a2b-a620-6c837aa04232`
- Creator Assignments: `cead1699-21dc-4b0c-b0b6-00cf31c5fa29`
- Delivery History: `733883073ccf48f2a83953ba2d5ad36d`
- Revision Log: `a05a523e-2489-45f4-ae69-4aaf3178aca7`

## Drive Root Folder
- Name: `In-House Editor`
- ID: `1hKXUhKZZo1WN-B5h309CEiSgZbogUoum`

## Editor Active Checkbox
- Editor Profiles has an `Active` checkbox — **unchecked editors are excluded from `fetch_editors_from_notion()` (discord_bot) and `get_editor_loads()` (notion_bridge)**: no assignments, pings, or folder-update notifications, but their rows/stats stay intact
- Notion checkboxes default to false — **a newly added editor is invisible to the bots until `Active` is checked**
- Danna and Karlo are unchecked (off team since 2026-06-28); Kaye is unchecked (off team since 2026-06-30)
- Editor Profiles also has `Capacity` (max concurrent videos) — off-team editors have `Capacity = 0` alongside unchecked `Active`
- **The assignable editor roster is Editor Profiles (`Active=YES` and `Capacity>0`) — never Active Queue's `Editor` select property.** That select field accumulates stale options from editors who left and were never cleaned out of the dropdown (e.g. Jied, Raim have Select options in Active Queue but no Editor Profiles row at all). "This editor currently has 0 in-progress folders" is not evidence they're on the team — always cross-check Editor Profiles before proposing an assignment plan.

## Completion Review Pipeline (added 2026-07-02)
- `/complete` **rejects duplicates**: if the folder already has a pending review or `Status=Delivered`, the editor gets an ephemeral "already submitted" message (a double `/complete` used to double-count stats + create duplicate Delivery History rows)
- **Wrong-folder flag**: on a name mismatch, the typed edited-folder name is compared against the editor's other In Progress assignments — a match adds a `🚨 Possible wrong folder` flag to the review
- `/reviews` (Team only) — lists all pending reviews with ages + flags, dropdown approves one at a time; uses the same `_approve_review()` path as the button
- `review_recheck_loop` (every 10 min, in discord_bot) — re-checks Drive for reviews whose flags are **all** count-mismatch/not-found; if Drive now has >= claimed videos, auto-approves and notes it in the completion channel; gives up after 6 attempts (`recheck_count` in pending_reviews.json). Name-mismatch/wrong-folder flags are never auto-cleared
- All approve paths (button, `/reviews`, dashboard) go through `_pop_pending_review()` first — pop-before-finalize makes double-approval impossible
- `deadline_checker` escalation: editor re-pinged at 12h overdue (`escalated_12h`), ops channel pinged at 24h and every 24h after (`last_vex_escalation_ts`); entries found Delivered during escalation are dropped
- **Recursive Edited-folder video scan (fixed 2026-08-13):** `find_edited_folder_videos()` used to count only the *direct children* of the matched submission subfolder — editors frequently organize a submission into further subfolders (e.g. a `sent/` folder), which silently undercounted and produced false count-mismatch flags. Real example: Zyon's "Composio 21" sat flagged for 3 days (editor said 13, old scan found ~1-3) because 13 of the videos were inside a `sent/` subfolder the scan never looked in. Fixed via `_find_videos_recursive()` (`EDITED_VIDEO_SCAN_MAX_DEPTH = 5`, same bounded-recursion pattern as `get_folder_video_tree()`/`fetch_folder_video_tree()`) — now used for the final video count after a submission subfolder is matched. The Step 1-4 *matching* logic (finding which subfolder of `Edited/` is the right one, including the one-level-deeper "Phrasly-style" group fallback) is unchanged — only the final count is now recursive.
- **Review channel split (added 2026-08-13):** flagged reviews now post to their own channel (`review_channel_id` in `config.json`, `REVIEW_CHANNEL_ID` constant — falls back to `COMPLETION_CHANNEL_ID` if unset) instead of sharing the completion channel with clean/unflagged deliveries. Clean completions still post to `COMPLETION_CHANNEL_ID`. This only changes where the initial flagged embed is sent — `review_recheck_loop`'s auto-approve FYI note still posts to `COMPLETION_CHANNEL_ID` since by that point the item is resolved/delivered, not something needing a decision.
- **Discrepancy decision (added 2026-08-13):** `DiscordReviewView` now has two buttons — `🔍 Approve & Finalize` (unchanged) and `⚠️ Flag Discrepancy`, which opens a modal (`DiscrepancyFeedbackModal`) for a free-text note, then routes the folder back to the editor through the **existing revision pipeline** (`open_revision_assignment()`) rather than a new mechanism: pops the pending review (clears the duplicate-submit guard so the editor can `/complete` again), flips Notion `Status` → `Revision`, increments the editor's `revisions` counter, logs to Revision Log, and sends a `🔄 Revision Request` embed with the feedback as `Revision Notes` to the editor's own channel. Since `open_revision_assignment()` already calls `post_dashboard_status(folder_id, 'revisions', ...)` (skipped only when `from_dashboard=True`), a discrepancy flagged this way is already reported to the Creator Collective website the same as any other revision — **no separate website-side change was needed for this.** (Website-native ticket completions are a wholly separate pipeline — see "Website-native batches in `/stats`" — and don't go through `/complete`/`DiscordReviewView` at all, so this discrepancy flow only applies to Drive/Notion folder-based completions.)

## ▶️ Start / Pickup Flow (added 2026-07-03)
- **New assignments have no deadline until the editor presses ▶️ Start** — `assign_folder()` calls `reset_start_state()`: `pending_start=True, started_at=None, due_ts=None`. On Start, `due_ts = started_at + 24h`. Entries without `pending_start` (pre-feature) behave exactly as before; no migration.
- Assignment embed carries a persistent `StartAssignmentView` (▶️ Start + ⚠️ Problem with footage buttons, `custom_id=start_folder_{fid}` / `footage_problem_{fid}`) and is **pinned on send, unpinned on Start**. Views re-registered in `on_ready` by custom_id (no message_id — also covers pickup-reminder messages).
- **Pickup ladder** in `deadline_checker()`: editor nag at 4h (`PICKUP_NAG_1_SECS`), stronger at 8h, ops ping at 12h + every 12h after (`last_pickup_ops_ts`). Editor nags are held while off-shift (`_editor_on_shift_now()` via `schedule_cache.json`; empty schedule = always available); ops pings fire regardless. Every nag carries its own Start button + jump link to the assignment message.
- `⚠️ Problem with footage` sets `footage_flagged=True` → pauses pickup nags entirely and posts the reason to ops. Clear the flag manually in `deadlines.json` (or reassign) to resume nags.
- **Start is idempotent** (`mark_folder_started()` returns None if not pending) and ownership-checked (clicker's channel editor must match entry's `editor_name`, Team exempt).
- On Start: assignment embed edited in place (In Progress + `<t:>` countdown), ops channel notified, **creator channel gets a "🎬 Editing has started" embed** (`handle_creator_start_notify()` — positive events only, never nags/delays, no delivery-time promise).
- **Auto-start backfill**: `/complete` on a never-started folder backfills `started_at = assigned_at` (in `CompleteModal.on_submit` so nags stop while review pending, and again defensively in `finalize_delivery`) — skipping Start gains nothing.
- `delivery_meta.json` now records `started_at`, `pickup_hours` (assigned→started), `edit_hours` (started→delivered) alongside `turnaround_hours`. `average_pickup_hours()` feeds `/stats` (Team performance field) and `/editorstats`.
- `/start` command (editor channel): dropdown of that editor's un-started folders; auto-starts if exactly one.
- `/extend` on a pending-start folder **exits the pending state** (Vex setting a deadline overrides the pickup flow); `reset_start_state()` preserves `indefinite` — Start on an indefinite folder stamps `started_at` but leaves `due_ts=None`.
- **Reassign = fresh un-started state** for the new editor (all paths converge on `assign_folder(is_reassign=True)`); the old editor's assignment message gets its buttons stripped + unpinned. Revisions are unaffected (no deadline entry; "starts immediately" by design).
- `/stats` active-folder names link to the **assignment message** (`assignment_jump_link()`, guild/channel/message from `assignment_messages.json`), falling back to Drive links for pre-feature folders. Shared views (`/leaderboard`) keep Drive links — jump links into private editor channels don't work for others.
- `daily_digest.py` lists folders `pending_start` >12h ("assigned but never started"), skipping footage-flagged ones.
- Full design rationale: `notes/start-command-review.md`.

## Video Counting
- A Drive file counts as a video if its **extension** matches `VIDEO_EXTENSIONS` **or its mimeType starts with `video/`** — some clients (e.g. Chris) upload videos with extension-less names ('1', '2'), which Drive types `video/mp4`. Extension-only counting caused false "0 videos" review flags and silently hid 17 folders from the watcher for weeks (found 2026-07-02)

## Key Rules
- Always use `folder_id` not `folder_name` for Drive lookups
- All Drive API calls must include `supportsAllDrives=True` (and `includeItemsFromAllDrives=True` for list calls)
- All `files().list()` calls must include `id` in the fields mask — omitting it silently breaks sub-folder iteration
- **Never walk up via `files.get(fields='parents')`** — on this Shared Drive it returns `parents: []`. Always search top-down from the root folder instead
- Editor stats update in both `discord_bot` (direct complete) and `notion_bridge` (review approval)
- Never hardcode editor/client names — always pull from Notion
- Notion PATCH rejects the **entire request** if any property name doesn't exist — always verify property names against the actual DB schema before adding new ones

## Pending Assignments — Source of Truth
- **Always use Notion Active Queue (`Status = Raw`) as the source of truth for unassigned folders** — not `pending_ops_assigns.json`
- `pending_ops_assigns.json` tracks Discord message IDs for ops-assign embeds and goes stale fast: folders assigned via Notion directly, or assigned then completed, leave orphan entries in this file
- To get the real unassigned count, query Notion: `filter Status = Raw` on Active Queue DB (`44593fbf-4276-47f0-bd12-27289dcb78fd`)
- `unassigned_reminder.py` does this query every hour and logs to `reminder.log` — "Reminder sent: N folder(s)" is the live count
- When asked about pending/unassigned folders, run the Notion query or check `reminder.log`, then sync `pending_ops_assigns.json` to match (keep only entries whose `folder_id` is in the current Raw set)
- `ignored_folders.json` — folder IDs that should be skipped permanently; add here to suppress both watcher notifications and reminder pings

## Known Gotchas
- `find_edited_folder_videos()` must search **top-down** (root → client → Edited/) not walk up parents — see `_find_edited_folder_top_down()`
- The `name='Edited'` Drive query is an **exact match** — a client folder named `'Edited '` (trailing space) or any other casing/whitespace variant silently fails to match and the editor's `/complete` flags "not found in Drive" even though the folder exists. Hit this for client Zi (2026-06-17), fixed by renaming the Drive folder. If it recurs for another client, check for exact name mismatch before assuming a code bug.
- `find_edited_folder_videos()` only scanned **direct children** of `Edited/` — some clients group submissions under a parent folder (e.g. `'Phrasly '` containing `'Phrasly vid 11'`, `'Phrasly vid 12'`, etc), one level deeper than the flat per-client layout most clients use. Hit this for client Joshua's "Phrasly vid 11" (2026-06-24) — folder existed with videos but `/complete` flagged "not found in Drive". Fixed by adding a one-level-deeper fallback search into each top-level group folder when no direct match is found.
- `get_folder_video_tree()` (`gdrive_watcher.py`, new-folder detection) and `fetch_folder_video_tree()` (`notion_bridge.py`, Telegram Show Contents) used to be **hard-capped at a fixed scan depth** (root + 1–2 levels). Client Karol's `Raw Footage/lovable 2` nests videos 4 levels deep (`lovable 2 → 1 channel → 1 format → files`), so the scan found 0 videos, and `scan_client()`'s `if total_count == 0: continue` silently dropped the whole folder from `watched_files.json` — never flagged as a new folder, never notified, even though the watcher's own subfolder-count log line looked normal. Found + fixed 2026-07-25: both functions now recurse to arbitrary depth (labels join as `parent / child / grandchild`, `flat_names`/`total_count` unaffected in shape). If a folder still goes undetected, this specific bug is ruled out — look elsewhere (ignored_folders.json, exact-name mismatches, etc).
- Drive OAuth token scope must be `drive` not `drive.readonly`
- Delivery History date field is `DELIVERY_DATE_PROP = 'date:Delivered Date:start'` (the actual Notion property name)
- `files.get(fields='parents')` silently returns `[]` for all folders in this Shared Drive
- `/stats` "delivered today" comes from a live Delivery History query (not the cached `Delivered This Week` counter in Editor Profiles)
- `Avg Turnaround Days` does **not** exist in Editor Profiles — do not include it in PATCH calls
- `_notion_patch()` in `discord_bot.py` now logs errors on non-200 responses; always check logs after stats updates
- `notion-bridge` (Telegram) is currently **not in active use** — the box can't reach `api.telegram.org` (connection times out at the network level, not a code bug) and the service crash-loops on `telegram.error.TimedOut`. This is expected/known as of 2026-06-18; don't treat it as a new incident or try to debug notion_bridge.py code for it. Vex is using Discord-side commands instead.
- `assign_folder()` sends the Discord embed, flips Notion `Status` to `In Progress`, and starts the deadline. It historically did **not** write the `Editor` select property. **As of 2026-08-16 it does, but only as a fallback**: when it's given no `notion_queue_page_id` and it does have a `folder_id`, it calls `_assign_raw_to_editor()` itself. That closes the dashboard-bridge hole (see below) without double-PATCHing the UI paths, which still resolve the page id first and pass it in. The original warning still applies to anything that bypasses `assign_folder()` entirely.
- **The dashboard bridge hit this exact trap for six weeks.** A `kind: 'assign'` command from the website falls into the final `else` in `dashboard_commands_loop` and calls `assign_folder()` with no `notion_queue_page_id` — so the editor got their Discord ping and the website showed the new owner, while the Active Queue row kept the *old* `Editor` (or none at all). `/stats` is 100% Notion-driven, so the folder was simply invisible to the editor who actually had it. Found 2026-08-16: 14 of 33 live drive batches disagreed between the site and Notion, 5 of them with a blank `Editor`. Fixed by the fallback above; the 10 already-diverged rows were PATCHed by hand. The real assign paths (`/assign` slash command, `AssignEditorView` dropdown) always call `_assign_raw_to_editor()` first to PATCH both `Status` and `Editor`, then call `assign_folder()` after. If you ever bulk-assign by writing directly to `discord_queue.json` (bypassing those UI paths), you must call `_assign_raw_to_editor()` (or PATCH `Editor` yourself) for each folder too — otherwise the row shows `Status: In Progress` with no `Editor` set, and any editor-grouped view (e.g. `/editorstats` sorted by folder) still counts it as unassigned even though the editor was already pinged on Discord. Hit this for real on 2026-06-25 bulk-assigning 11 backlog folders — had to PATCH `Editor` on all 11 pages after the fact.
- **Bulk-assign ops-assign message cleanup (required):** `pending_ops_assigns.json` is keyed by Discord message ID; each entry has a `folder_id` field. After any bulk assignment, always: (1) look up each assigned `folder_id` in `pending_ops_assigns.json` to find its message ID, (2) PATCH the Discord message via `PATCH /channels/{channel_id}/messages/{msg_id}` with a green "✅ Assigned" embed + `"components": []` to remove the dropdown, (3) delete the entry from `pending_ops_assigns.json`. Discord rate-limits edits on messages >1h old — use 12s gaps between edits to avoid 429s. Bot token is in `config['discord_bot_token']`, use `Authorization: Bot {token}` header. Without this step the assignments channel still shows open "choose editor" dropdowns for already-assigned folders.
  - **User-Agent gotcha:** any raw HTTP call to `discord.com/api/...` from a script (not via discord.py) **must** set a `User-Agent` header like `DiscordBot (https://github.com/vexxefx/ccvm, 1.0)`. Without it Cloudflare rejects the request with `HTTP 403, error code 1010` before it reaches Discord — this is not an auth/token problem. Python's `urllib` sends no acceptable default UA, so every edit 403s silently. Hit this on 2026-07-12 doing a bulk-assign cleanup.

## Stats Update Flow
- `finalize_delivery()` (discord_bot) and `finalize_notion_delivery()` (notion_bridge) both:
  1. GET current Editor Profiles values fresh
  2. Add `confirmed_count` to `Delivered This Week`, `Delivered This Month`, `Total Videos Delivered`
  3. PATCH Editor Profiles
  4. Log "Before update" and "After update" with exact values
- Weekly leaderboard (`fetch_all_editor_stats_for_range()`, see Leaderboard section) queries **Delivery History** for the current Mon–Sun week grouped by editor, maxed against the cached `Delivered This Week` counter — see Leaderboard section for why the max matters
- Monthly stats still read from Editor Profiles `Delivered This Month`

## Show Contents (Telegram)
- `handle_show_callback()` always does a **fresh Drive API query** — never uses cached video_names
- `fetch_folder_video_tree()` recurses **2 levels deep**: root → subfolder → sub-subfolder
- Message format: clickable HTML drive link header + videos grouped by subfolder with `📂` section headers
- Single-section folders render as a flat numbered list (no section header)

## /remove and /recover
- Available on both **Telegram** (`notion_bridge.py`) and **Discord** (`discord_bot.py`, Team role only)
- `/remove` — shows a selection of all Pending (Status=Raw) and Active (Status=In Progress) Active Queue rows
- Selecting one archives the Notion page (`archived: true`) and caches its row data in `removed_folders.json`, keyed by `notion_page_id`
- `/recover` — lists cached removed folders; selecting one un-archives the page (`archived: false`) and drops it from `removed_folders.json`
- Archiving a page removes it from all `Status` queries (Notion API treats archived pages as deleted) without losing data — recoverable any time
- `removed_folders.json` — `{notion_page_id: {folder_id, folder_name, client_name, editor_name, video_count, status, removed_at}}`, shared between both bots
- Telegram: inline keyboard via `cmd_remove`/`cmd_recover` + `handle_remove_callback`/`handle_recover_callback`
- Discord: `/remove` and `/recover` slash commands use `RemoveFolderSelect`/`RecoverFolderSelect` dropdown views; `fetch_removable_folders()` returns combined Raw+In Progress rows
- Discord channel scoping: `fetch_editor_by_channel_id()` detects if the command is run in an editor's channel
  - In an editor's channel: `/remove` only shows that editor's In Progress folders (`fetch_removable_folders(editor_name)`); `/recover` only shows that editor's cached removals (filtered by `editor_name` in `removed_folders.json`)
  - Outside editor channels (ops/Team channels): both commands show everything across all editors and unassigned (Raw) folders

## Ignore Folders
- Ignored folder IDs stored in `ignored_folders.json`
- Vex taps 🚫 Ignore on Telegram notification → folder skipped on all future watcher runs
- ↩️ Unignore button restores original notification with full editor keyboard
- `gdrive_watcher.py` calls `is_folder_ignored(fid)` before `send_new_folder_notification()`

## Leaderboard
- **Weeks are Sunday→Saturday** (changed from Monday→Sunday on 2026-08-08, same day as the switch below) — `week_start = today - timedelta(days=(today.weekday() + 1) % 7)` is the "most recent Sunday" formula used everywhere a week boundary is computed (`build_weekly_leaderboard_embed`, `fetch_delivered_this_week_for_editor`, `/leaderboard`, `leaderboard_loop`, `weekly_leaderboard_post.py`). Do not reintroduce a plain `today.weekday()` Monday-start calculation in any new weekly-figure code — check this list for every place "this week" gets computed and keep them all in sync, that consistency is the entire point of this section.
- `/leaderboard` command's weekly figure comes from `fetch_all_editor_stats_for_range()` (live Delivery History query for the current Sun–Sat week, EDT), not the cached `Delivered This Week` Editor Profiles field directly (fixed 2026-08-08 — it had drifted to reading the cached field only, contradicting this doc; bonuses are paid weekly off this number so it needs to be live)
- `fetch_all_editor_stats_for_range(start_str, end_str)` returns `week = max(live Delivery History sum, cached 'Delivered This Week')` — the max guards against website-native deliveries (`handle_cc_dashboard_delivered`, see Creator Collective Dashboard Bridge) which PATCH the cached counter directly and never create a Delivery History row, so a pure live query would undercount them
- `reset_weekly.py` cron moved from Monday 00:00 UTC to **Sunday 00:00 UTC** (`0 0 * * 0`) to match the new Sun-Sat week — do not switch this to a calendar-month (1st–31st) split, bonuses are paid weekly and a month-anchored split would produce partial weeks at month boundaries
- Monthly figure (Team-only second embed) still reads the cached `Delivered This Month` field — monthly reset (`reset_monthly.py`, 1st of month 00:00 UTC) is a separate, already-correct cadence unrelated to the weekly changes above
- **Weekly auto-post moved out of `discord_bot.py` on 2026-08-08.** `weekly_leaderboard_post.py` (repo root, wired to cron `30 15 * * 6` = **Saturday** 15:30 UTC / 11:30 PM PHT — the night before the Sunday reset) posts the final weekly numbers to channel `1499407261381038242`, pinging the `Editors` role (`1498943182296190977`), so Vex has a number for bonus calculations before `reset_weekly.py` zeroes the counters. It reimplements the same `max(live Delivery History, cached counter)` logic as `fetch_all_editor_stats_for_range()` rather than importing `discord_bot.py` (see that module's own one-off-script convention).
- `WEEKLY_LEADERBOARD_AUTOPOST_ENABLED = False` in `discord_bot.py` — the old `leaderboard_loop` weekly branch (also updated to Sunday-start math for consistency, in case it's ever revisited) is intentionally off so it doesn't duplicate the cron post above. Don't re-enable it without also disabling/removing the cron job, or Vex gets two weekly posts.
- `MONTHLY_LEADERBOARD_AUTOPOST_ENABLED = False` (paused 2026-07-31, Vex posts monthly manually) — unrelated to the weekly change, still sitting in `leaderboard_loop`, would resume if flipped back to `True`.
- **Cadence history:** Mon-Sun (original) → briefly "calendar-month" 2026-08-02 (never actually coded, just a paused flag — see git blame on `WEEKLY_LEADERBOARD_AUTOPOST_ENABLED`) → reverted to Mon-Sun 2026-08-08 → switched to Sun-Sat 2026-08-08 (same day, later). The Sun-Sat switch happened mid-cycle deliberately (Vex chose to cut the in-progress Mon-Sun week short rather than wait for it to finish) — if you're ever auditing why one week's Delivery History range looks short, this is why.

## Assignment Embed Drive Links
- `assign_folder()` does top-down search: `DRIVE_ROOT_ID` → client folder → Raw Footage → subfolder
- Caches client root in `_client_root_folder_cache`, Raw Footage in `_client_raw_footage_folder_cache`
- Both caches are in-memory (reset on bot restart)

## Health Monitor
- Runs every 30min via cron; logs to `logs/health_monitor.log`
- Alerts on: expired token, webhook ping >3h old, watch expiry <2h (auto re-registers), dead services (auto-restarts), >5 errors in last 30min in discord_bot.log or notion_bridge.log
- `drive_webhook_last_ping.json` — written by drive_webhook.py on every POST

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
- Stored in `editor_counters.json` — `{editor_name: {revisions: N, missed_deadlines: N, slow_pickups_4h: N, slow_pickups_12h: N}}`
- `revisions` incremented in `open_revision_assignment()` each time a folder is sent back
- `missed_deadlines` incremented in `deadline_checker()` when `due_ts` passes without delivery; guarded by `missed_deadline_logged` flag in `deadlines.json` to prevent double-counting
- `slow_pickups_4h` / `slow_pickups_12h` (added 2026-07-04) incremented in `deadline_checker()` when a `pending_start` folder sits un-started past 4h / 12h — based on pure elapsed time (shift-held nags don't hide it); guarded by `slow_pickup_4h_logged` / `slow_pickup_12h_logged` flags per entry, reset by `reset_start_state()` so a reassign starts the new editor's clock fresh; `footage_flagged` folders never count
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

## Embedded Drive Links (Client / Raw Footage / Edited)
- `build_drive_links_field(client_root_id, raw_folder_id, edited_subfolder_id)` is the shared helper for embedding clickable Client/Raw Footage/Edited folder links in any Discord embed — omits whichever link wasn't resolved (e.g. no Edited link → only Client + Raw Footage are shown)
- `raw_folder_id` is just the assignment's `folder_id` — it's already the Drive ID of the Raw Footage subfolder, no extra Drive lookup needed
- Used in: `CompleteModal.on_submit`'s review-flag embed and clean-delivery embed, `DiscordReviewView.approve`'s confirmation embed, `finalize_delivery()`'s edited-assignment-message embed
- Telegram `/complete` review message (`tg_msg` in `CompleteModal.on_submit`) builds its own HTML `<a href>` version of the same three links inline (`tg_links`)
- Creator notify messages (`creator_complete_notify` payloads) carry `client_folder_drive_link` + `raw_footage_drive_link` + `edited_folder_drive_link` — `handle_creator_complete_notify()` falls back to Client + Raw Footage links when no Edited folder was found
- `DiscordReviewView`'s `review_data` now also stores `client_root_id` and `edited_subfolder_id` so the eventual approve confirmation (and `finalize_delivery()` call it makes) can still resolve the Edited folder link — previously `edited_subfolder_id` wasn't passed through on manager-approved reviews, so the creator never got an Edited folder link in that path

## /info Command
- Editor-channel mode: `/info` (no args) resolves the editor via `fetch_editor_by_channel_id()` and shows a dropdown of their in-progress folders (`fetch_in_progress_for_editor()`) + last 10 delivered (`fetch_recent_delivered_for_editor()` — queries **Active Queue** with `Status=Delivered`, not Delivery History, specifically so the result carries `notion_page_id`)
- Team-wide mode: `/info folder:<name>` — autocompletes live against Active Queue `Video` title (`contains` filter) across **all** statuses/clients, suggesting `"FolderName — ClientName"`; the autocomplete `value` is the Notion page ID itself
- Both modes converge on `_build_dossier_embed(notion_page_id)` — one function builds the full embed (status, editor, videos, due/overdue or delivered/turnaround, revisions from Revision Log, notes, and Client/Raw Footage/Edited Drive links via `resolve_drive_ids_for_dossier()`)
- Why Active Queue and not Delivery History for delivered lookups: Delivery History rows don't carry a back-reference to the Active Queue page, and Active Queue rows are never deleted on delivery (just `Status` flips to `Delivered`) — so the Active Queue `page_id` is a single stable key usable whether a folder is in-progress or long since delivered

## Turnaround / Overdue Tracking
- `assign_folder()` stamps `entry['assigned_at'] = time.time()` into the `deadlines.json` entry on every (re)assignment — resets on reassign so turnaround reflects the *current* editor's time, not the folder's full lifetime
- `finalize_delivery()` reads `assigned_at` + `due_ts` from the deadlines entry **before** popping it (the entry is deleted immediately after) and writes `{assigned_at, turnaround_hours, was_overdue, editor_name, recorded_at}` to `delivery_meta.json` keyed by the Active Queue `notion_page_id` — `load_delivery_meta()` / `save_delivery_meta_entry()` in `discord_bot.py`
- **Known gap:** only folders assigned/delivered after 2026-06-24 have this data — `deadlines.json` entries didn't carry `assigned_at` before that, so older folders show no turnaround/overdue in `/info`

## Pending Review Buttons Survive Restarts
- `DiscordReviewView`'s Approve button is now re-registered on `on_ready`, same pattern as `AssignEditorView` (ops-assign dropdowns) — previously it wasn't, so any flagged `/complete` review still pending across a bot restart had a **silently dead** Approve button (no error shown to whoever clicked it, it just did nothing)
- Hit this for real: Jasmine/Launchpoint 3 (Kaye) on 2026-06-24 — review posted at 05:17 UTC, bot restarted at 05:20 UTC, Approve button dead from then on; had to finalize it manually via a one-off script calling `finalize_delivery()` directly
- Fix: `review_data` now also stores `review_message_id` (the *review embed's* own message ID — distinct from `discord_message_id`, which is the original assignment message) + `review_channel_id`, saved right after `assign_ch.send()`. `on_ready` calls `load_pending_reviews()` and `bot.add_view(DiscordReviewView(rd), message_id=rd['review_message_id'])` for every entry with `status == 'pending'`
- **Gap:** reviews created before this fix don't have `review_message_id`, so they're skipped on re-registration (logged as part of the `0 pending review view(s)` count) — their buttons stay dead; finalize those manually like the Jasmine case above if any are still sitting in `pending_reviews.json`

## Cross-repo PRs must name their counterpart

The editing system spans this repo and `trycreatorcollective-website`. A reviewer only sees the repo they're looking at, so a PR that is half of a pair is unreadable on its own.

Every PR touching the bridge states in its body:
- **Which repo holds the other half and its PR number** — or, just as important, **"no companion PR needed"** and why. A dev searching the website repo for a fix that lives entirely here will not find it and will assume it wasn't done. That happened on 2026-08-16 with #21 (`assign_folder` writing the Notion Editor): the whole fix was six lines here, the website was already sending a correct payload, and nothing said so.
- **Deploy order**, when it matters. Merging here redeploys the Railway service on its own, but a website change that lands first can still reach a bot that hasn't restarted yet. A website change that starts sending a new command kind before the bot handles it is a live incident: unknown kinds fall through `dashboard_commands_loop`'s final `else` and get re-posted as an ASSIGN.
- **Whether the website half includes a migration**, since those auto-apply on merge there.
- **Full URLs, never bare `#123`.** The two repos have overlapping PR numbers — both have a #21, and GitHub auto-links `#21` to whichever repo you're reading, so a cross-repo reference silently points at the wrong thing. Bot #21 is the Notion Editor write; website #21 is an unrelated June sign-in fix. Write `https://github.com/Creator-Collective/trycreatorcollective-website/pull/1284` or at minimum `trycreatorcollective-website#1284`.

## Creator Collective Dashboard Bridge (added 2026-07-31)
- Mirrors folders/assignments/status into the separate `trycreatorcollective-website` repo's editing ticket system so Vex can see and assign from the site. Best-effort throughout — a dead/unreachable dashboard must never block or slow the Discord/Notion flow.
- Config keys (`config.json`, gitignored): `dashboard_url` (assign endpoint), `dashboard_status_url` (status endpoint), `dashboard_commands_url` (inbound command feed), `dashboard_offer_url` (where an editor's accept/pass on an offer goes — the site's `/api/discord/editing-offer`), `dashboard_secret` (Bearer token, matches the site's `EDITING_BRIDGE_SECRET`). `_dashboard_post()` no-ops silently (`return True`, nothing queued) if either the relevant URL or the secret is missing from config — that's not a network failure, it's config not merely unset, and it produces zero log signal, so always check both keys exist before assuming the endpoint is broken.
- `_dashboard_post(kind, payload)` (`discord_bot.py`) — one POST attempt. `kind` picks the endpoint via `_DASHBOARD_POST_URL_KEYS`: `assign` → `dashboard_url`, `status` → `dashboard_status_url`, `offer` → `dashboard_offer_url`. Returns `True` when settled (sent, or rejected in a way retrying can't fix), `False` when it should retry.
  - `400`/`404`/`422` are terminal — logged at ERROR, never retried (bad payload, ticket/assignment never mirrored, or no matching student profile — all human problems, not network blips)
  - `401` is retryable but logged at ERROR with an explicit "dashboard_secret doesn't match the site's EDITING_BRIDGE_SECRET" message — without this a bad secret retries forever and reads exactly like the site being down
  - Anything else retries silently at WARNING
- Failures that should retry are parked in `pending_dashboard_pushes.json` (`_queue_dashboard_push`, one entry per `(kind, identity)` — a later state supersedes an earlier one) and flushed by `flush_dashboard_pushes()` from the poll loop. Identity is per-kind (`_push_identity`): the folder for drive-keyed pushes, the `offer_id` for offer answers. Offers carry no `folder_id`, so keying them on one would make every parked answer look identical and drop all but the last.
- **`assign_offer`** (site → bot) — asks ONE editor whether they'll take a batch rather than dropping it on them. Posts to their own channel (DM fallback) with Accept / Pass buttons; Pass opens a modal for an optional reason. Only the addressed editor can answer (`AssignOfferView._is_addressee`, gated on `editor_discord_id`; falls open when the site sent no id). The answer goes back through `post_dashboard_offer_response` → `dashboard_offer_url`, and the SITE does the assignment on accept — the bot never writes the ticket itself. Stored in `pending_ops_assigns.json` with `card_kind: 'assign_offer'`, which is what keeps `on_ready` from restoring it as an editor-picker dropdown (it also carries a `ticket_id`, so the old two-arm check would have).
- `post_dashboard_assignment(payload)` — mirrors an assignment (create/update ticket). Called from two places:
  - `assign_folder()` — every real assignment, with `editor_name`/`editor_discord_id` populated (the assign push)
  - `handle_ops_assign_request()` — fired once per newly-detected folder (gated on `_is_new_folder` upstream in `notion_bridge.py:send_new_folder_notification()`), with `editor_name`/`editor_discord_id` omitted entirely — the site creates the ticket unassigned (`status: submitted`) so it shows in the dashboard's unassigned queue before Discord is ever touched (the detection push)
  - Both pushes also send `creator_channel_id` (and `creator_discord_id` when known) resolved via `fetch_creator_discord_info(client_name)` against the Creator Assignments DB — the site resolves the creator by Discord id first, falling back to normalized-name matching only when no id was sent. This is what disambiguates creators sharing a first name (e.g. two different "Chris"es) — name-only matching can't tell them apart. `creator_discord_id` is usually empty (Creator Assignments' "Discord User ID" field is only populated for a few creators) — that's fine, it's optional; `creator_channel_id` is present for essentially everyone and is enough to disambiguate.
  - **Do not add the detection push at `pending_folders.json`'s write point** — that write only happens after a *successful* Telegram send, and Telegram has been unreachable since 2026-06-18 (see gotcha below), so it would never fire under current conditions.
- `post_dashboard_status(folder_id, status, ...)` — tells the dashboard a batch moved to `delivered` or `revisions`. Called from `finalize_delivery()`, `_finalize_va_approval()`, and `open_revision_assignment()`.
  - **Never send `status='approved'`** even though the endpoint accepts it — on the site, `approved` means the *creator* accepted the cuts, is terminal, and closes the ticket (removing their revision path). That approval only ever flows site → bot (`handle_cc_dashboard_approve`, fired when a creator approves in the dashboard UI), never bot → site. A VA/Team sign-off in Discord is still `delivered` from the dashboard's point of view, not `approved` — nothing in this codebase represents the creator's own sign-off.
- `test/replay_dashboard_status.py` — one-off backfill for status pushes discarded before the site's status endpoint existed (it 404'd until 2026-07-31). Sources from Notion Active Queue directly (not `delivery_meta.json`/`deadlines.json`/`editor_state_history.jsonl` — none of those retain `folder_id` once a folder is delivered; Active Queue rows persist post-delivery with `Drive Link` intact). Dry-run by default; `--since`/`--limit`/`--folder-id` filters.
- `test/backfill_dashboard_unassigned.py` — one-off catch-up mirroring every current Active Queue `Status=Raw` folder as an unassigned ticket. Dry-run by default. Splits any `422 student_not_found` by the response body's `had_ids` field: `false` means we had no `creator_channel_id`/`creator_discord_id` to send (fixable by populating Creator Assignments); `true` means the creator genuinely has no dashboard profile yet (needs a human) — **but confirm what `had_ids` actually covers before trusting `true`**: it may only reflect `creator_discord_id` (real user id) and not `creator_channel_id`, in which case a `true` for a creator we only have a channel id for isn't necessarily "hopeless," just "wrong kind of id." Hit this for real with creator "Chris" on 2026-07-31 — unresolved as of that backfill.
- Both catch-up scripts follow the `test/` `BASE_DIR`-is-parent convention and reimplement the minimal posting/parking logic locally rather than importing `discord_bot.py` — that module is ~7000 lines with a live `discord.Client` and command-tree decorators that execute at import time; pulling it into a one-off script's process is unnecessary weight and risk for reusing ~15 lines of logic.
- **Website-native batches in `/stats` (added 2026-08-05):** a `notify`-kind batch (no Drive folder → no Notion row) used to be entirely invisible to `/stats`, since `/stats` is 100% Notion-driven. `dashboard_batches.json` (gitignored, keyed by `ticket_id`) is now the shadow record for these — `handle_cc_dashboard_notify()` upserts an active entry on assignment/reassignment; `/stats`'s editor branch reads `active_dashboard_batches_for_editor()` into a `🌐 Website Batches` field alongside the normal `📁 Active Folders` one.
  - Delivery requires a new inbound command kind, `delivered` (ticket_id, editor, video_count) → `handle_cc_dashboard_delivered()`, which marks the `dashboard_batches.json` entry delivered and PATCHes Editor Profiles' `Delivered This Week`/`Delivered This Month`/`Total Videos Delivered` directly (same fields `finalize_delivery()`/`_finalize_va_approval()` touch), since there's no Notion Delivery History row for these to land in. Week/month/total then surface in `/stats` for free via the existing `max(live query, editor_data)` fallback and the direct `editor_data['month']`/`['total']` reads. "Today" has no such fallback (no live Delivery History query will ever find these), so `/stats` also adds `dashboard_delivered_videos_for_editor(editor_name, since_ts=<today 00:00 EDT>)` straight from `dashboard_batches.json`.
  - **`trycreatorcollective-website` must actually send the `delivered` command** for any of this to fire — as of 2026-08-05 the site only sends `assign`/`assign_request`/`revision`/`approve`/`notify`/`message`. Until the site adds `kind: 'delivered'` (with `ticket_id`, `editor_name` or `editor_discord_id`, `video_count`) to its outbound command feed, website-native batches will show correctly as active in `/stats` but their delivered counts still won't move. This is a site-side change, not fixable from this repo.
- **Unassigned website batches in `/editorstats` (added 2026-08-13):** an `assign_request`-kind command (unclaimed website batch, no editor yet) posts to `#assignments` via `handle_cc_dashboard_assign_request()` and is recorded in `pending_ops_assigns.json` — but `dashboard_batches.json` only ever gets an entry once a batch is actually assigned (`upsert_active_dashboard_batch`, called from `handle_cc_dashboard_notify()`), so there was previously no team-wide view of what's sitting unclaimed on the website: `/editorstats`'s `📁 Unassigned Folders` field is Notion-only (`fetch_active_queue_non_delivered()`), and `/stats`'s `🌐 Website Batches` field is per-editor and active-only. `editorstats_command()` now also calls `fetch_pending_website_batches()` (already existed, previously only used by `/assign`'s autocomplete/manual-entry paths) and renders a `🌐 Unassigned Website Batches` field the same way, linking each entry to its dashboard ticket via `ticket_url` when present.

## Where this runs (Railway, since 2026-08-19)

The Ubuntu box died on 2026-08-19 and everything moved to Railway, project
`harmonious-charisma`, service `worker`. There is no systemd, no ssh, no ngrok.
Deploys happen on merge to `main`.

- **One container runs three processes.** `railway_boot.py` is the entrypoint:
  it writes the secrets, links the state files, starts `drive_webhook.py` and
  `cron_runner.py`, then runs `discord_bot.py` in the foreground. They share a
  container because they share state files, and a Railway volume attaches to
  exactly one service.
- **Secrets are env vars, not files.** `CC_CONFIG_JSON`, `CC_TOKEN_JSON`,
  `CC_CREDENTIALS_JSON` hold the entire contents of `config.json`,
  `token.json`, `credentials.json`. The shim writes them to disk at boot; a
  file already present always wins, so a laptop checkout is unaffected.
- **State lives on a volume at `/data`,** symlinked back to the repo dir, so
  counters, deadlines and the pending queues survive a redeploy. Write state
  files through the symlink — `os.replace(tmp, path)` replaces the LINK and
  quietly puts the file back in the container (see `save_state` in
  `cron_runner.py` for the pattern that doesn't).
- **The crontab is `cron_runner.py`,** not crontab. Twelve jobs, schedules in
  its `JOBS` table, gated by `CC_RUN_CRONS=1`. Each run is a subprocess with a
  15-minute timeout. No boot-time catch-up except `register_watch` and
  `refresh_schedule_cache`.
- **The Drive webhook** is the same container on `$PORT`, public at
  `worker-production-3ee99.up.railway.app/webhook`, gated by
  `CC_RUN_WEBHOOK=1`. `DRIVE_WEBHOOK_URL` is what `register_watch.py` points
  Drive at — that used to be a hardcoded ngrok tunnel.
- **Logs:** `railway logs --service worker`. **Restart:** `railway redeploy`.
- `notion_bridge.py` (the Telegram side) and `dashboard.py` (the Flask
  dashboard) are NOT running anywhere right now — they had their own systemd
  units on the box and haven't been rehomed.

### Google OAuth
`reauth.py` mints a new `token.json` from any checkout (OOB flow, paste the
code). Then `railway variables --set "CC_TOKEN_JSON=$(cat token.json)"`. The
OAuth client is still in **testing** in Google Cloud, which expires refresh
tokens after 7 days — publishing it is what stops this recurring.

# `/start` Command — Design Review & Suggestions

> **STATUS: BUILT & DEPLOYED 2026-07-03** — all four build-order steps shipped in one pass
> (core state + buttons, 4h/8h/12h pickup ladder, displays, shift-aware nags + creator notify
> + pickup metric + footage button). See the "▶️ Start / Pickup Flow" section in CLAUDE.md
> for the as-built reference.

*Idea (from colleague): editor runs `/start` when they actually begin a video, and the deadline timer counts from that command instead of from when the folder was assigned. Goal: cut down overdues since the clock only starts once they pick it up.*

*Reviewed 2026-07-03 against the current deadline flow: `assign_folder()` at `discord_bot.py:4786`, `deadline_checker()` at `discord_bot.py:6406`.*

---

## Verdict

Good idea, fits the existing code cleanly — **but** it has one failure mode that will quietly undo the benefit unless designed around (see "The trap" below).

## Why it's sound

Right now `assign_folder()` stamps `due_ts = assignment time + 24h` even if the editor is asleep, mid-shift on another video, or hasn't seen the ping. A lot of the current "overdues" are really "assigned at a bad hour," which pollutes the `missed_deadlines` counters and the AI assignment context that uses them. Starting the clock at pickup makes the deadline measure what actually matters: **editing time**.

## ⚠️ The trap

If the deadline only exists after `/start`, an editor who never runs it has **no deadline at all** — no 6h warning, no 12h escalation, no ops ping. The folder just sits. Overdues would drop, but partly because the clock never starts.

So `/start` must ship with two safety nets:

1. **Pickup deadline** — if a folder isn't started within N hours of assignment (8–12h, or shift-aware using `schedule_cache.json`), ping the editor; escalate to ops after that. `deadline_checker()` already loops every entry, so this is a new branch, not a new loop.
2. **Auto-start fallback** — if an editor runs `/complete` on a folder they never started, backfill `started_at = assigned_at` so turnaround stats aren't null and there's no incentive to skip `/start` to dodge stats.

## UX suggestion

Make it a **▶️ Start button on the assignment embed** rather than (or in addition to) a slash command:

- Zero typing, no ambiguity about which folder
- The persistent-view pattern already exists (`AssignEditorView` / `DiscordReviewView` re-registration in `on_ready`), so the button survives bot restarts
- Fallback: channel-scoped `/start` with a dropdown of the editor's un-started folders — same shape as `/remove`

## Data model (small change)

- `deadlines.json` entries gain `started_at` (null until pickup), keep `assigned_at`
- `due_ts` stays **null** until start, then becomes `started_at + 24h`
- Existing 34 entries keep current behavior — only new assignments enter the "pending start" state. **No migration needed.**

## Interactions to handle (where the real work is)

| Area | What's needed |
|---|---|
| **Reassign** | New editor gets a fresh un-started state; `update_deadline_editor()` must clear `started_at` + escalation flags |
| **Revisions** | Decide: does a revision reopen require a new `/start`? Recommendation: **start immediately** — the editor already knows the folder; a second Start click is friction with no signal |
| **Turnaround stats** | `delivery_meta.json` should record *both* `pickup_hours` (assigned→started) and `edit_hours` (started→delivered). Right now a slow pickup and a slow edit look identical in `turnaround_hours` — splitting them is a free win |
| **Display** | `/stats`, `/info`, `format_deadline()`, and the daily digest all need a "⏸️ Not started yet (assigned 3h ago)" state instead of a countdown |

## Additional changes to ship alongside

1. **Daily digest line for un-started folders** — `daily_digest.py` already flags reviews >24h and overdue folders; "assigned but not started >12h" belongs in the same embed.
2. **Shift-aware pickup nagging** — use `schedule_cache.json` so the pickup reminder doesn't fire at an editor whose shift starts in 5 hours; fire it N hours into their next shift instead. Without this the pickup pings become noise and get ignored.
3. **Avg pickup time in `/editorstats`** — once captured, this is the metric that shows who sits on assignments. Also feeds the AI assignment context alongside revisions/missed deadlines.
4. **"⚠️ Problem with footage" button next to Start** — the moment an editor opens a folder is exactly when they discover missing/corrupt raw footage. One-click report to ops at that moment beats a DM to Vex an hour later, and it explains *why* something wasn't started.

## What NOT to do

Don't retro-apply this to the 9 `indefinite` entries or in-flight folders — let them drain under current rules.

## Round 2 decisions (2026-07-03, after discussing with Vex)

### Button lifetime — not an issue
Buttons don't expire on Discord's side; the "dead after a few minutes" behavior is discord.py's default `timeout=180`. The Start button must be a **persistent view** (`timeout=None`, stable `custom_id=f'start_{folder_id}'`, re-registered in `on_ready` for every un-started `deadlines.json` entry) — same pattern as `AssignEditorView` / `DiscordReviewView`. Handler must `defer()` within 3s before touching Notion/JSON. Start must be **idempotent**: second click or click-after-reassign → ephemeral "already started / no longer yours," never a second timer.

### Pause — do NOT build self-serve pause in v1
A free ⏸️ button is the abuse hole that undoes the feature (pause nightly → deadline never fires, `edit_hours` becomes fiction, `deadline_checker` grows pause branches everywhere). Legit "I need to stop" cases are already covered:
- **Footage problems** → the "⚠️ Problem with footage" button (routes to ops with a reason)
- **Need more time** → the existing `/extend` flow (`ExtendFolderSelect`, discord_bot.py:5397)
- If pause is demanded later: request-based only (one-tap Vex approval, max once per folder)

### Pickup nag ladder (Vex, 2026-07-03): first flag at 4h, then escalate
- **4h** after assignment → gentle nudge in editor channel (with Start button + jump link)
- **8h** → stronger second nudge
- **12h** → ops channel pinged + folder listed in daily digest; repeat ops ping every 12h after
- Shift-aware where schedule data exists: count 4h of *shift-overlapped* time, not wall-clock, so nags don't fire mid-sleep; plain wall-clock fallback when no schedule

### Creator transparency: "editing started" embed (Vex, 2026-07-03)
- When the editor presses ▶️ Start, the **creator's channel** gets a `🎬 Editing has started on [folder]` embed — rides the same `discord_queue.json` IPC path as reassign/complete creator notifications (new `creator_start_notify` type + handler)
- **Rule: creators only see the positive event.** Never expose not-started nags, escalations, or pickup delays to creators — internal ops only
- **No delivery-time promise in the embed** — an `/extend` or footage problem would turn it into a visible miss; just announce the start

### Assignment messages getting buried in editor channels
Assignments and chat share the same channel, so the embed scrolls away. Fix by never depending on the editor finding the original message:
1. **Every pickup reminder carries its own ▶️ Start button + jump link** to the assignment message (`https://discord.com/channels/{guild}/{channel}/{message_id}` — `discord_message_id` is already stored). The nag itself is the recovery path.
2. **`/start` fallback with a dropdown** of the editor's un-started folders (same shape as `/remove`).
3. **Pin on assign, unpin on Start** — pins tab becomes the editor's live to-do list (50-pin limit is plenty).

### `/stats` folder names link to the assignment message (Vex's idea — adopted)
Instead of hyperlinking folder names to Drive (`folder_link()`, discord_bot.py:1680), link them to the **assignment message** (`discord.com/channels/{guild}/{channel}/{message}`). The assignment embed already carries all three Drive links + deadline + (soon) the ▶️ Start button — so `/stats` becomes a navigation hub and a fourth recovery layer for buried assignments. Requirements:
- Store `discord_channel_id` alongside `discord_message_id` in the deadlines entry (`assign_folder()`)
- Fall back to the Drive link when no message/channel ID exists (old entries, deleted messages)
- **Scope by audience:** jump links only in editor-scoped and Team-scoped views — in shared views (`/leaderboard` etc.) other editors can't open someone else's channel, so keep Drive links there

## Suggested implementation order

1. `deadlines.json` state + ▶️ Start button on assignment embed
2. `deadline_checker()` pickup-escalation branch
3. Display updates (`/stats`, `/info`, `format_deadline`, digest)
4. Extras: digest line, shift-aware nagging, `/editorstats` pickup metric, footage-problem button

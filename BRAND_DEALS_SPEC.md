# Brand Deals Tracker — Functional Spec

Build-ready summary of the existing **Brand Deals Tracker** bot
(`~/brand_deals_bot/deal_tracker.py`, ~1,070 lines). Standalone Discord DM bot,
backed entirely by a Notion database. No web UI today — everything is Discord
DMs to a single user. This doc is the reference for rebuilding it as an app.

---

## 1. What it does (one line)
Tracks brand-deal posting quotas across **Instagram / TikTok / YouTube /
Facebook**. It does **not** scrape or auto-count posts — a human clicks
"Mark Done" per platform per post. The bot handles reminders, scheduled
"time to post" nudges, caption rotation, and deadline escalation.

**Key design decision:** automated post-counting (IG/TikTok/YouTube APIs +
scrapers) was tried three times and abandoned — IG GraphQL breakage, TikTok
geo-block + bot-detection, YouTube worked but was dropped for simplicity. The
model is deliberately **manual mark-done**. Do not rebuild scraping.

---

## 2. Data model — Notion "Brand Deals" DB
- **DB id:** `9b469b29-b891-43f9-94e6-0354f96ea28a`
  (the real database ID — *not* the `72cf786a…` data-source/collection ID that
  Notion's MCP surfaces; that one 404s)
- **One row = one deal.**

| Property | Type | Meaning |
|---|---|---|
| `Brand Name` | title | Deal name |
| `Status` | select | `Active` / `Completed` / `Overdue` |
| `Start Date`, `Deadline` | date | Deadline drives reminder escalation |
| `{Platform} Handle` | rich_text | e.g. `Instagram Handle`; leading `@` stripped |
| `{Platform} Required` | number | Quota; `0`/empty = platform inactive for this deal |
| `{Platform} Done` | number | Manually incremented by button click |
| `Caption Queue` | rich_text | Multiple captions separated by a line that is exactly `---` |
| `Post Times` | rich_text | e.g. `09:00,18:00` — **UTC**; takes priority for scheduling |
| `Posts Per Day` | number | Fallback if `Post Times` empty; auto-spaced across 09:00–21:00 UTC |
| `Reminder Intervals` | rich_text | e.g. `72,24,2` (hours before deadline); default `72,24,2` |
| `Deal Notes` | rich_text | Free note, editable from the UI |

- The 4 platforms are hardcoded: `instagram, tiktok, youtube, facebook`.
- **Platform is "active"** for a deal only if `Handle` is set **AND**
  `Required > 0` (`is_platform_active()`). Inactive platforms are ignored
  everywhere (no fields, no buttons, no nudges).

---

## 3. Core behaviors / business logic
- **Mark Done** → bump `{Platform} Done` by 1 in Notion (capped at Required),
  advance the caption index, stamp a cooldown, record an undo point.
- **Auto-complete** → when every *active* platform has `Done ≥ Required`,
  set `Status = Completed` and send a 🎉 confirmation.
- **Caption rotation** → `Caption Queue` split on lines equal to `---`; current
  index tracked per deal; advances +1 on every Mark Done; caps at the last
  entry (no wrap, no error). Undo rolls it back one.
- **Per-slot cooldown** → after marking a platform done, its button greys to
  "✓ posted — next slot pending" until the next scheduled posting slot arrives.
  Only applies if the deal has a schedule; no schedule → no cooldown.
- **Single-level Undo** → restores the Done count, rolls caption back one,
  clears the cooldown, un-Completes the deal if the mark had completed it.
  One level deep, per deal.

---

## 4. Two background loops
- **Nudge loop (5-min tick)** — "time to post" reminders. Computes each deal's
  daily slots (`Post Times`, or auto-spaced `Posts Per Day`). Fires one DM per
  slot per day with **catch-up semantics**: a passed-but-unsent slot fires late
  rather than being dropped; multiple missed slots collapse into one nudge.
  Dedup via `nudges_sent`, pruned to today each tick. Nudge lists every
  still-short platform + current caption + a Mark Done button each.
- **Sync loop (30-min tick)** — refreshes each deal's main embed and runs
  deadline escalation: at each `Reminder Intervals` threshold send a reminder
  (once); at/after the deadline send 🚨 every 2h and set `Status = Overdue`.
  Overdue deals stay in both loops until Completed.

### Slot computation
- If `Post Times` set (e.g. `09:00,18:00`) → use those exact UTC times.
- Else if `Posts Per Day = N` → auto-space N slots across the default window
  **09:00–21:00 UTC**: 1 post → 09:00; N posts → evenly stepped
  `start + (end-start)/(N-1) * i`.
- Else → no schedule (no nudges, no cooldown).

---

## 5. Front-end UX (currently Discord DM to one user)
- **Main deal card** (`📦 Brand Name`): per-platform progress bars
  (`✅✅⬜⬜ 2/4`), deadline + days left/overdue, "⏰ Next post" countdown,
  notes; status color = red (≤24h to deadline) / yellow (behind) / green (on
  track). Actions: **Mark {Platform} Done** ×N, Refresh, Edit Note, Undo,
  Mark Complete, Copy caption.
- **Caption** is a separate **bare plain-text** message (no rich formatting) so
  mobile long-press → Copy Text grabs the raw caption verbatim. (In a real app,
  just a copy-to-clipboard button.)
- **Nudge card** (`⏰ Time to post`): brand-accent color (stable per deal),
  pending platforms, next-reminder countdown, caption + one mark button per
  pending platform.

---

## 6. One-tap caption copy (existing web surface)
Captions are published to `~/brand_deals_bot/current_captions.json`, and
`drive_webhook.py` (in the CC Video Manager repo) serves a `/caption/<page_id>`
copy page over a static ngrok domain. The 📋 button links to it. This is the
only existing web surface — a natural thing to fold into the new app.

---

## 7. State (persistence beyond Notion)
`deal_tracker_state.json` keys: `messages` (deal→card id), `caption_messages`,
`caption_index`, `nudges_sent`, `reminders_sent`, `last_marked` (cooldowns),
`last_action` (undo), `verification`.

**Concurrency rule (important):** any state write after an `await` must be an
atomic read-modify-write (load → mutate → save with no awaits in between). The
two loops interleave at awaits, and a stale full-state save silently drops the
other loop's writes.

---

## 8. Config & ops
- Config needs only: `notion_token`, `notion_deals_db_id`, user id, bot token.
  **No API keys / session cookies anywhere** — all removed in the manual pivot.
- Runs as systemd unit `brand-deals-bot`; auto-restarted by `health_monitor.py`.

---

## 9. Deal lifecycle
```
          create (Status=Active)
                  │
                  ▼
   ┌──────────  Active  ──────────┐
   │  (nudges + reminders run)    │
   │                              │
   │  all active platforms        │  deadline passes
   │  Done ≥ Required             │  while still behind
   ▼                              ▼
Completed  ◀── un-complete ──  Overdue
   ▲            (Undo)            │
   └───── all Done ≥ Required ────┘
```

---

## 10. What to build for the app
Replace the Discord-DM front-end with a web/mobile UI over the same model:
- Read/write the same Notion properties (or migrate to your own DB — Notion is
  currently the single source of truth).
- Reimplement: platform-active rule, caption `---` split + rotation, slot
  computation (`Post Times` vs auto-spaced `Posts Per Day`), per-slot cooldown,
  single-level undo, auto-complete, deadline reminder escalation, and the
  nudge scheduler with catch-up semantics.
- The "mark done" action reduces to: increment Done → advance caption →
  check-and-maybe-complete.

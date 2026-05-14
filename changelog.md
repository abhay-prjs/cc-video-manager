# Changelog

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

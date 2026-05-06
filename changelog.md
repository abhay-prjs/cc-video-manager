# Changelog

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

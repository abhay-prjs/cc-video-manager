# notion_bridge.py dead-Telegram-code cleanup checklist

Context: Telegram is permanently dead (unreachable since 2026-06-18, never coming
back). `gdrive_watcher.py` only imports 3 things from this file:
`send_new_folder_notification`, `is_folder_ignored`, `send_folder_update_notification`.
Discord (`discord_bot.py`) does not import anything from here — it has its own
duplicate implementations of several helpers (notion_headers, Drive folder
lookups, ignore-folder handling, delivery history, etc).

Going through one item at a time. Check off as we land each one.

- [ ] **1. Remove the Telegram bot entrypoint** — `main()` (2808-2853), the
  `if __name__ == '__main__'` block, and the `Application`/`CommandHandler`/
  `CallbackQueryHandler`/`MessageHandler`/`filters` imports from `telegram.ext`
  and `Update`/`ContextTypes` from `telegram`. Nothing else can run this file
  standalone after this — confirm the `notion-bridge` systemd service should
  be disabled/removed too (it's been crash-looping anyway).

- [ ] **2. Remove all `async def *(update, context)` Telegram command handlers**
  — `cmd_help`, `cmd_load`, `cmd_pending`, `cmd_today`, `cmd_editor`,
  `cmd_client`, `cmd_remove`, `cmd_recover`, `cmd_reassign`,
  `cmd_pending_reviews`, `cmd_recommend`, `cmd_ask`, `cmd_autoassign`,
  `cmd_schedule`, `cmd_setschedule`, `cmd_note`, `cmd_markoff`, `cmd_whosout`,
  `handle_text_assignment` (lines ~1319-2472, ~25 functions). All of these
  have working Discord slash-command equivalents already in `discord_bot.py`.

- [ ] **3. Remove all Telegram callback-query handlers** —
  `handle_show_callback`, `_clear_update_assignment_buttons`,
  `handle_assignment_callback`, `handle_ignore_callback`,
  `handle_unignore_callback`, `handle_remove_callback`,
  `handle_recover_callback`, `handle_reassign_folder_callback`,
  `handle_reassign_editor_callback`, `handle_review_callback`,
  `handle_count_choice_callback`, `handle_override_callback` (lines
  ~971-2501). These only fire from inline keyboard taps in Telegram messages
  that no longer get sent.

- [ ] **4. Remove the review-finalization helpers only used by those handlers**
  — `load_pending_reviews`, `get_pending_review`, `resolve_pending_review`,
  `finalize_notion_delivery`, `enqueue_discord_finalize` (lines 1876-2072).
  Need to confirm: does Discord's own `/allapproved` premium-review flow in
  `discord_bot.py` already do its own finalize logic independently? (Likely
  yes — worth a quick check before deleting so we don't lose the only
  finalize path.)

- [ ] **5. Strip the Telegram-send portions out of the 2 functions
  `gdrive_watcher.py` actually calls** — `send_new_folder_notification` and
  `send_folder_update_notification`. Remove `_send_telegram()` calls,
  `build_folder_notification_message`, `build_folder_keyboard`, the
  `pending.json` / `pending_folders.json` message-id bookkeeping tied to
  Telegram. Keep: Active Queue row creation, project number assignment,
  `enqueue_ops_assign_request` (Discord), deadline-writing, the auto-assign
  path's Discord enqueue calls.
  ⚠️ Found a real gap while mapping this: the "folder updated but still
  unassigned" branch of `send_folder_update_notification` currently *only*
  notifies via Telegram — there's no Discord enqueue for that case at all.
  Once Telegram is gone, an unassigned folder getting more videos dropped in
  will notify nobody. Need a decision: add a Discord ping for that case, or
  accept the gap (e.g. dashboard/`/pending` covers it).

- [ ] **6. Remove now-orphaned pending/dedup state helpers** —
  `load_pending`/`save_pending`/`add_pending`/`get_pending_item`/
  `remove_pending` (pending.json, Telegram callback-key tracking) and
  `load_pending_folders`/`save_pending_folders` (pending_folders.json,
  Telegram message-id + dedup) IF nothing after step 5 still needs them.
  Confirmed: no other file reads `pending_folders.json` or `pending.json`.

- [ ] **7. Remove `add_ignored_folder`/`remove_ignored_folder`** (only used by
  the Telegram ignore/unignore callbacks). Keep `load_ignored_folders`,
  `save_ignored_folders`, `is_folder_ignored` — `gdrive_watcher.py` needs
  `is_folder_ignored`, and `save_ignored_folders` may still be needed
  internally by it. Note: `discord_bot.py` already has its own
  `_load_ignored_folder_ids`/`_add_ignored_folder_id` reading the same
  `ignored_folders.json` — that duplication is a separate cleanup, not in
  scope here unless you want it.

- [ ] **8. Final pass** — remove now-unused imports (`InlineKeyboardButton`,
  `InlineKeyboardMarkup`, `threading` if only used for the bot warmup thread,
  etc), re-run `python3 -m py_compile notion_bridge.py`, and do one live
  `gdrive_watcher.py` run to confirm folder detection + assignment still
  works end to end.

Expected outcome: `notion_bridge.py` drops from ~2856 lines to roughly
800-900 lines of pure Notion/Drive/Discord-queue helper functions, all of
which are real and still in use.

# CC Video Manager — Feature Recommendations

Features are rated on **Value** (impact on daily ops) and **Effort** (implementation complexity given existing architecture).

---

## Done

| Feature | Shipped |
|---------|---------|
| Auto Token Refresh | `b1f1d67` — health_monitor proactively refreshes 1hr before expiry |
| Deadline Tracking | `b1f1d67` — 24hr deadlines, `/stats` shows time remaining, 6hr ping, `/extend` |
| Reassignment Flow | `b1f1d67` — `/reassign` in Discord + Telegram inline keyboard |

---

## Tier 1 — High Value, Low Effort (Build Next)

### 1. Editor Availability / Time-Off
**Value: High | Effort: Low**

Add an `Available` checkbox property to the Editor Profiles Notion DB. A `/unavailable` and `/available` Discord slash command toggles it. The Telegram assignment keyboard already filters by capacity — filtering out unavailable editors uses the same logic.

This prevents Vex from accidentally assigning to an editor who is offline/unavailable without any visible signal.

**Verdict: Build this. Simple flag with clear operational benefit.**

---

## Tier 2 — Medium Value, Medium Effort (Build Next)

### 2. Revision Request Flow
**Value: High | Effort: Medium**

Add a **Request Revision** button to the Telegram review/approval message. On tap:
- Sets Notion Active Queue status back to `In Progress`
- Prompts Vex for revision notes (single follow-up message)
- Writes a new Discord notification to the editor's channel with the revision notes
- Tracks `Revision Count` (new integer property in Active Queue)

The Notion API supports writable review/verification status and all standard property types. The revision flow reuses `discord_queue.json` for the notification. The main added effort is the multi-step Telegram conversation state for entering revision notes.

**Verdict: Worth building. Closes a real gap in the post-delivery loop.**

---

### 3. Dashboard Enhancements
**Value: Medium | Effort: Medium**

`dashboard.py` already runs on port 8080. Concrete additions worth adding:
- **Live queue table** — all Active Queue entries with status, editor, time-since-assigned, and due date
- **Editor load bar chart** — current assignments vs capacity per editor
- **Delivery velocity** — videos delivered per day over the last 30 days (from Delivery History DB)

All data is already in Notion and fetchable via existing API patterns. A simple Chart.js frontend on the Flask app would cover this with no new dependencies.

**Verdict: Worth building, especially for managing 22+ clients at a glance.**

---

### 4. Rush / Priority Flag
**Value: Medium | Effort: Low**

Add a **Rush** toggle button to new folder Telegram notifications. Sets a `Priority` select property in Notion (`Normal` / `Rush`). Rush jobs:
- Show a warning emoji in the Discord assignment embed
- Sort to top in `/pending` output
- Get a shorter default deadline (e.g., 24hrs instead of 48hrs)

One new Notion property + one new Telegram callback handler.

**Verdict: Useful for a high-volume client operation. Low cost to add.**

---

### 5. Per-Client Capacity Caps
**Value: Medium | Effort: Low**

Add an optional `max_concurrent` field per client in `clients.json` (or a Notion property in Creator Assignments). During assignment, if the client already has N folders in progress, show a warning (similar to the existing editor over-capacity warning). Does not block assignment — just surfaces the constraint to Vex.

**Verdict: Low effort, prevents one specific overload pattern.**

---

## Tier 3 — Lower Priority / Not Worth It Now

### 6. Batch Assignment for Multi-Folder Clients
**Value: Low | Effort: High**

Would require the watcher to buffer new folder detections over a time window and group them before notifying. Conflicts with the current webhook-triggered architecture where notifications fire immediately. Introduces timing complexity, state management for buffered folders, and edge cases when folders arrive from different clients in the same window.

**Verdict: Skip. The operational benefit is minor and the implementation complexity is high.**

---

### 7. Slack / Email Digest
**Value: Low | Effort: Medium**

The daily Telegram summary at 11PM IST already covers this use case for Vex. An email digest would only add value if clients need visibility — but clients aren't currently in this notification loop at all, and adding them requires a new access control layer.

**Verdict: Skip for now. Revisit only if clients request progress visibility.**

---

## Build Order Summary

| Priority | Feature | Why |
|----------|---------|-----|
| 1 | ~~Auto Token Refresh~~ ✅ | Done |
| 2 | ~~Deadline Tracking~~ ✅ | Done |
| 3 | ~~Reassignment Flow~~ ✅ | Done |
| 4 | Editor Availability | Prevents silent mis-assignment |
| 5 | Revision Request Flow | Closes post-delivery loop |
| 6 | Dashboard Enhancements | Visibility at scale |
| 7 | Rush Flag | Useful, low cost |
| 8 | Per-Client Caps | Niche but easy |
| — | Batch Assignment | Skip — high effort, low gain |
| — | Email Digest | Skip — redundant with Telegram |

# CC Video Manager — Feature Recommendations

Features are rated on **Value** (impact on daily ops) and **Effort** (implementation complexity given existing architecture).

---

## Tier 1 — High Value, Low Effort (Build These First)

### 1. Auto Token Refresh Before Expiry
**Value: Critical | Effort: Low**

The `google-auth` Python library already supports proactive refresh via `credentials.expired` and `google.auth.transport.requests.Request()`. The current setup requires manual `reauth.py` intervention when the token expires, which causes downtime. Adding a check in `health_monitor.py` or directly in the Drive API wrapper to refresh 1hr before expiry eliminates this entirely — no user interaction needed.

- Refresh tokens never expire; only access tokens (1hr lifetime) do
- `google-auth` handles the refresh automatically if you call `credentials.refresh(Request())` before making API calls
- Already confirmed feasible by Google's official Python client library docs

**Verdict: Build this. It's a reliability fix more than a feature.**

---

### 2. Editor Deadline Tracking
**Value: High | Effort: Low**

Add a `Due Date` date property to the Active Queue Notion database. Set it at assignment time (e.g., now + 48hrs, configurable per client). Surface it in:
- The Discord assignment embed
- `/stats` output (shows time remaining)
- `unassigned_reminder.py` style escalation for overdue assignments

The Notion API fully supports date properties and filtering by date. The existing assignment flow in `notion_bridge.py` already PATCHes the Active Queue on assignment — adding one more field is trivial.

**Verdict: Build this. Direct impact on accountability and turnaround tracking.**

---

### 3. Reassignment Flow
**Value: High | Effort: Low**

Add a **Reassign** inline button to the Telegram assignment message (stored in `assignment_messages.json`). Tapping it:
1. Clears the current editor assignment in Notion
2. Re-shows the editor selection keyboard
3. Writes a new job to `discord_queue.json` for the new editor
4. Updates the original Discord embed via `assignment_messages.json`

This mirrors the existing initial assignment flow almost exactly. The Telegram inline button callback pattern is already in `notion_bridge.py`.

**Verdict: Build this. Removing a manual intervention point in a 5-editor team is high leverage.**

---

### 4. Editor Availability / Time-Off
**Value: High | Effort: Low**

Add an `Available` checkbox property to the Editor Profiles Notion DB. A `/unavailable` and `/available` Discord slash command toggles it. The Telegram assignment keyboard already filters by capacity — filtering out unavailable editors uses the same logic.

This prevents Vex from accidentally assigning to an editor who is offline/unavailable without any visible signal.

**Verdict: Build this. Simple flag with clear operational benefit.**

---

## Tier 2 — Medium Value, Medium Effort (Build Next)

### 5. Revision Request Flow
**Value: High | Effort: Medium**

Add a **Request Revision** button to the Telegram review/approval message. On tap:
- Sets Notion Active Queue status back to `In Progress`
- Prompts Vex for revision notes (single follow-up message)
- Writes a new Discord notification to the editor's channel with the revision notes
- Tracks `Revision Count` (new integer property in Active Queue)

The Notion API (as of April 2026) supports writable review/verification status and all standard property types. The revision flow reuses `discord_queue.json` for the notification. The main added effort is the multi-step Telegram conversation state for entering revision notes.

**Verdict: Worth building. Closes a real gap in the post-delivery loop.**

---

### 6. Dashboard Enhancements
**Value: Medium | Effort: Medium**

`dashboard.py` already runs on port 8080 but its scope isn't documented. Concrete additions worth adding:
- **Live queue table** — all Active Queue entries with status, editor, time-since-assigned, and due date
- **Editor load bar chart** — current assignments vs capacity per editor
- **Delivery velocity** — videos delivered per day over the last 30 days (from Delivery History DB)

All data is already in Notion and fetchable via existing API patterns in the codebase. A simple Chart.js frontend on the Flask app would cover this with no new dependencies.

**Verdict: Worth building, especially for managing 22+ clients at a glance.**

---

### 7. Rush / Priority Flag
**Value: Medium | Effort: Low**

Add a **Rush** toggle button to new folder Telegram notifications. Sets a `Priority` select property in Notion (`Normal` / `Rush`). Rush jobs:
- Show a warning emoji in the Discord assignment embed
- Sort to top in `/pending` output
- Get a shorter default deadline (e.g., 24hrs instead of 48hrs)

One new Notion property + one new Telegram callback handler.

**Verdict: Useful for a high-volume client operation. Low cost to add.**

---

### 8. Per-Client Capacity Caps
**Value: Medium | Effort: Low**

Add an optional `max_concurrent` field per client in `clients.json` (or a Notion property in Creator Assignments). During assignment, if the client already has N folders in progress, show a warning (similar to the existing editor over-capacity warning). Does not block assignment — just surfaces the constraint to Vex.

**Verdict: Low effort, prevents one specific overload pattern.**

---

## Tier 3 — Lower Priority / Not Worth It Now

### 9. Batch Assignment for Multi-Folder Clients
**Value: Low | Effort: High**

Would require the watcher to buffer new folder detections over a time window and group them before notifying. This conflicts with the current webhook-triggered architecture where notifications fire immediately. Introduces timing complexity (how long to wait?), state management for buffered folders, and edge cases when folders arrive from different clients in the same window.

**Verdict: Skip. The operational benefit is minor (less Telegram noise) and the implementation complexity is high relative to the gain.**

---

### 10. Slack / Email Digest
**Value: Low | Effort: Medium**

The daily Telegram summary at 11PM IST already covers this use case for Vex. An email digest would only add value if clients need visibility — but clients aren't currently in this notification loop at all, and adding them requires a new access control layer (what to share, with whom).

**Verdict: Skip for now. Revisit only if clients request progress visibility.**

---

## Build Order Summary

| Priority | Feature | Why |
|----------|---------|-----|
| 1 | Auto Token Refresh | Reliability fix, eliminates downtime |
| 2 | Deadline Tracking | Core ops gap, low effort |
| 3 | Reassignment Flow | Removes manual intervention |
| 4 | Editor Availability | Prevents silent mis-assignment |
| 5 | Revision Request Flow | Closes post-delivery loop |
| 6 | Dashboard Enhancements | Visibility at scale |
| 7 | Rush Flag | Useful, low cost |
| 8 | Per-Client Caps | Niche but easy |
| — | Batch Assignment | Skip — high effort, low gain |
| — | Email Digest | Skip — redundant with Telegram |

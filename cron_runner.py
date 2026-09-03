#!/usr/bin/env python3
"""The box's crontab, as a process.

Every scheduled job used to be a crontab line on the machine the bot ran on.
That machine is gone, and Railway's own cron services can't replace it one-for-
one: a volume attaches to exactly ONE service, and these jobs share state with
the bot (`editor_counters.json`, `deadlines.json`, `schedule_cache.json`).
Split across services they'd each get their own disk and silently disagree.
So they run in the bot's container, against the bot's disk, exactly as they
did on the box.

Started by railway_boot.py alongside the bot. Each job is a subprocess, so a
job that throws can't take the gateway down with it, and a job that hangs only
blocks its own next run.

Schedules are the ones documented in CLAUDE.md (which supersedes the crontab
block in README.md — that one still lists `daily_summary.py`, dead since
2026-08, and the pre-2026-08 Sunday reset time).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from croniter import croniter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "cron_state.json")
TICK_SECONDS = 30

# (script, cron expression in UTC, run once at boot too)
#
# run_at_boot is for the one job where being late is worse than being early:
# refresh_schedule_cache just repopulates a cache the bot reads. Everything
# else waits for its slot — a boot-time catch-up burst is how you get eleven
# digests at once after a redeploy.
JOBS = [
    ("snapshot_editor_state.py",  "0 * * * *",    False),  # hourly
    ("refresh_schedule_cache.py", "0 */2 * * *",  True),   # every 2h
    ("daily_digest.py",           "30 3 * * *",   False),  # 03:30 UTC
    ("cantina_daily_reminder.py", "30 3 * * *",   False),  # 03:30 UTC
    ("daily_status_update.py",    "30 17 * * *",  False),  # 17:30 UTC
    ("sanity_checker.py",         "0 20 * * *",   False),  # 20:00 UTC nightly
    ("weekly_leaderboard_post.py","30 15 * * 6",  False),  # Sat 15:30 UTC, before the reset
    ("reset_weekly.py",           "0 0 * * 0",    False),  # Sun 00:00 UTC (CLAUDE.md 2026-08)
    ("reset_monthly.py",          "30 18 1 * *",  False),  # 1st of the month
]


def log(msg):
    print(f"[cron] {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    # Write through the SYMLINK, not over it. railway_boot links every state
    # file out to the volume, and os.replace(tmp, link) drops a regular file on
    # top of the link — after the first save this map was living in the
    # container again and every deploy read "first boot". Resolving first keeps
    # the atomic swap AND the link.
    target = os.path.realpath(STATE_FILE)
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, target)


def run(script):
    started = time.time()
    log(f"{script}: start")
    try:
        p = subprocess.run(
            [sys.executable, os.path.join(BASE_DIR, script)],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=15 * 60,
        )
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        note = tail[-1][:200] if tail else ""
        log(f"{script}: exit {p.returncode} in {time.time() - started:.0f}s {note}")
    except subprocess.TimeoutExpired:
        log(f"{script}: TIMED OUT after 15m — killed")
    except Exception as e:
        log(f"{script}: failed to launch — {e}")


def main():
    missing = [s for s, _, _ in JOBS if not os.path.exists(os.path.join(BASE_DIR, s))]
    if missing:
        log(f"WARNING: not in this checkout, skipping: {', '.join(missing)}")
    jobs = [(s, expr, boot) for s, expr, boot in JOBS if s not in missing]

    state = load_state()
    now = datetime.now(timezone.utc)
    fresh = not state
    for script, expr, at_boot in jobs:
        if script in state:
            continue
        # First ever boot: pretend everything just ran, so nothing fires a
        # backlog. The at_boot pair still runs immediately below.
        state[script] = now.isoformat()
    save_state(state)
    log(f"{len(jobs)} jobs, tick {TICK_SECONDS}s" + (", first boot" if fresh else ""))

    for script, expr, at_boot in jobs:
        if at_boot:
            run(script)
            state[script] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    while True:
        now = datetime.now(timezone.utc)
        for script, expr, _ in jobs:
            try:
                last = datetime.fromisoformat(state[script])
            except Exception:
                last = now
            due = croniter(expr, last).get_next(datetime)
            if due <= now:
                run(script)
                state[script] = now.isoformat()
                save_state(state)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()

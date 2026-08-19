#!/usr/bin/env python3
"""Boot shim for hosts with an ephemeral filesystem (Railway, Fly, any PaaS).

The bot was written for a box it owned: secrets sat next to the code and every
state file was written in place. Neither survives a PaaS — the repo has no
secrets in it (they're gitignored, correctly) and the disk is wiped on every
deploy. This puts both back before the bot starts:

  1. secrets  — config.json / token.json / credentials.json are written from
     env vars (CC_CONFIG_JSON, CC_TOKEN_JSON, CC_CREDENTIALS_JSON) when the
     file isn't already there. Existing files always win, so running this on
     the old box or a laptop changes nothing.
  2. state    — with a volume mounted (RAILWAY_VOLUME_MOUNT_PATH, or STATE_DIR
     for any other host) every runtime json is symlinked out to the volume, so
     a redeploy doesn't reset counters, deadlines, or the pending queues. The
     symlink means the bot's own paths are untouched: it still opens
     BASE_DIR/deadlines.json and never knows.

Run it instead of the service: `python railway_boot.py discord_bot.py`.
Defaults to discord_bot.py so the start command can just be `python
railway_boot.py`.
"""
import json
import os
import runpy
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# env var -> file it writes. Values are the file's whole contents as JSON.
SECRETS = {
    "CC_CONFIG_JSON": "config.json",
    "CC_TOKEN_JSON": "token.json",
    "CC_CREDENTIALS_JSON": "credentials.json",
}

# Every gitignored runtime json the services read AND write. Anything missing
# here silently resets on each deploy, so add new state files as they appear.
STATE_FILES = [
    "assignment_messages.json",
    "clients.json",
    "dashboard_batches.json",
    "deadlines.json",
    "delivery_meta.json",
    "discord_queue.json",
    "drive_webhook_last_ping.json",
    "edited_files.json",
    "editor_counters.json",
    "editor_counters_history.json",
    "editor_state_history.jsonl",
    "folder_update_msgs.json",
    "ignored_folders.json",
    "page_token.json",
    "pending_assignments.json",
    "pending_dashboard_pushes.json",
    "pending_folders.json",
    "pending_ops_assigns.json",
    "pending_reviews.json",
    "pending_status_check2.json",
    "project_numbers.json",
    "removed_folders.json",
    "schedule_cache.json",
    "stale_assignments.json",
    "watch_channel.json",
    "watched_files.json",
]


def write_secrets():
    for env_key, name in SECRETS.items():
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            print(f"[boot] {name}: already on disk, left alone")
            continue
        raw = os.environ.get(env_key)
        if not raw:
            print(f"[boot] {name}: MISSING — set {env_key}")
            continue
        try:
            json.loads(raw)  # fail loudly here, not 200 lines into the bot
        except Exception as e:
            raise SystemExit(f"[boot] {env_key} is not valid JSON: {e}")
        with open(path, "w") as f:
            f.write(raw)
        os.chmod(path, 0o600)
        print(f"[boot] {name}: written from {env_key}")


def link_state():
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("STATE_DIR")
    if not vol:
        print("[boot] no volume mounted — state files are EPHEMERAL, they reset on redeploy")
        return
    os.makedirs(vol, exist_ok=True)
    for name in STATE_FILES:
        local = os.path.join(BASE_DIR, name)
        kept = os.path.join(vol, name)
        if os.path.islink(local) and os.path.realpath(local) == os.path.realpath(kept):
            continue
        # First boot: seed the volume from whatever shipped in the image.
        if not os.path.exists(kept) and os.path.exists(local) and not os.path.islink(local):
            shutil.copy2(local, kept)
        if os.path.exists(local) or os.path.islink(local):
            os.remove(local)
        os.symlink(kept, local)
    print(f"[boot] state linked to {vol} ({len(STATE_FILES)} files)")


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "discord_bot.py"
    write_secrets()
    link_state()
    print(f"[boot] starting {target}")
    sys.argv = [target] + sys.argv[2:]
    runpy.run_path(os.path.join(BASE_DIR, target), run_name="__main__")


if __name__ == "__main__":
    main()

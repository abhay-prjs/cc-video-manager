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
import atexit
import json
import os
import runpy
import shutil
import subprocess
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
    "cron_state.json",
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
    # Every secret that couldn't be produced. Collected rather than raised one
    # at a time, so the first boot on a fresh host names ALL the missing vars
    # instead of making you fix them one deploy each.
    missing = []
    for env_key, name in SECRETS.items():
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            print(f"[boot] {name}: already on disk, left alone", flush=True)
            continue
        raw = os.environ.get(env_key)
        if not raw:
            print(f"[boot] {name}: MISSING — set {env_key}", flush=True)
            missing.append(env_key)
            continue
        try:
            json.loads(raw)  # fail loudly here, not 200 lines into the bot
        except Exception as e:
            raise SystemExit(f"[boot] {env_key} is not valid JSON: {e}")
        with open(path, "w") as f:
            f.write(raw)
        os.chmod(path, 0o600)
        print(f"[boot] {name}: written from {env_key}", flush=True)
    if missing:
        # Without this the bot starts anyway and dies on its own open() five
        # frames deep, and the restart policy replays that traceback ten times
        # — the actual answer ("set these variables") scrolls away. Stop here.
        raise SystemExit(
            "[boot] cannot start: "
            + ", ".join(missing)
            + " not set. Paste each file's contents into that variable "
              "(Railway → service → Variables), then redeploy."
        )


def link_logs(vol):
    """logs/ onto the volume too.

    logger_setup writes DEBUG+ with 7-day rotation, which was true on the box
    and a lie here: the container disk is wiped on every redeploy, so the file
    an error message promises ("this has been logged") was usually gone before
    anyone looked. Railway's own log view is stdout only, and a short window of
    it. Symlink the directory, so the rotation survives deploys.
    """
    kept = os.path.join(vol, "logs")
    local = os.path.join(BASE_DIR, "logs")
    os.makedirs(kept, exist_ok=True)
    if os.path.islink(local) and os.path.realpath(local) == os.path.realpath(kept):
        return
    if os.path.isdir(local) and not os.path.islink(local):
        for name in os.listdir(local):
            src, dst = os.path.join(local, name), os.path.join(kept, name)
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
        shutil.rmtree(local)
    elif os.path.islink(local):
        os.remove(local)
    os.symlink(kept, local)
    print(f"[boot] logs kept on {kept}", flush=True)


def link_state():
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("STATE_DIR")
    if not vol:
        # Loud, but not fatal: the bot runs fine without a volume, it just
        # forgets its counters, deadlines and pending queues on every deploy.
        print("[boot] WARNING: no volume mounted — state files are EPHEMERAL and reset on every", flush=True)
        print("[boot] WARNING: redeploy (counters, deadlines, pending assign cards). Mount one", flush=True)
        print("[boot] WARNING: at /data, or set STATE_DIR, to keep them.", flush=True)
        return
    os.makedirs(vol, exist_ok=True)
    link_logs(vol)
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
    print(f"[boot] state linked to {vol} ({len(STATE_FILES)} files)", flush=True)


def start_crons():
    """The crontab lives in this container (cron_runner.py) because a Railway
    volume attaches to one service only, and every job shares the bot's state
    files. Off by default so a laptop run doesn't fire real digests and resets;
    Railway turns it on by setting CC_RUN_CRONS=1."""
    if os.environ.get("CC_RUN_CRONS", "").strip() not in ("1", "true", "yes"):
        print("[boot] crons off (set CC_RUN_CRONS=1 to run the schedule here)", flush=True)
        return
    proc = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "cron_runner.py")])
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    print(f"[boot] cron_runner started (pid {proc.pid})", flush=True)


def start_webhook():
    """Drive's push notifications need a public URL to POST to. The box had an
    ngrok tunnel; here it's the service's own domain, so the Flask receiver
    runs in this container too and binds $PORT. Same volume, so the watcher it
    spawns shares page_token.json with everything else.

    Off unless CC_RUN_WEBHOOK=1: only the host holding the public domain should
    answer, and register_watch points Drive at exactly one address."""
    if os.environ.get("CC_RUN_WEBHOOK", "").strip() not in ("1", "true", "yes"):
        print("[boot] drive webhook off (set CC_RUN_WEBHOOK=1 on the host with the domain)", flush=True)
        return
    proc = subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "drive_webhook.py")])
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    print(f"[boot] drive_webhook started on port {os.environ.get('PORT', '8081')} (pid {proc.pid})", flush=True)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "discord_bot.py"
    write_secrets()
    link_state()
    start_webhook()
    start_crons()
    print(f"[boot] starting {target}", flush=True)
    sys.argv = [target] + sys.argv[2:]
    runpy.run_path(os.path.join(BASE_DIR, target), run_name="__main__")


if __name__ == "__main__":
    main()

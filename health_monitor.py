"""
health_monitor.py
Checks system health every 30 minutes (via cron).
Alerts Telegram on: expired Drive token, stale webhook ping, expiring watch, dead services, log errors.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR        = Path("/home/ubuntu/gdrive_watcher")
CONFIG_FILE     = BASE_DIR / "config.json"
TOKEN_FILE      = BASE_DIR / "token.json"
WATCH_FILE      = BASE_DIR / "watch_channel.json"
LAST_PING_FILE  = BASE_DIR / "drive_webhook_last_ping.json"
LOGS_DIR        = BASE_DIR / "logs"

SERVICES = [
    "notion-bridge",
    "discord-bot",
    "drive-webhook",
    "gdrive-dashboard",
    "ngrok-webhook",
]


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def send_telegram_alert(config, message):
    token   = config.get("notion_bridge_token") or config.get("telegram_token")
    chat_id = config.get("notion_bridge_chat_id") or config.get("chat_id")
    if not token or not chat_id:
        print(f"[ALERT] {message}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram alert failed: {e}")


# ── A. Token Health ────────────────────────────────────────────────────────────

def check_token_health(config):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GRequest
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), ["https://www.googleapis.com/auth/drive"])
        if creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
        service = build("drive", "v3", credentials=creds)
        service.files().list(pageSize=1, supportsAllDrives=True).execute()
        print("Token health: OK")
    except Exception as e:
        err = str(e).lower()
        if "invalid_grant" in err or "token has been expired or revoked" in err:
            send_telegram_alert(config,
                "🔴 DRIVE TOKEN EXPIRED — Re-authenticate immediately!\n"
                "Run: cd /home/ubuntu/gdrive_watcher && python3 reauth.py"
            )
            print(f"Token health: EXPIRED — {e}")
        else:
            print(f"Token health check error (non-auth): {e}")


# ── B. Webhook Health ──────────────────────────────────────────────────────────

def check_webhook_health(config):
    if not LAST_PING_FILE.exists():
        send_telegram_alert(config,
            "⚠️ Drive webhook has never received a ping\n"
            "Check: ngrok tunnel, Drive watch registration"
        )
        print("Webhook health: no ping file found")
        return

    data      = json.loads(LAST_PING_FILE.read_text())
    last_ping = data.get("last_ping", 0)
    elapsed   = time.time() - last_ping
    if elapsed > 3 * 3600:
        last_time = datetime.fromtimestamp(last_ping, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        send_telegram_alert(config,
            f"⚠️ Drive webhook hasn't received any pings in 3+ hours\n"
            f"Last ping: {last_time}\n"
            f"Check: ngrok tunnel, Drive watch registration"
        )
        print(f"Webhook health: stale ({elapsed/3600:.1f}h since last ping)")
    else:
        print(f"Webhook health: OK ({elapsed/60:.0f}m since last ping)")


# ── C. Watch Expiry ────────────────────────────────────────────────────────────

def check_watch_expiry(config):
    if not WATCH_FILE.exists():
        print("Watch expiry: no watch_channel.json found")
        return

    data       = json.loads(WATCH_FILE.read_text())
    expiration = data.get("expiration")
    if not expiration:
        print("Watch expiry: no expiration field in watch_channel.json")
        return

    exp_ts   = int(expiration) / 1000  # milliseconds → seconds
    now_ts   = time.time()
    remaining = exp_ts - now_ts

    if remaining < 2 * 3600:
        hours = remaining / 3600
        send_telegram_alert(config,
            f"⚠️ Drive watch expires in {hours:.1f}h — re-registering now\n"
            f"Running register_watch.py..."
        )
        print(f"Watch expiry: expiring soon ({hours:.1f}h) — running register_watch.py")
        try:
            subprocess.run(
                ["python3", str(BASE_DIR / "register_watch.py")],
                timeout=60,
                capture_output=True,
            )
            print("register_watch.py completed")
        except Exception as e:
            print(f"register_watch.py failed: {e}")
    else:
        print(f"Watch expiry: OK ({remaining/3600:.1f}h remaining)")


# ── D. Service Health ──────────────────────────────────────────────────────────

def check_service_health(config):
    for svc in SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            status = result.stdout.strip()
            if status != "active":
                send_telegram_alert(config,
                    f"🔴 Service DOWN: {svc} (status: {status}) — attempting restart"
                )
                print(f"Service {svc}: {status} — restarting")
                subprocess.run(["systemctl", "restart", svc], timeout=15)
            else:
                print(f"Service {svc}: active")
        except Exception as e:
            print(f"Service check failed for {svc}: {e}")


# ── E. Log Error Monitor ───────────────────────────────────────────────────────

def check_log_errors(config):
    log_files = {
        "discord_bot":    LOGS_DIR / "discord_bot.log",
        "notion_bridge":  LOGS_DIR / "notion_bridge.log",
    }
    cutoff = time.time() - 30 * 60  # last 30 minutes

    for service_name, log_path in log_files.items():
        if not log_path.exists():
            continue
        try:
            with open(log_path) as f:
                lines = f.readlines()

            error_lines = []
            last_error  = ""
            for line in lines:
                if " | ERROR" not in line and "ERROR" not in line:
                    continue
                # Parse timestamp from log line (format: "YYYY-MM-DD HH:MM:SS,mmm | ...")
                try:
                    ts_str   = line.split(" | ")[0].strip()
                    ts       = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").timestamp()
                    if ts >= cutoff:
                        error_lines.append(line.rstrip())
                        last_error = line.rstrip()
                except Exception:
                    pass  # can't parse timestamp, skip

            count = len(error_lines)
            if count > 5:
                send_telegram_alert(config,
                    f"⚠️ High error rate in {service_name}: {count} errors in last 30 mins\n"
                    f"Latest: {last_error[-200:]}"
                )
                print(f"Log errors for {service_name}: {count} errors in last 30m")
            else:
                print(f"Log errors for {service_name}: {count} (OK)")
        except Exception as e:
            print(f"Log check failed for {service_name}: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"=== Health Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    try:
        config = load_config()
    except Exception as e:
        print(f"Failed to load config: {e}")
        return

    check_token_health(config)
    check_webhook_health(config)
    check_watch_expiry(config)
    check_service_health(config)
    check_log_errors(config)
    print("=== Health check complete ===")


if __name__ == "__main__":
    main()

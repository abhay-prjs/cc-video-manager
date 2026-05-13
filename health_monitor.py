"""
health_monitor.py
Checks system health every 30 minutes (via cron).
Alerts Discord first; falls back to Telegram if Discord is unreachable.
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

DISCORD_ALERT_CHANNEL = "1503993880717299723"

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


def send_alert(config, message):
    """Send to Discord; fall back to Telegram if Discord fails."""
    discord_token = config.get("discord_bot_token")
    discord_ok = False

    if discord_token:
        try:
            resp = requests.post(
                f"https://discord.com/api/v10/channels/{DISCORD_ALERT_CHANNEL}/messages",
                headers={"Authorization": f"Bot {discord_token}", "Content-Type": "application/json"},
                json={"content": message},
                timeout=10,
            )
            discord_ok = resp.status_code in (200, 201)
        except Exception as e:
            print(f"Discord alert failed: {e}")

    if not discord_ok:
        # Discord is down or failed — notify via Telegram
        tg_token = config.get("notion_bridge_token") or config.get("telegram_token")
        chat_id  = config.get("notion_bridge_chat_id") or config.get("chat_id")
        if tg_token and chat_id:
            try:
                tg_message = f"⚠️ Discord unreachable — alert via Telegram:\n{message}"
                requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": chat_id, "text": tg_message},
                    timeout=10,
                )
            except Exception as e:
                print(f"Telegram fallback failed: {e}")


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
            send_alert(config,
                "🔴 DRIVE TOKEN EXPIRED — run `python3 reauth.py` immediately"
            )
            print(f"Token health: EXPIRED — {e}")
        else:
            print(f"Token health check error (non-auth): {e}")


# ── B. Webhook Health ──────────────────────────────────────────────────────────

def check_webhook_health(config):
    if not LAST_PING_FILE.exists():
        send_alert(config, "⚠️ Drive webhook: no pings ever received — check ngrok + Drive watch")
        print("Webhook health: no ping file found")
        return

    data      = json.loads(LAST_PING_FILE.read_text())
    last_ping = data.get("last_ping", 0)
    elapsed   = time.time() - last_ping
    hours     = elapsed / 3600

    # Only alert after 6h (not 3h) — quiet periods are normal when no folders are added
    if hours > 6:
        send_alert(config, f"⚠️ Drive webhook: no ping in {hours:.0f}h — check ngrok tunnel")
        print(f"Webhook health: stale ({hours:.1f}h since last ping)")
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

    exp_ts    = int(expiration) / 1000
    remaining = exp_ts - time.time()

    if remaining < 2 * 3600:
        hours = remaining / 3600
        send_alert(config, f"⚠️ Drive watch expires in {hours:.1f}h — re-registering now")
        print(f"Watch expiry: expiring soon ({hours:.1f}h) — running register_watch.py")
        try:
            subprocess.run(
                ["python3", str(BASE_DIR / "register_watch.py")],
                timeout=60, capture_output=True,
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
            if status == "active":
                print(f"Service {svc}: active")
                continue

            # Wait 5s and re-check before alerting — catches transient deactivating state
            time.sleep(5)
            result2 = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=5,
            )
            status2 = result2.stdout.strip()
            if status2 != "active":
                if svc == "discord-bot":
                    # Discord itself is down — this will be the fallback alert
                    send_alert(config, f"🔴 {svc} is DOWN (status: {status2}) — restarting")
                else:
                    send_alert(config, f"🔴 {svc} is DOWN (status: {status2}) — restarting")
                print(f"Service {svc}: {status2} — restarting")
                subprocess.run(["systemctl", "restart", svc], timeout=15)
            else:
                print(f"Service {svc}: recovered on recheck (was {status})")
        except Exception as e:
            print(f"Service check failed for {svc}: {e}")


# ── E. Log Error Monitor ───────────────────────────────────────────────────────

def check_log_errors(config):
    log_files = {
        "discord-bot":    LOGS_DIR / "discord_bot.log",
        "notion-bridge":  LOGS_DIR / "notion_bridge.log",
    }
    cutoff = time.time() - 30 * 60

    for service_name, log_path in log_files.items():
        if not log_path.exists():
            continue
        try:
            with open(log_path) as f:
                lines = f.readlines()

            error_lines = []
            last_error  = ""
            for line in lines:
                if "ERROR" not in line:
                    continue
                try:
                    ts_str = line.split(" | ")[0].strip()
                    ts     = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").timestamp()
                    if ts >= cutoff:
                        error_lines.append(line.rstrip())
                        last_error = line.rstrip()
                except Exception:
                    pass

            count = len(error_lines)
            if count > 5:
                send_alert(config,
                    f"⚠️ {service_name}: {count} errors in last 30min — `{last_error[-150:]}`"
                )
                print(f"Log errors for {service_name}: {count} in last 30m")
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

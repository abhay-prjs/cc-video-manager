"""
reauth.py
Guided re-authentication script for Google Drive OAuth.
Runs the OOB flow, saves a new token.json, restarts all services,
and sends a Telegram confirmation.
"""

import json
import os
import subprocess
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials

BASE_DIR       = Path("/home/ubuntu/gdrive_watcher")
CREDENTIALS    = BASE_DIR / "credentials.json"
TOKEN_FILE     = BASE_DIR / "token.json"
CONFIG_FILE    = BASE_DIR / "config.json"
SCOPES         = ["https://www.googleapis.com/auth/drive"]

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


def send_telegram(config, message):
    token   = config.get("notion_bridge_token") or config.get("telegram_token")
    chat_id = config.get("notion_bridge_chat_id") or config.get("chat_id")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")


def main():
    print("=== Google Drive Re-Authentication ===\n")

    if not CREDENTIALS.exists():
        print(f"ERROR: credentials.json not found at {CREDENTIALS}")
        return

    print("Starting OAuth flow (OOB/manual copy mode)...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent")

    print(f"\nOpen this URL in your browser:\n{auth_url}\n")
    code = input("Paste the authorization code here: ").strip()

    print("\nFetching token...")
    flow.fetch_token(code=code)
    creds = flow.credentials

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"✅ New token.json saved to {TOKEN_FILE}")

    print("\nRestarting all services...")
    for svc in SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "restart", svc],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                print(f"  ✅ {svc} restarted")
            else:
                print(f"  ⚠️ {svc} restart exit code {result.returncode}: {result.stderr.strip()}")
        except Exception as e:
            print(f"  ❌ {svc} restart failed: {e}")

    print("\nSending Telegram confirmation...")
    try:
        config = load_config()
        send_telegram(config,
            "✅ Drive re-authentication complete!\n"
            "New token.json saved. All services restarted."
        )
        print("Telegram confirmation sent.")
    except Exception as e:
        print(f"Could not send Telegram confirmation: {e}")

    print("\n=== Re-authentication complete ===")


if __name__ == "__main__":
    main()

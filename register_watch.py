import json
import time
import uuid
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE_DIR = Path(__file__).parent
TOKEN_FILE = BASE_DIR / "token.json"
PAGE_TOKEN_FILE = BASE_DIR / "page_token.json"
WATCH_CHANNEL_FILE = BASE_DIR / "watch_channel.json"

WEBHOOK_URL = "https://subprime-water-overheat.ngrok-free.dev/webhook"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def load_credentials():
    return Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)


def main():
    creds = load_credentials()
    service = build("drive", "v3", credentials=creds)

    print("Getting startPageToken...")
    response = service.changes().getStartPageToken().execute()
    page_token = response.get("startPageToken")
    print(f"startPageToken: {page_token}")

    PAGE_TOKEN_FILE.write_text(json.dumps({"pageToken": page_token}, indent=2))
    print(f"Saved page token to {PAGE_TOKEN_FILE}")

    body = {
        "id": str(uuid.uuid4()),
        "type": "web_hook",
        "address": WEBHOOK_URL,
        "expiration": str(int((time.time() + 24 * 60 * 60) * 1000)),
    }

    print("Registering changes.watch...")
    watch_response = service.changes().watch(pageToken=page_token, body=body).execute()
    print(f"Watch registered: {watch_response}")

    WATCH_CHANNEL_FILE.write_text(json.dumps(watch_response, indent=2))
    print(f"Saved watch channel to {WATCH_CHANNEL_FILE}")


if __name__ == "__main__":
    main()

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
SCOPES = ["https://www.googleapis.com/auth/drive"]


def load_credentials():
    return Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)


def stop_old_channel(service):
    """Stop the previously registered watch channel so we don't accumulate stale channels."""
    if not WATCH_CHANNEL_FILE.exists():
        return
    try:
        old = json.loads(WATCH_CHANNEL_FILE.read_text())
        channel_id  = old.get("id")
        resource_id = old.get("resourceId")
        if channel_id and resource_id:
            service.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()
            print(f"Stopped old channel: {channel_id}")
    except Exception as e:
        print(f"Warning: could not stop old channel: {e}")


def get_current_page_token():
    """Return the current page token from page_token.json, or None if unavailable."""
    try:
        if PAGE_TOKEN_FILE.exists():
            data = json.loads(PAGE_TOKEN_FILE.read_text())
            token = data.get("pageToken")
            if token:
                return token
    except Exception:
        pass
    return None


def main():
    creds = load_credentials()
    service = build("drive", "v3", credentials=creds)

    # Stop the old channel before registering a new one
    stop_old_channel(service)

    # Prefer the current page token to avoid replaying already-seen changes
    page_token = get_current_page_token()
    if page_token:
        print(f"Using existing pageToken: {page_token}")
    else:
        print("Getting startPageToken (no existing token found)...")
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

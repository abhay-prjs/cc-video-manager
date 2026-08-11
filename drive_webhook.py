import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, request, jsonify
from logger_setup import get_logger
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

app = Flask(__name__)
logger = get_logger('drive_webhook')

BASE_DIR = Path("/home/ubuntu/gdrive_watcher")
WATCHER_SCRIPT = str(BASE_DIR / "gdrive_watcher.py")
TOKEN_FILE = BASE_DIR / "token.json"
PAGE_TOKEN_FILE = BASE_DIR / "page_token.json"
LAST_PING_FILE = BASE_DIR / "drive_webhook_last_ping.json"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.activity.readonly",
]
COOLDOWN_SECONDS = 5

_folder_cooldowns: dict = {}  # folder_id → last trigger timestamp
_trigger_lock = threading.Lock()


def get_service():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def run_watcher(parent_ids=None):
    """Run gdrive_watcher.py in a background thread."""
    cmd = [sys.executable, WATCHER_SCRIPT]
    if parent_ids:
        cmd += ["--parent-ids", ",".join(parent_ids)]
    proc = subprocess.Popen(cmd)
    logger.info(
        "Spawned watcher pid=%d parent_ids=%s",
        proc.pid,
        ",".join(parent_ids) if parent_ids else "full-scan",
    )
    proc.wait()
    logger.info("Watcher pid=%d finished (returncode=%d)", proc.pid, proc.returncode)


def try_trigger_watcher(parent_ids=None):
    """Per-folder cooldown check, then fire watcher in a background thread if any folder is eligible."""
    global _folder_cooldowns
    with _trigger_lock:
        now = time.time()
        if parent_ids:
            eligible = set()
            for fid in parent_ids:
                elapsed = now - _folder_cooldowns.get(fid, 0.0)
                if elapsed >= COOLDOWN_SECONDS:
                    eligible.add(fid)
                else:
                    logger.info("Folder %s cooldown active (%.1fs) — skipping", fid, elapsed)
            if not eligible:
                return
            for fid in eligible:
                _folder_cooldowns[fid] = now
            ids_to_trigger = eligible
        else:
            elapsed = now - _folder_cooldowns.get('__full__', 0.0)
            if elapsed < COOLDOWN_SECONDS:
                logger.info("Full-scan cooldown active (%.1fs) — skipping", elapsed)
                return
            _folder_cooldowns['__full__'] = now
            ids_to_trigger = None

    threading.Thread(target=run_watcher, args=(ids_to_trigger,), daemon=True).start()


@app.route("/webhook", methods=["POST"])
def webhook():
    logger.info("=== WEBHOOK HIT ===")
    logger.info("Headers: %s", dict(request.headers))
    LAST_PING_FILE.write_text(json.dumps({"last_ping": time.time(), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))

    state = request.headers.get("X-Goog-Resource-State", "")
    if state == "sync":
        return "", 200

    if state not in ("change", "add", "update"):
        return "", 200

    try:
        page_token_data = json.loads(PAGE_TOKEN_FILE.read_text())
        page_token = page_token_data.get("pageToken") or page_token_data.get("startPageToken")

        service = get_service()
        all_parent_ids = set()
        new_token = page_token

        while page_token:
            result = service.changes().list(
                pageToken=page_token,
                fields="nextPageToken,newStartPageToken,changes(fileId,file(name,parents,mimeType))",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()

            changes = result.get("changes", [])
            logger.info("Got %d changes for pageToken=%s", len(changes), page_token)

            for change in changes:
                file_meta = change.get("file") or {}
                logger.info(
                    "Change: fileId=%s name=%s parents=%s",
                    change.get("fileId"),
                    file_meta.get("name"),
                    file_meta.get("parents"),
                )
                for pid in file_meta.get("parents", []):
                    all_parent_ids.add(pid)

            new_token = result.get("newStartPageToken", new_token)
            page_token = result.get("nextPageToken")

        PAGE_TOKEN_FILE.write_text(json.dumps({"pageToken": new_token}, indent=2))
        logger.info("Updated pageToken to: %s", new_token)
        logger.info("Collected parent_ids: %s", all_parent_ids)

        if all_parent_ids:
            try_trigger_watcher(parent_ids=all_parent_ids)
        else:
            logger.info("No parent IDs found in changes — skipping watcher trigger")

    except Exception as e:
        logger.error("Error processing webhook: %s", e, exc_info=True)

    return "", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "CC Video Manager webhook alive"})


# ── Brand Deals caption copy page ────────────────────────────────────────────
# The deal tracker bot (brand_deals_bot/deal_tracker.py) publishes each deal's
# current caption to this file; the 📋 Copy caption link buttons in its DMs
# point here. This just gives Vex a one-tap clipboard copy on mobile, which
# Discord itself can't do.

CAPTIONS_FILE = Path("/home/ubuntu/brand_deals_bot/current_captions.json")


@app.route("/caption/<page_id>", methods=["GET"])
def caption_copy(page_id):
    import html as _html
    try:
        entry = json.loads(CAPTIONS_FILE.read_text()).get(page_id)
    except Exception:
        entry = None
    if not entry:
        return "Caption not found — it may have rotated. Check the latest DM from the bot.", 404
    caption = entry.get("caption", "")
    brand = entry.get("brand", "")
    cap_js = json.dumps(caption)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Copy caption — {_html.escape(brand)}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #1e1f22; color: #dbdee1; display: flex; flex-direction: column;
         min-height: 90vh; }}
  h2 {{ margin: 0 0 16px; font-size: 18px; color: #949ba4; font-weight: 600; }}
  pre {{ background: #2b2d31; border-radius: 12px; padding: 16px; white-space: pre-wrap;
         word-wrap: break-word; font-family: inherit; font-size: 16px; flex: 1; }}
  button {{ background: #5865f2; color: #fff; border: 0; border-radius: 12px;
            padding: 20px; font-size: 20px; font-weight: 700; width: 100%;
            position: sticky; bottom: 16px; }}
  button.ok {{ background: #23a55a; }}
</style></head><body>
<h2>📦 {_html.escape(brand)}</h2>
<pre id="cap">{_html.escape(caption)}</pre>
<button id="btn" onclick="doCopy()">📋 Tap to copy caption</button>
<script>
const CAP = {cap_js};
function doCopy() {{
  const done = () => {{ const b = document.getElementById('btn');
    b.textContent = '✅ Copied — go paste it!'; b.className = 'ok'; }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(CAP).then(done).catch(fallback);
  }} else fallback();
  function fallback() {{
    const ta = document.createElement('textarea'); ta.value = CAP;
    document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); done(); }} catch (e) {{ alert('Long-press the text and copy manually'); }}
    document.body.removeChild(ta);
  }}
}}
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)

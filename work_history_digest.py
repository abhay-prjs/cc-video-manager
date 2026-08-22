"""
work_history_digest.py

Posts a running "what moved" report into the work-history channel every 6
hours. Founder, 2026-08-22: he wants to see it when something passes, without
having to open the dashboard.

The dashboard owns the data — this asks it for a window and renders the embed.
Nothing here knows about tickets, statuses or editors beyond what the JSON
says, so the site can change the shape of a batch without touching this file.

  GET {dashboard_digest_url}?hours=6   Authorization: Bearer {dashboard_secret}

Config keys (config.json):
  work_history_channel_id   the channel to post in
  dashboard_digest_url      the site's /api/discord/work-digest
  dashboard_secret          same bearer the rest of the bridge uses
  discord_bot_token         to post

Missing config = silent no-op, matching every other bridge caller: a bridge
that isn't wired should be inert, not noisy.

Sends nothing when the window is quiet — four empty embeds a day trains people
to ignore the channel, which costs more than the missing report.
"""

import json
import logging
import os
import requests
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

WINDOW_HOURS = 6
# Discord caps an embed field value at 1024. Leave room for the ellipsis.
FIELD_LIMIT = 1020
# Never list more than this per section; the count in the heading carries the
# rest. A 40-line field is not read by anyone.
MAX_LINES = 8

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f'cannot read config: {e}')
        return {}


def fetch(url, secret):
    try:
        r = requests.get(
            url,
            params={'hours': WINDOW_HOURS},
            headers={'Authorization': f'Bearer {secret}'},
            timeout=20,
        )
    except Exception as e:
        logger.warning(f'work-digest fetch failed: {e}')
        return None
    if r.status_code != 200:
        logger.warning(f'work-digest {r.status_code}: {r.text[:200]}')
        return None
    try:
        return r.json()
    except Exception as e:
        logger.warning(f'work-digest returned non-json: {e}')
        return None


def _clip(lines, total):
    """Join up to MAX_LINES, and say how many were left off."""
    shown = lines[:MAX_LINES]
    hidden = total - len(shown)
    if hidden > 0:
        shown.append(f'…and {hidden} more')
    val = '\n'.join(shown) or '—'
    return val[:FIELD_LIMIT] + ('…' if len(val) > FIELD_LIMIT else '')


def _batch_line(b, tail=''):
    who = b.get('editor') or 'nobody'
    src = ' · drive' if b.get('source') == 'drive' else ''
    vids = b.get('videos') or 0
    return (f"• {b.get('batch')} — {b.get('creator')} · {vids} vid"
            f"{'' if vids == 1 else 's'} · {who}{src}{tail}")


def build_fields(d):
    fields = []

    for key, icon, label in (
        ('approved', '✅', 'Approved'),
        ('delivered', '📤', 'Delivered'),
    ):
        rows = d.get(key) or []
        if rows:
            fields.append({
                'name': f'{icon} {label} ({len(rows)})',
                'value': _clip([_batch_line(b) for b in rows], len(rows)),
                'inline': False,
            })

    subs = d.get('submitted') or []
    if subs:
        lines = [_batch_line(b, '' if b.get('routed') else '  ← unrouted')
                 for b in subs]
        fields.append({
            'name': f'📥 New submissions ({len(subs)})',
            'value': _clip(lines, len(subs)),
            'inline': False,
        })

    att = d.get('needsAttention') or {}

    stalled = att.get('stalled') or []
    if stalled:
        lines = [_batch_line(b, f"  · {b.get('waitingHours')}h, never started")
                 for b in stalled]
        fields.append({
            'name': f'⏸️ Assigned but never started ({len(stalled)})',
            'value': _clip(lines, len(stalled)),
            'inline': False,
        })

    unrouted = att.get('unrouted') or []
    if unrouted:
        fields.append({
            'name': f'⏳ Nobody assigned ({len(unrouted)})',
            'value': _clip([_batch_line(b) for b in unrouted], len(unrouted)),
            'inline': False,
        })

    desks = att.get('desks') or []
    if desks:
        lines = [f"• {x.get('editor')} — {x.get('batches')} open batches"
                 for x in desks]
        fields.append({
            'name': f'📚 Holding a stack ({len(desks)})',
            'value': _clip(lines, len(desks)),
            'inline': False,
        })

    footage = att.get('footage') or []
    if footage:
        lines = [f"• {(x.get('note') or '').splitlines()[0][:110]}"
                 for x in footage]
        fields.append({
            'name': f'🎞️ Open footage problems ({len(footage)})',
            'value': _clip(lines, len(footage)),
            'inline': False,
        })

    # The bridge failing to reach somebody shows up nowhere else.
    und = att.get('undelivered') or []
    if und:
        lines = [f"• {x.get('kind')} — {x.get('error')}" for x in und]
        fields.append({
            'name': f'🔌 Messages that reached nobody ({len(und)})',
            'value': _clip(lines, len(und)),
            'inline': False,
        })

    return fields


def main():
    config = load_config()
    channel_id = config.get('work_history_channel_id')
    url = config.get('dashboard_digest_url')
    secret = config.get('dashboard_secret')
    bot_token = config.get('discord_bot_token')
    if not (channel_id and url and secret and bot_token):
        logger.info('work-history digest not configured — skipping')
        return

    d = fetch(url, secret)
    if not d or not d.get('ok'):
        return
    if d.get('errors'):
        logger.warning(f"digest reported partial failures: {d['errors']}")
    if d.get('quiet'):
        logger.info('quiet window — nothing to post')
        return

    fields = build_fields(d)
    if not fields:
        logger.info('nothing worth posting')
        return

    t = d.get('totals') or {}
    passed = (t.get('approved') or 0) + (t.get('delivered') or 0)
    needs = ((t.get('stalled') or 0) + (t.get('unrouted') or 0)
             + (t.get('openFootage') or 0) + (t.get('undelivered') or 0))
    # Green when the window is purely good news, amber once something wants a
    # person. The colour is the whole message for anyone scrolling past.
    colour = 0x2ecc71 if needs == 0 else 0xf1c40f

    summary = f"{passed} passed · {t.get('submitted') or 0} came in"
    if needs:
        summary += f" · {needs} need you"

    embed = {
        'title': f'🗂️ Last {d.get("hours", WINDOW_HOURS)}h in the queue',
        'description': summary,
        'color': colour,
        'fields': fields,
        'footer': {'text': datetime.now(timezone.utc).strftime('%a %d %b %H:%M UTC')},
    }
    r = requests.post(
        f'https://discord.com/api/v10/channels/{channel_id}/messages',
        headers={'Authorization': f'Bot {bot_token}', 'Content-Type': 'application/json'},
        json={'embeds': [embed]}, timeout=20,
    )
    logger.info(f'work-history digest sent: {r.status_code} — {summary}')
    if r.status_code >= 300:
        logger.warning(f'discord refused it: {r.text[:200]}')


if __name__ == '__main__':
    main()

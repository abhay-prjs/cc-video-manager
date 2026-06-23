"""
deal_tracker.py
Brand Deals tracker module for the CC Video Manager Discord bot.
Fully DM-driven: no slash commands, no channels, no API/scraper checking of
any platform. Reads/writes the "Brand Deals" Notion database and DMs Vex a
checklist embed per active deal with a copy-pasteable suggested caption and
one "Mark Done" button per active platform — clicking it just bumps that
platform's Done count in Notion by 1. A platform (Instagram/TikTok/YouTube/
Facebook) is only active for a deal when it has both a handle and a Required
count > 0.

Reminders are still deadline-driven (escalating as the deadline approaches,
overdue handling) plus a periodic "time to post" nudge per platform driven by
that platform's Check Interval field — the nudge just resends the caption and
the same mark-done button, there is no automated counting behind it.

Imported by discord_bot.py; call init(bot) once from on_ready().
"""

import json
import os
import requests
import discord
from discord.ext import tasks
from datetime import datetime, timezone
from filelock import FileLock

from logger_setup import get_logger

logger = get_logger('deal_tracker')

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
STATE_FILE  = os.path.join(BASE_DIR, 'deal_tracker_state.json')
STATE_LOCK  = FileLock(os.path.join(BASE_DIR, 'deal_tracker_state.json.lock'))

with open(CONFIG_FILE) as _cf:
    _cfg = json.load(_cf)
    NOTION_TOKEN = _cfg.get('notion_token', '')
    DEALS_DB_ID   = _cfg.get('notion_deals_db_id', '')
    VEX_USER_ID    = int(_cfg.get('vex_discord_user_id', 0) or 0)

DEFAULT_CHECK_INTERVAL_HOURS = 4
DEFAULT_REMINDER_INTERVALS   = '72,24,2'
SYNC_INTERVAL_MINUTES         = 30

# Bumping this resends the interactive verification checklist on next startup.
VERIFICATION_VERSION = 'v5-scheduled-nudges'

# Platform key -> Notion property prefix / display label / short job-id tag.
PLATFORM_CONFIG = {
    'instagram': {'prefix': 'Instagram', 'label': 'Instagram', 'tag': 'ig'},
    'tiktok':    {'prefix': 'TikTok',    'label': 'TikTok',    'tag': 'tt'},
    'youtube':   {'prefix': 'YouTube',   'label': 'YouTube',   'tag': 'yt'},
    'facebook':  {'prefix': 'Facebook',  'label': 'Facebook',  'tag': 'fb'},
}
PLATFORM_ORDER = ['instagram', 'tiktok', 'youtube', 'facebook']

_bot = None  # set by init()


# ── state persistence ───────────────────────────────────────────────────────

def _load_state():
    with STATE_LOCK:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                return json.load(f)
    return {'messages': {}, 'last_checked': {}, 'reminders_sent': {}, 'verification': {}}


def _save_state(state):
    with STATE_LOCK:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)


# ── Notion helpers (mirrors discord_bot.py's pattern) ───────────────────────

def _notion_headers():
    return {
        'Authorization': f'Bearer {NOTION_TOKEN}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def _notion_query_all(db_id, body=None):
    url, results, cursor = f'https://api.notion.com/v1/databases/{db_id}/query', [], None
    while True:
        req_body = dict(body or {})
        req_body['page_size'] = 100
        if cursor:
            req_body['start_cursor'] = cursor
        resp = requests.post(url, headers=_notion_headers(), json=req_body, timeout=15)
        if not resp.ok:
            logger.error(f'_notion_query_all failed for {db_id}: {resp.status_code} {resp.text[:200]}')
            break
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        cursor = data.get('next_cursor')
    return results


def _notion_patch(page_id, properties):
    resp = requests.patch(
        f'https://api.notion.com/v1/pages/{page_id}',
        headers=_notion_headers(),
        json={'properties': properties},
        timeout=15,
    )
    if not resp.ok:
        logger.error(f'_notion_patch failed for deal page {page_id}: {resp.status_code} {resp.text}')
    return resp


def _rich_text(page, prop):
    parts = page.get('properties', {}).get(prop, {}).get('rich_text', [])
    return ''.join(p.get('plain_text', '') for p in parts)


def _title(page, prop):
    parts = page.get('properties', {}).get(prop, {}).get('title', [])
    return ''.join(p.get('plain_text', '') for p in parts)


def _number(page, prop, default=0):
    val = page.get('properties', {}).get(prop, {}).get('number')
    return val if val is not None else default


def _select(page, prop):
    sel = page.get('properties', {}).get(prop, {}).get('select')
    return sel.get('name', '') if sel else ''


def _date(page, prop):
    d = page.get('properties', {}).get(prop, {}).get('date')
    return d.get('start') if d else None


def is_platform_active(platform):
    return bool(platform['handle']) and platform['required'] > 0


def _parse_caption_queue(raw):
    if not raw:
        return []
    blocks, current = [], []
    for line in raw.split('\n'):
        if line.strip() == '---':
            if current:
                blocks.append('\n'.join(current).strip())
            current = []
        else:
            current.append(line)
    if current:
        blocks.append('\n'.join(current).strip())
    return [b for b in blocks if b]


def _parse_post_times(raw):
    slots = []
    for chunk in (raw or '').split(','):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, mm = chunk.split(':')
            slots.append((int(hh), int(mm)))
        except ValueError:
            logger.warning(f'deal_tracker: could not parse Post Times entry: {chunk!r}')
    return slots


def parse_deal(page):
    reminder_raw = _rich_text(page, 'Reminder Intervals') or DEFAULT_REMINDER_INTERVALS
    try:
        reminder_hours = sorted({int(x.strip()) for x in reminder_raw.split(',') if x.strip()}, reverse=True)
    except ValueError:
        reminder_hours = [72, 24, 2]

    platforms = {}
    for key in PLATFORM_ORDER:
        prefix = PLATFORM_CONFIG[key]['prefix']
        platforms[key] = {
            'label':    PLATFORM_CONFIG[key]['label'],
            'handle':   _rich_text(page, f'{prefix} Handle'),
            'required': _number(page, f'{prefix} Required', 0),
            'done':     _number(page, f'{prefix} Done', 0),
        }

    return {
        'page_id':        page['id'],
        'brand_name':     _title(page, 'Brand Name'),
        'start_date':     _date(page, 'Start Date'),
        'deadline':       _date(page, 'Deadline'),
        'notes':          _rich_text(page, 'Deal Notes'),
        'caption_queue':  _parse_caption_queue(_rich_text(page, 'Caption Queue')),
        'post_times':     _parse_post_times(_rich_text(page, 'Post Times')),
        'posts_per_day':  _number(page, 'Posts Per Day', 0),
        'status':         _select(page, 'Status') or 'Active',
        'platforms':      platforms,
        'reminder_hours': reminder_hours,
    }


def fetch_active_deals():
    pages = _notion_query_all(DEALS_DB_ID, {'filter': {'property': 'Status', 'select': {'equals': 'Active'}}})
    return [parse_deal(p) for p in pages]


def fetch_deal(page_id):
    resp = requests.get(f'https://api.notion.com/v1/pages/{page_id}', headers=_notion_headers(), timeout=15)
    return parse_deal(resp.json()) if resp.ok else None


def mark_complete(page_id):
    _notion_patch(page_id, {'Status': {'select': {'name': 'Completed'}}})


def mark_overdue(page_id):
    _notion_patch(page_id, {'Status': {'select': {'name': 'Overdue'}}})


def update_note(page_id, note):
    _notion_patch(page_id, {'Deal Notes': {'rich_text': [{'text': {'content': note[:2000]}}]}})


def bump_platform_done(page_id, platform_key, new_done):
    prop = f"{PLATFORM_CONFIG[platform_key]['prefix']} Done"
    _notion_patch(page_id, {prop: {'number': new_done}})


# ── embed rendering ──────────────────────────────────────────────────────────

def _progress_bar(done, required):
    required = max(required, 1)
    done = max(0, min(done, required))
    return '✅' * done + '⬜' * (required - done)


def _to_utc(iso_str):
    dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _days_left(deadline_str):
    if not deadline_str:
        return None
    return _to_utc(deadline_str) - datetime.now(timezone.utc)


def _active_platforms(deal):
    return [deal['platforms'][k] for k in PLATFORM_ORDER if is_platform_active(deal['platforms'][k])]


def _current_caption(deal):
    queue = deal['caption_queue']
    if not queue:
        return ''
    state = _load_state()
    idx = state.get('caption_index', {}).get(deal['page_id'], 0)
    return queue[min(idx, len(queue) - 1)]


def _advance_caption(page_id):
    state = _load_state()
    idx_map = state.setdefault('caption_index', {})
    idx_map[page_id] = idx_map.get(page_id, 0) + 1
    _save_state(state)


def _embed_color(deal):
    delta = _days_left(deal['deadline'])
    if delta is not None and delta.total_seconds() <= 86400:
        return discord.Color.red()
    active = _active_platforms(deal)
    behind = any(p['done'] < p['required'] for p in active)
    if behind:
        return discord.Color.yellow()
    return discord.Color.green()


def build_deal_embed(deal):
    delta = _days_left(deal['deadline'])
    if delta is None:
        days_str = 'no deadline set'
    else:
        days = int(delta.total_seconds() // 86400)
        days_str = f'{days} days left' if days >= 0 else f'{abs(days)} days overdue'

    embed = discord.Embed(title=f"📦 {deal['brand_name']}", color=_embed_color(deal))
    for key in PLATFORM_ORDER:
        p = deal['platforms'][key]
        if not is_platform_active(p):
            continue
        embed.add_field(
            name=p['label'],
            value=f"@{p['handle']}  [{_progress_bar(p['done'], p['required'])}] {p['done']}/{p['required']}",
            inline=False,
        )
    deadline_disp = deal['deadline'] or 'unset'
    embed.add_field(name='Deadline', value=f"{deadline_disp} · {days_str}", inline=False)
    if deal['notes']:
        embed.add_field(name='🗒️ Notes', value=deal['notes'], inline=False)
    return embed


# ── buttons / views ──────────────────────────────────────────────────────────

class EditNoteModal(discord.ui.Modal, title='Edit Deal Note'):
    note = discord.ui.TextInput(label='New note', style=discord.TextStyle.paragraph, required=False, max_length=2000)

    def __init__(self, page_id):
        super().__init__()
        self.page_id = page_id

    async def on_submit(self, interaction: discord.Interaction):
        update_note(self.page_id, str(self.note.value or ''))
        deal = fetch_deal(self.page_id)
        if deal:
            await _refresh_message(self.page_id, deal)
        await interaction.response.send_message('Note updated.', ephemeral=True)


class DealActionView(discord.ui.View):
    def __init__(self, page_id, active_platform_keys=None):
        super().__init__(timeout=None)
        self.page_id = page_id
        for key in (active_platform_keys or []):
            label = f"Mark {PLATFORM_CONFIG[key]['label']} Done"
            btn = discord.ui.Button(
                label=label, emoji='✅', style=discord.ButtonStyle.success,
                custom_id=f'deal_mark_{key}_{page_id}',
            )
            btn.callback = self._make_mark_done_callback(key)
            self.add_item(btn)

        refresh_btn = discord.ui.Button(label='Refresh', emoji='🔄', style=discord.ButtonStyle.secondary,
                                         custom_id=f'deal_refresh_{page_id}')
        refresh_btn.callback = self._refresh_callback
        self.add_item(refresh_btn)

        note_btn = discord.ui.Button(label='Edit Note', emoji='📝', style=discord.ButtonStyle.secondary,
                                      custom_id=f'deal_edit_note_{page_id}')
        note_btn.callback = self._edit_note_callback
        self.add_item(note_btn)

        complete_btn = discord.ui.Button(label='Mark Complete', emoji='🏁', style=discord.ButtonStyle.danger,
                                          custom_id=f'deal_complete_{page_id}')
        complete_btn.callback = self._complete_callback
        self.add_item(complete_btn)

    def _make_mark_done_callback(self, platform_key):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer()
            deal = fetch_deal(self.page_id)
            if not deal:
                return
            p = deal['platforms'][platform_key]
            new_done = min(p['done'] + 1, p['required']) if p['required'] else p['done'] + 1
            bump_platform_done(self.page_id, platform_key, new_done)
            _advance_caption(self.page_id)
            deal = fetch_deal(self.page_id)
            await _refresh_message(self.page_id, deal)
            active = _active_platforms(deal)
            if active and all(ap['done'] >= ap['required'] for ap in active):
                mark_complete(self.page_id)
                await _send_plain_dm(f"🎉 {deal['brand_name']}: all platforms marked done — marked Completed in Notion.")
                deal = fetch_deal(self.page_id)
                await _refresh_message(self.page_id, deal)
        return callback

    async def _refresh_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        deal = fetch_deal(self.page_id)
        if deal:
            await _refresh_message(self.page_id, deal)

    async def _edit_note_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditNoteModal(self.page_id))

    async def _complete_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        mark_complete(self.page_id)
        deal = fetch_deal(self.page_id)
        if deal:
            await _refresh_message(self.page_id, deal)


# ── DM helpers ───────────────────────────────────────────────────────────────

async def _get_vex_dm():
    if not VEX_USER_ID:
        logger.error('deal_tracker: vex_discord_user_id is not set in config.json')
        return None
    user = _bot.get_user(VEX_USER_ID) or await _bot.fetch_user(VEX_USER_ID)
    return await user.create_dm()


async def _send_or_refresh_caption_message(deal, dm, state):
    page_id = deal['page_id']
    caption = _current_caption(deal)
    if not caption:
        return
    queue_len = len(deal['caption_queue'])
    label = '📝 Next caption to copy' if queue_len > 1 else '📝 Caption to copy'
    content = f"{label} (long-press the text below → Copy Text):\n```{caption[:1900]}```"
    msg_id = state.setdefault('caption_messages', {}).get(page_id)
    if msg_id:
        try:
            msg = await dm.fetch_message(int(msg_id))
            await msg.edit(content=content)
            return
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f'deal_tracker: could not edit caption message for {page_id}: {e}')
    msg = await dm.send(content)
    state['caption_messages'][page_id] = msg.id


async def _send_or_refresh_deal_embed(deal):
    state = _load_state()
    page_id = deal['page_id']
    active_keys = [k for k in PLATFORM_ORDER if is_platform_active(deal['platforms'][k])]
    embed = build_deal_embed(deal)
    view = DealActionView(page_id, active_keys)
    msg_id = state['messages'].get(page_id)
    dm = await _get_vex_dm()
    if dm is None:
        return
    if msg_id:
        try:
            msg = await dm.fetch_message(int(msg_id))
            await msg.edit(embed=embed, view=view)
            await _send_or_refresh_caption_message(deal, dm, state)
            _save_state(state)
            return
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f'deal_tracker: could not edit existing message for {page_id}: {e}')
    msg = await dm.send(embed=embed, view=view)
    state['messages'][page_id] = msg.id
    await _send_or_refresh_caption_message(deal, dm, state)
    _save_state(state)


async def _refresh_message(page_id, deal):
    await _send_or_refresh_deal_embed(deal)


async def _send_plain_dm(text):
    dm = await _get_vex_dm()
    if dm:
        await dm.send(text)


# ── "time to post" nudges (replaces the old API/scraper checkers) ──────────
# Nudges are deal-level (not per-platform): one DM per scheduled slot, listing
# every platform still short of its Required count, with the next queued
# caption and a Mark Done button per pending platform.

DEFAULT_NUDGE_WINDOW = (9, 21)  # UTC hours used to auto-space "Posts Per Day" when no exact Post Times are set


def _daily_slots(deal):
    if deal['post_times']:
        return deal['post_times']
    count = deal['posts_per_day']
    if not count or count <= 0:
        return []
    start, end = DEFAULT_NUDGE_WINDOW
    if count == 1:
        return [(start, 0)]
    step = (end - start) / (count - 1)
    slots = []
    for i in range(int(count)):
        hour_float = start + step * i
        hh = int(hour_float)
        mm = int(round((hour_float - hh) * 60))
        slots.append((hh, mm))
    return slots


async def _send_post_nudge(deal, pending_keys):
    lines = [f"⏰ {deal['brand_name']}: time to post!"]
    for key in pending_keys:
        p = deal['platforms'][key]
        lines.append(f"• {p['label']} (@{p['handle']}) — {p['done']}/{p['required']}")
    caption = _current_caption(deal)
    if caption:
        lines.append('Caption (long-press the text below → Copy Text):')
        lines.append(f"```{caption[:1000]}```")
    dm = await _get_vex_dm()
    if dm is None:
        return
    view = discord.ui.View(timeout=None)
    for key in pending_keys:
        label = f"Mark {deal['platforms'][key]['label']} Done"
        btn = discord.ui.Button(label=label, emoji='✅', style=discord.ButtonStyle.success,
                                 custom_id=f'nudge_mark_{key}_{deal["page_id"]}')
        btn.callback = _make_nudge_callback(deal['page_id'], key, view)
        view.add_item(btn)
    await dm.send('\n'.join(lines), view=view)


def _make_nudge_callback(page_id, platform_key, view):
    async def on_click(interaction: discord.Interaction):
        await interaction.response.defer()
        fresh = fetch_deal(page_id)
        if not fresh:
            return
        fp = fresh['platforms'][platform_key]
        new_done = min(fp['done'] + 1, fp['required']) if fp['required'] else fp['done'] + 1
        bump_platform_done(page_id, platform_key, new_done)
        _advance_caption(page_id)
        for child in view.children:
            if child.custom_id == f'nudge_mark_{platform_key}_{page_id}':
                child.disabled = True
        await interaction.edit_original_response(view=view)
        fresh = fetch_deal(page_id)
        await _refresh_message(page_id, fresh)
        active = _active_platforms(fresh)
        if active and all(ap['done'] >= ap['required'] for ap in active):
            mark_complete(page_id)
            await _send_plain_dm(f"🎉 {fresh['brand_name']}: all platforms marked done — marked Completed in Notion.")
    return on_click


# ── reminders / overdue handling ─────────────────────────────────────────────

async def _check_reminders(deal):
    delta = _days_left(deal['deadline'])
    if delta is None:
        return
    hours_left = delta.total_seconds() / 3600
    page_id = deal['page_id']
    state = _load_state()
    sent = state['reminders_sent'].setdefault(page_id, [])

    active = _active_platforms(deal)
    behind = any(p['done'] < p['required'] for p in active)

    if hours_left <= 0:
        if behind:
            last_2h = state['reminders_sent'].get(f'{page_id}_overdue_ts')
            now_ts = datetime.now(timezone.utc).timestamp()
            if not last_2h or now_ts - last_2h >= 7200:
                await _send_plain_dm(f"🚨🚨 {deal['brand_name']}: deadline passed and still not all marked done!")
                state['reminders_sent'][f'{page_id}_overdue_ts'] = now_ts
                _save_state(state)
            mark_overdue(page_id)
        return

    for threshold in deal['reminder_hours']:
        key = f'{threshold}h'
        if hours_left <= threshold and key not in sent:
            urgent = threshold <= 24
            prefix = '🚨' if urgent else '⚠️'
            if behind:
                await _send_plain_dm(
                    f"{prefix} {deal['brand_name']}: {threshold}h to deadline and not all active platforms marked done."
                )
            sent.append(key)
            _save_state(state)


# ── scheduling ───────────────────────────────────────────────────────────────
# Nudges fire on a 5-minute tick: each active deal's daily slots (exact Post
# Times, or auto-spaced from Posts Per Day) are checked against the current
# UTC time, with a per-slot-per-day dedup so a slot fires at most once.

NUDGE_CHECK_MINUTES = 5
NUDGE_TOLERANCE_SECONDS = 150  # half the tick interval


@tasks.loop(minutes=NUDGE_CHECK_MINUTES)
async def nudge_loop():
    now = datetime.now(timezone.utc)
    today_str = now.strftime('%Y-%m-%d')
    try:
        deals = fetch_active_deals()
    except Exception as e:
        logger.error(f'deal_tracker nudge_loop failed: {e}')
        return

    state = _load_state()
    sent_slots = state.setdefault('nudges_sent', {})

    for deal in deals:
        pending_keys = [k for k in PLATFORM_ORDER
                        if is_platform_active(deal['platforms'][k]) and deal['platforms'][k]['done'] < deal['platforms'][k]['required']]
        if not pending_keys:
            continue
        for hh, mm in _daily_slots(deal):
            slot_key = f"{deal['page_id']}_{today_str}_{hh:02d}{mm:02d}"
            if slot_key in sent_slots:
                continue
            slot_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if abs((now - slot_dt).total_seconds()) <= NUDGE_TOLERANCE_SECONDS:
                try:
                    await _send_post_nudge(deal, pending_keys)
                except Exception as e:
                    logger.error(f"deal_tracker: failed to send nudge for {deal['brand_name']}: {e}")
                sent_slots[slot_key] = True

    # prune slot keys older than today to keep state small
    state['nudges_sent'] = {k: v for k, v in sent_slots.items() if today_str in k}
    _save_state(state)


@nudge_loop.before_loop
async def _before_nudge_loop():
    await _bot.wait_until_ready()


@tasks.loop(minutes=SYNC_INTERVAL_MINUTES)
async def sync_loop():
    try:
        deals = fetch_active_deals()
    except Exception as e:
        logger.error(f'deal_tracker sync_loop failed: {e}')
        return
    for deal in deals:
        try:
            await _send_or_refresh_deal_embed(deal)
            await _check_reminders(deal)
        except Exception as e:
            logger.error(f"deal_tracker: error processing deal {deal.get('brand_name')}: {e}")


@sync_loop.before_loop
async def _before_sync_loop():
    await _bot.wait_until_ready()


# ── interactive verification checklist ───────────────────────────────────────

CHECKLIST_SECTIONS = [
    ('EXISTING BOT — MUST STILL WORK', [
        ('existing_complete',   '/complete command responds correctly'),
        ('existing_stats',      '/stats command responds correctly'),
        ('existing_leaderboard', '/leaderboard command responds correctly'),
        ('existing_notion',     'Existing Notion connection still works'),
        ('existing_online',     'Bot connects and stays online without errors'),
        ('existing_cogs',       'No import errors or cog loading failures'),
    ]),
    ('MANUAL TRACKING MODEL', [
        ('manual_no_scraping',  'No more API keys / session cookies / scraper calls anywhere'),
        ('manual_caption_shown', 'Suggested Caption shows in the embed as a copy-pasteable code block'),
        ('manual_mark_buttons', 'One Mark Done button per active platform shows on the deal embed'),
        ('manual_mark_increments', 'Clicking Mark Done bumps that platform\'s Done count in Notion by 1'),
        ('manual_all_done_completes', 'Marking all active platforms done auto-sets Status to Completed'),
        ('manual_facebook_works', 'Facebook now works the same as the other three platforms'),
    ]),
    ('SCHEDULED NUDGES', [
        ('nudge_exact_times',    'Setting Post Times (e.g. "09:00,18:00") fires nudges at those exact UTC times'),
        ('nudge_posts_per_day',  'Setting Posts Per Day (no Post Times) auto-spaces nudges across the day'),
        ('nudge_lists_pending',  'Nudge DM lists every platform still short of its Required count'),
        ('nudge_has_caption',    'Nudge DM shows the current queued caption in a copy-pasteable code block'),
        ('nudge_has_mark_button', 'Nudge DM has a working Mark Done button per pending platform'),
        ('nudge_stops_when_done', 'Nudges stop once all active platforms hit their Required count'),
        ('caption_queue_advances', 'Caption Queue advances to the next entry after each Mark Done'),
        ('deadline_reminders_work', 'Deadline-based reminder escalation (72/24/2h, overdue) still works'),
    ]),
    ('CONFIG', [
        ('cfg_keys_removed', 'yt_api_key / ig_session_id / tt_session_id removed from config.json'),
    ]),
]

_VERIFY_RESULT_STYLE = {
    'pass': (discord.ButtonStyle.success, '✅'),
    'fail': (discord.ButtonStyle.danger, '❌'),
    'skip': (discord.ButtonStyle.secondary, '⚠️'),
}


class VerificationItemView(discord.ui.View):
    def __init__(self, item_id, text, chosen=None):
        super().__init__(timeout=None)
        self.item_id = item_id
        self.text = text
        for result in ('pass', 'fail', 'skip'):
            style, emoji = _VERIFY_RESULT_STYLE[result]
            label = {'pass': 'Pass', 'fail': 'Fail', 'skip': 'Skip'}[result]
            btn = discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f'verify_{item_id}_{result}',
                disabled=(chosen is not None),
            )
            btn.callback = self._make_callback(result)
            self.add_item(btn)

    def _make_callback(self, result):
        async def callback(interaction: discord.Interaction):
            await _record_verification_result(interaction, self.item_id, self.text, result)
        return callback


async def _record_verification_result(interaction, item_id, text, result):
    state = _load_state()
    verify_state = state.setdefault('verification', {})
    results = verify_state.setdefault('results', {})
    results[item_id] = result
    _save_state(state)

    emoji = _VERIFY_RESULT_STYLE[result][1]
    view = VerificationItemView(item_id, text, chosen=result)
    await interaction.response.edit_message(content=f'{emoji} {text}', view=view)

    pending = set(verify_state.get('pending', []))
    if pending and pending.issubset(results.keys()):
        await _send_verification_summary(pending, results)


async def _send_verification_summary(pending, results):
    passed = [i for i in pending if results.get(i) == 'pass']
    failed = [i for i in pending if results.get(i) == 'fail']
    skipped = [i for i in pending if results.get(i) == 'skip']

    lines = [f"✅ {len(passed)} passed · ❌ {len(failed)} failed · ⚠️ {len(skipped)} skipped"]
    if failed:
        id_to_text = {iid: text for _, items in CHECKLIST_SECTIONS for iid, text in items}
        lines.append('\nFailed items — suggested fixes:')
        for iid in failed:
            lines.append(f"❌ {id_to_text.get(iid, iid)} — check `logs/discord_bot.log` for the relevant error and re-test after fixing.")
    else:
        lines.append('\nManual mark-done tracker live 🚀')

    await _send_plain_dm('\n'.join(lines))


async def send_verification_checklist():
    """Sends one message per checklist item (each with Pass/Fail/Skip buttons) to Vex's DM."""
    dm = await _get_vex_dm()
    if dm is None:
        return

    state = _load_state()
    verify_state = state.setdefault('verification', {})
    verify_state['results'] = {}
    verify_state['messages'] = {}
    pending = []

    await dm.send('**Deal Tracker patch — verification checklist**')
    for section_title, items in CHECKLIST_SECTIONS:
        await dm.send(f'**{section_title}:**')
        for item_id, text in items:
            view = VerificationItemView(item_id, text)
            msg = await dm.send(content=f'⬜ {text}', view=view)
            verify_state['messages'][item_id] = msg.id
            pending.append(item_id)

    verify_state['pending'] = pending
    verify_state['version'] = VERIFICATION_VERSION
    _save_state(state)


def init(bot):
    """Call once from discord_bot.py's on_ready()."""
    global _bot
    _bot = bot

    state = _load_state()
    for page_id, msg_id in state.get('messages', {}).items():
        try:
            # Active platform keys aren't known until the deal is fetched fresh; register with
            # no platform buttons for now — the next sync_loop tick (within 30 min) rebuilds the
            # message with the correct buttons anyway.
            bot.add_view(DealActionView(page_id, []), message_id=int(msg_id))
        except Exception as e:
            logger.warning(f'deal_tracker: could not re-register view for {page_id}/{msg_id}: {e}')
    if state.get('messages'):
        logger.info(f"deal_tracker: re-registered {len(state['messages'])} pending deal view(s)")

    if not sync_loop.is_running():
        sync_loop.start()
    if not nudge_loop.is_running():
        nudge_loop.start()

    verify_state = state.get('verification', {})
    if verify_state.get('version') != VERIFICATION_VERSION:
        bot.loop.create_task(send_verification_checklist())
        logger.info(f'deal_tracker: scheduled verification checklist send for {VERIFICATION_VERSION}')
    else:
        id_to_text = {iid: text for _, items in CHECKLIST_SECTIONS for iid, text in items}
        results = verify_state.get('results', {})
        for item_id, msg_id in verify_state.get('messages', {}).items():
            if item_id in results:
                continue  # already answered, no live buttons to restore
            try:
                bot.add_view(VerificationItemView(item_id, id_to_text.get(item_id, item_id)), message_id=int(msg_id))
            except Exception as e:
                logger.warning(f'deal_tracker: could not re-register verification view for {item_id}: {e}')

    logger.info('deal_tracker initialized')

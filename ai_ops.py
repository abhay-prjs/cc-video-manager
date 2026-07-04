"""
ai_ops.py
AI-powered ops assistant using local Ollama (qwen2.5:7b).
Used by notion_bridge (Telegram) and discord_bot (Discord) for smart
editor assignment and natural-language ops queries.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
EDITOR_COUNTERS_FILE = os.path.join(BASE_DIR, 'editor_counters.json')
SCHEDULE_CACHE_FILE  = os.path.join(BASE_DIR, 'schedule_cache.json')
EDITOR_PROFILES_DB   = 'a18d5c16f3594a2ba6206c837aa04232'
SCHEDULE_CACHE_TTL   = 7200  # seconds — refresh every 2 hours

EDT          = timezone(timedelta(hours=-4))
PHT          = timezone(timedelta(hours=8))
DAYS_OF_WEEK = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

_DAY_SCHEDULE_PROPS = {
    'Monday': 'Mon Schedule', 'Tuesday': 'Tue Schedule',
    'Wednesday': 'Wed Schedule', 'Thursday': 'Thu Schedule',
    'Friday': 'Fri Schedule', 'Saturday': 'Sat Schedule',
    'Sunday': 'Sun Schedule',
}

OLLAMA_URL   = 'http://localhost:11434/api/chat'
OLLAMA_MODEL = 'qwen2.5:7b'

SYSTEM_PROMPT = (
    "You are an ops assistant for CC Video Manager, a video editing team. "
    "Answer using ONLY the facts provided in the message. Do NOT reason out loud. Do NOT say 'we need to' or 'perhaps'. "
    "Just state the answer directly. "
    "ONLINE NOW means the editor IS on shift right now. OFFLINE means they are off shift. "
    "For assignments, prefer editors who are ONLINE NOW, below 70% load, and have fewer missed deadlines. "
    "Never assign to someone at 95%+ load unless no one else is available. "
    "Answer in 2-3 sentences max. No preamble, no suggestions, just the answer."
)


def _load_counters():
    try:
        with open(EDITOR_COUNTERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _parse_shifts_pht(sched_str: str) -> list:
    """Parse 'HH:MM-HH:MM' or split 'HH:MM-HH:MM|HH:MM-HH:MM' → [(start_mins, end_mins)]."""
    shifts = []
    for part in sched_str.split('|'):
        part = part.strip()
        if '-' not in part:
            continue
        try:
            s, e = part.split('-', 1)
            s_h, s_m = map(int, s.strip().split(':'))
            e_h, e_m = map(int, e.strip().split(':'))
            s_mins = s_h * 60 + s_m
            e_mins = e_h * 60 + e_m
            if e_mins <= s_mins:
                e_mins += 1440  # overnight
            shifts.append((s_mins, e_mins))
        except (ValueError, AttributeError):
            continue
    return shifts


def _on_shift_now(sched_str: str, now_pht: datetime):
    """Returns True (on shift), False (off shift), or None (flexible/unknown)."""
    if not sched_str:
        return None
    low = sched_str.lower()
    if 'no certain time' in low:
        return None
    if low == 'off':
        return False
    now_mins = now_pht.hour * 60 + now_pht.minute
    for s, e in _parse_shifts_pht(sched_str):
        if s <= now_mins < e:
            return True
        if e > 1440 and now_mins < e - 1440:
            return True
    return False


def _fetch_raw_schedules(notion_token: str) -> dict:
    """
    Fetch all-days schedule data from Editor Profiles and write to schedule_cache.json.
    Returns {name: {Mon/Tue/.../Sun: 'HH:MM-HH:MM', 'timezone': 'PHT (UTC+8)'}}.
    """
    url     = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    headers = {
        'Authorization':  f'Bearer {notion_token}',
        'Notion-Version': '2022-06-28',
        'Content-Type':   'application/json',
    }
    raw = {}
    resp = requests.post(url, headers=headers, json={}, timeout=15)
    if not resp.ok:
        logger.error(f'_fetch_raw_schedules: {resp.status_code}')
        return raw
    for page in resp.json().get('results', []):
        props   = page['properties']
        name_rt = props.get('Editor', {}).get('title', [])
        name    = name_rt[0].get('plain_text', '') if name_rt else ''
        if not name:
            continue
        tz_rt = props.get('Timezone', {}).get('rich_text', [])
        tz    = ''.join(seg.get('plain_text', '') for seg in tz_rt).strip() or 'PHT'
        entry = {'timezone': tz}
        for day, prop in _DAY_SCHEDULE_PROPS.items():
            rt    = props.get(prop, {}).get('rich_text', [])
            entry[day] = ''.join(seg.get('plain_text', '') for seg in rt).strip()
        raw[name] = entry

    try:
        import time as _time
        cache = {'updated_at': _time.time(), 'editors': raw}
        with open(SCHEDULE_CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=2)
        logger.info(f'schedule_cache.json updated ({len(raw)} editors)')
    except Exception as e:
        logger.error(f'schedule_cache write failed: {e}')
    return raw


def _load_schedule_cache(notion_token: str) -> dict:
    """Return raw schedule dict from cache, refreshing from Notion if stale or missing."""
    import time as _time
    try:
        with open(SCHEDULE_CACHE_FILE) as f:
            cache = json.load(f)
        age = _time.time() - cache.get('updated_at', 0)
        if age < SCHEDULE_CACHE_TTL:
            return cache['editors']
        logger.info(f'schedule_cache stale ({int(age)}s), refreshing')
    except FileNotFoundError:
        logger.info('schedule_cache.json missing, fetching from Notion')
    except Exception as e:
        logger.warning(f'schedule_cache read error: {e}')
    return _fetch_raw_schedules(notion_token)


def refresh_schedule_cache(notion_token: str):
    """Force-refresh the schedule cache from Notion. Call from cron or after /setschedule."""
    return _fetch_raw_schedules(notion_token)


def fetch_schedules_from_profiles(notion_token: str) -> dict:
    """
    Return today's shift status per editor using cached schedule data.
    Cache auto-refreshes from Notion every 2 hours.
    Returns {name: {'sched': label_str, 'on_shift': True/False/None}}.
    """
    raw      = _load_schedule_cache(notion_token)
    now_pht  = datetime.now(PHT)
    today    = now_pht.strftime('%A')
    now_mins = now_pht.hour * 60 + now_pht.minute
    result   = {}
    for name, entry in raw.items():
        sched    = entry.get(today, '')
        tz_str   = entry.get('timezone', 'PHT')
        tz_short = tz_str.split()[0] if tz_str else 'PHT'
        on_shift = _on_shift_now(sched, now_pht)

        if sched.lower() == 'off':
            label = '[OFFLINE] day off'
        elif not sched or 'no certain time' in sched.lower():
            label = f'[FLEXIBLE] hours not fixed ({tz_short})'
        elif on_shift:
            label = f'[ONLINE] shift {sched} {tz_short}'
        else:
            mins_until, next_start = None, ''
            for s, _ in _parse_shifts_pht(sched):
                wait = s - now_mins if s > now_mins else None
                if wait is not None and (mins_until is None or wait < mins_until):
                    mins_until = wait
                    next_start = f'{s // 60:02d}:{s % 60:02d}'
            if mins_until is not None:
                h, m = divmod(mins_until, 60)
                eta   = f'{h}h {m}m' if h else f'{m}m'
                label = f'[OFFLINE] starts {next_start} {tz_short} (in {eta})'
            else:
                label = f'[OFFLINE] shift {sched} {tz_short} already ended'

        result[name] = {'sched': label, 'on_shift': on_shift}
    return result


def warmup():
    """Fire a cheap request so 7b is loaded into memory before the first real query."""
    try:
        requests.post(OLLAMA_URL, json={
            'model':   OLLAMA_MODEL,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'stream':  False,
            'options': {'temperature': 0.1, 'num_predict': 1},
        }, timeout=180)
        logger.info('Ollama warmup complete')
    except Exception as e:
        logger.warning(f'Ollama warmup failed (non-fatal): {e}')


def query_ai(user_message: str, timeout: int = 180) -> str:
    """Call local Ollama with SYSTEM_PROMPT and return response text, or '' on failure."""
    try:
        resp = requests.post(OLLAMA_URL, json={
            'model':    OLLAMA_MODEL,
            'messages': [
                {'role': 'system',  'content': SYSTEM_PROMPT},
                {'role': 'user',    'content': user_message},
            ],
            'stream':  False,
            'options': {'temperature': 0.1, 'num_predict': 350},
        }, timeout=timeout)
        if resp.ok:
            return resp.json()['message']['content'].strip()
        logger.error(f'Ollama error: {resp.status_code} {resp.text[:200]}')
    except Exception as e:
        logger.error(f'query_ai failed: {e}')
    return ''


def _schedule_summary(profile_schedules: dict) -> str:
    """Precomputed one-line schedule summary injected at top of context so the model reads facts, not reasons."""
    if not profile_schedules:
        return ''
    online   = [n for n, d in profile_schedules.items() if d['on_shift'] is True]
    flexible = [n for n, d in profile_schedules.items() if d['on_shift'] is None]
    offline  = {n: d['sched'] for n, d in profile_schedules.items() if d['on_shift'] is False}

    parts = []
    if online:
        parts.append(f"ONLINE NOW: {', '.join(online)}")
    else:
        parts.append('ONLINE NOW: nobody')
    if flexible:
        parts.append(f"FLEXIBLE (no fixed shift): {', '.join(flexible)}")

    # Find who starts soonest from the offline labels "starts HH:MM PHT (in Xh Ym)"
    soonest = []
    import re
    for name, sched in offline.items():
        m = re.search(r'in (\d+)h (\d+)m', sched)
        if m:
            mins = int(m.group(1)) * 60 + int(m.group(2))
            soonest.append((mins, name, sched))
        else:
            m2 = re.search(r'in (\d+)m', sched)
            if m2:
                soonest.append((int(m2.group(1)), name, sched))
    if soonest:
        soonest.sort()
        _, first_name, first_sched = soonest[0]
        # Extract "starts HH:MM PHT" portion
        start_match = re.search(r'starts [\d:]+\s+\w+', first_sched)
        start_info = start_match.group(0) if start_match else ''
        parts.append(f"NEXT TO COME ONLINE: {first_name} ({start_info})")

    return ' | '.join(parts)


def build_context_from_ranked(ranked: list, profile_schedules: dict = None) -> str:
    """
    Build a plain-text context block from _rank_editors() output (notion_bridge format).
    ranked: list of dicts with keys: editor, ratio, available_now, has_schedule,
            mins_until, tier, info{active, capacity}
    profile_schedules: optional output of fetch_schedules_from_profiles() — overrides
                       shift status with data read directly from Editor Profiles.
    """
    counters = _load_counters()
    now_pht  = datetime.now(PHT)
    summary  = _schedule_summary(profile_schedules)
    header   = (f'Current time: {datetime.now(EDT).strftime("%H:%M EDT")} / '
                f'{now_pht.strftime("%H:%M PHT %a")}')
    lines    = [header]
    if summary:
        lines.append(summary)
    lines.append('EDITORS:')
    for r in ranked:
        pct  = round(r['ratio'] * 100)
        name = r['editor']
        act  = r['info'].get('active', 0)
        cap  = r['info'].get('capacity', 0)

        if profile_schedules and name in profile_schedules:
            avail = profile_schedules[name]['sched']
        elif r['available_now'] and r['has_schedule']:
            avail = 'in shift now'
        elif not r['has_schedule']:
            avail = 'no schedule data'
        elif r['mins_until'] is not None:
            h, m = divmod(int(r['mins_until']), 60)
            avail = f"available in {h}h {m}m" if h else f"available in {m}m"
        else:
            avail = 'out of shift'

        ec     = counters.get(name, {})
        revs   = ec.get('revisions', 0)
        missed = ec.get('missed_deadlines', 0)
        slow   = ec.get('slow_pickups_4h', 0)
        stats  = f", {revs} revisions, {missed} missed deadlines" if (revs or missed) else ''
        if slow:
            stats += f", {slow} slow pickups"

        lines.append(f"- {name}: {act}/{cap} videos ({pct}% load), {avail}{stats}")
    return '\n'.join(lines)


def build_context_from_editors(editors: dict, profile_schedules: dict = None) -> str:
    """
    Build a plain-text context block from fetch_editors_from_notion() output (discord_bot format).
    editors: {name: {active, capacity, page_id, ...}}
    profile_schedules: optional output of fetch_schedules_from_profiles() — adds shift status
    """
    counters = _load_counters()
    now_pht  = datetime.now(PHT)
    summary  = _schedule_summary(profile_schedules)
    header   = (f'Current time: {datetime.now(EDT).strftime("%H:%M EDT")} / '
                f'{now_pht.strftime("%H:%M PHT %a")}')
    lines    = [header]
    if summary:
        lines.append(summary)
    lines.append('EDITORS:')
    for name, info in editors.items():
        act  = info.get('active', 0)
        cap  = info.get('capacity', 1)
        pct  = round((act / cap) * 100) if cap else 0
        ec     = counters.get(name, {})
        revs   = ec.get('revisions', 0)
        missed = ec.get('missed_deadlines', 0)
        slow   = ec.get('slow_pickups_4h', 0)
        stats  = f", {revs} revisions, {missed} missed deadlines" if (revs or missed) else ''
        if slow:
            stats += f", {slow} slow pickups"
        shift = f', {profile_schedules[name]["sched"]}' if profile_schedules and name in profile_schedules else ''
        lines.append(f"- {name}: {act}/{cap} videos ({pct}% load){shift}{stats}")
    return '\n'.join(lines)


def ai_recommend_editor(ranked: list, folder_name: str, client: str, video_count: int):
    """
    Ask AI to pick the best editor for a new folder.
    Returns (editor_name, reason_str) or ('', '') if AI fails or returns unknown name.
    Caller should fall back to ranked[0] on ('', '').
    """
    valid_names = [r['editor'] for r in ranked]
    context     = build_context_from_ranked(ranked)
    message     = (
        f"{context}\n\n"
        f"New folder to assign: \"{folder_name}\" for client {client} — {video_count} video(s)\n\n"
        f"Valid editor names (use exactly as written): {', '.join(valid_names)}\n\n"
        f"Pick the best editor. Reply in this exact format:\n"
        f"EDITOR: <name>\n"
        f"REASON: <one sentence why>"
    )
    response = query_ai(message)
    if not response:
        return '', ''

    editor, reason = '', ''
    for line in response.splitlines():
        upper = line.upper()
        if upper.startswith('EDITOR:'):
            editor = line.split(':', 1)[1].strip()
        elif upper.startswith('REASON:'):
            reason = line.split(':', 1)[1].strip()

    # Validate exact match
    if editor in valid_names:
        return editor, reason

    # Try case-insensitive match
    for name in valid_names:
        if name.lower() == editor.lower():
            return name, reason

    # Try substring match
    for name in valid_names:
        if name.lower() in editor.lower() or editor.lower() in name.lower():
            logger.info(f'ai_recommend_editor: fuzzy matched "{editor}" → "{name}"')
            return name, reason

    logger.warning(f'ai_recommend_editor: AI returned unknown editor "{editor}", falling back')
    return '', ''


def _schedule_fact(profile_schedules: dict) -> str:
    """Build a plain-text schedule fact string from precomputed profile_schedules."""
    import re
    now_pht  = datetime.now(PHT)
    online   = [n for n, d in profile_schedules.items() if d['on_shift'] is True]
    flexible = [n for n, d in profile_schedules.items() if d['on_shift'] is None]
    offline  = [(n, d['sched']) for n, d in profile_schedules.items() if d['on_shift'] is False]

    def _mins(label):
        m = re.search(r'in (\d+)h (\d+)m', label)
        if m: return int(m.group(1)) * 60 + int(m.group(2))
        m2 = re.search(r'in (\d+)m\b', label)
        return int(m2.group(1)) if m2 else 9999

    parts = [f"Current time: {now_pht.strftime('%H:%M PHT %a')}"]
    parts.append(f"Currently on shift: {', '.join(online) if online else 'nobody'}")
    if flexible:
        parts.append(f"Flexible hours (no fixed shift): {', '.join(flexible)}")
    if offline:
        upcoming = sorted(offline, key=lambda x: _mins(x[1]))
        parts.append("Upcoming shifts: " + ', '.join(
            f"{n} ({s.replace('[OFFLINE] ', '').replace('[FLEXIBLE] ', '')})"
            for n, s in upcoming
        ))
    return '\n'.join(parts)


def ai_answer_query(context: str, question: str, profile_schedules: dict = None) -> str:
    """
    Answer a free-form ops question given a pre-built context string.
    If profile_schedules is provided and the question is schedule-related,
    the precomputed schedule fact is embedded directly in the prompt so the
    model only needs to present it, not reason about it.
    Returns the AI answer or an error message.
    """
    import re as _re
    schedule_kws = ['online', 'on shift', 'working', 'start', 'schedule', 'available now', 'shift']
    q = question.lower()
    if profile_schedules and any(kw in q for kw in schedule_kws):
        fact    = _schedule_fact(profile_schedules)
        message = (
            f"VERIFIED SCHEDULE FACTS (use these exactly, do not contradict):\n{fact}\n\n"
            f"EDITOR LOAD:\n{context}\n\n"
            f"The user asked: \"{question}\"\n"
            f"Answer in 2-3 sentences using only the facts above."
        )
    else:
        message = f"{context}\n\nQuestion: {question}"

    response = query_ai(message)
    return response or '⚠️ AI unavailable right now (Ollama timeout or model error).'

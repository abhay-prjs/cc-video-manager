"""
ai_ops.py
AI-powered ops assistant using local Ollama (qwen2.5:7b).
Used by notion_bridge (Telegram) and discord_bot (Discord) for smart
editor assignment and natural-language ops queries.
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
EDITOR_COUNTERS_FILE = os.path.join(BASE_DIR, 'editor_counters.json')

OLLAMA_URL   = 'http://localhost:11434/api/chat'
OLLAMA_MODEL = 'qwen2.5:3b'

SYSTEM_PROMPT = (
    "You are an ops assistant for CC Video Manager, a video editing team. "
    "There are up to 6 editors. Be concise and direct. "
    "For assignments, prefer editors who are: in their shift, below 70% load, "
    "and have fewer missed deadlines. Never assign to someone at 95%+ load unless "
    "absolutely no one else is available. "
    "For free-form questions, answer in 2-3 sentences max."
)


def _load_counters():
    try:
        with open(EDITOR_COUNTERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def query_ai(user_message: str, timeout: int = 90) -> str:
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


def build_context_from_ranked(ranked: list) -> str:
    """
    Build a plain-text context block from _rank_editors() output (notion_bridge format).
    ranked: list of dicts with keys: editor, ratio, available_now, has_schedule,
            mins_until, tier, info{active, capacity}
    """
    counters = _load_counters()
    lines = ['EDITORS (name: load%, availability, all-time stats):']
    for r in ranked:
        pct  = round(r['ratio'] * 100)
        name = r['editor']
        act  = r['info'].get('active', 0)
        cap  = r['info'].get('capacity', 0)

        if r['available_now'] and r['has_schedule']:
            avail = 'in shift now'
        elif not r['has_schedule']:
            avail = 'no schedule set'
        elif r['mins_until'] is not None:
            h, m = divmod(int(r['mins_until']), 60)
            avail = f"available in {h}h {m}m" if h else f"available in {m}m"
        else:
            avail = 'out of shift'

        ec     = counters.get(name, {})
        revs   = ec.get('revisions', 0)
        missed = ec.get('missed_deadlines', 0)
        stats  = f", {revs} revisions, {missed} missed deadlines" if (revs or missed) else ''

        lines.append(f"- {name}: {act}/{cap} videos ({pct}% load), {avail}{stats}")
    return '\n'.join(lines)


def build_context_from_editors(editors: dict) -> str:
    """
    Build a plain-text context block from fetch_editors_from_notion() output (discord_bot format).
    editors: {name: {active, capacity, page_id, ...}}
    """
    counters = _load_counters()
    lines = ['EDITORS (name: load%, all-time stats):']
    for name, info in editors.items():
        act  = info.get('active', 0)
        cap  = info.get('capacity', 1)
        pct  = round((act / cap) * 100) if cap else 0
        ec     = counters.get(name, {})
        revs   = ec.get('revisions', 0)
        missed = ec.get('missed_deadlines', 0)
        stats  = f", {revs} revisions, {missed} missed deadlines" if (revs or missed) else ''
        lines.append(f"- {name}: {act}/{cap} videos ({pct}% load){stats}")
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


def ai_answer_query(context: str, question: str) -> str:
    """
    Answer a free-form ops question given a pre-built context string.
    Returns the AI answer or an error message.
    """
    message  = f"{context}\n\nQuestion: {question}"
    response = query_ai(message)
    return response or '⚠️ AI unavailable right now (Ollama timeout or model error).'

"""
dashboard.py
Flask dashboard for Editing Operations — port 8080.
"""

import json
import os
import re
import threading
import time as _time
import requests
from datetime import datetime, timedelta
from filelock import FileLock
from flask import Flask, render_template_string, request, Response, jsonify
from logger_setup import get_logger

app = Flask(__name__)
logger = get_logger('dashboard')

BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE        = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB    = '44593fbf-4276-47f0-bd12-27289dcb78fd'
ASSIGNMENTS_DB     = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
DELIVERY_HISTORY_DB = '733883073ccf48f2a83953ba2d5ad36d'
DELIVERY_DATE_PROP  = 'date:Delivered Date:start'
VELOCITY_DAYS       = 14

EDITOR_COLORS = {
    'Vex':   '#a855f7',
    'Jied':  '#3b82f6',
    'Karlo': '#22c55e',
    'Jill':  '#06b6d4',
    'Anne':  '#f59e0b',
    'Danna': '#ef4444',
    'Naomi': '#f97316',
    'Iana':  '#ec4899',
    'E1':    '#3b82f6',
    'E2':    '#22c55e',
    'E3':    '#f97316',
    'E4':    '#ec4899',
}

STATUS_COLORS = {
    'Raw':         '#888888',
    'In Progress': '#eab308',
    'Review':      '#3b82f6',
    'Delivered':   '#22c55e',
    'Revision':    '#ef4444',
}

# Colors for editors not in EDITOR_COLORS — must not overlap with any value above
COLOR_POOL = ['#14b8a6', '#8b5cf6', '#f43f5e', '#84cc16', '#0ea5e9', '#d946ef', '#fb923c', '#4ade80']
_dynamic_colors = {}


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Notion helpers ─────────────────────────────────────────────────────────────

def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def query_db(token, db_id):
    url = f'https://api.notion.com/v1/databases/{db_id}/query'
    results, body = [], {}
    while True:
        resp = requests.post(url, headers=notion_headers(token), json=body, timeout=10)
        if not resp.ok:
            break
        data = resp.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        body['start_cursor'] = data['next_cursor']
    return results


def _txt(prop, kind='rich_text'):
    items = prop.get(kind, [])
    return items[0].get('plain_text', '').strip() if items else ''


def _sel(prop):
    return (prop.get('select') or {}).get('name', '').strip()


def _num(prop):
    return prop.get('number') or 0


def _dt(prop):
    return ((prop.get('date') or {}).get('start') or '')


def _url(prop):
    return (prop.get('url') or '').strip()


# ── Color resolution ───────────────────────────────────────────────────────────

def editor_color(name):
    if not name:
        return '#888888'
    if name in EDITOR_COLORS:
        return EDITOR_COLORS[name]
    if name not in _dynamic_colors:
        used = set(EDITOR_COLORS.values()) | set(_dynamic_colors.values())
        available = [c for c in COLOR_POOL if c not in used]
        _dynamic_colors[name] = available[0] if available else COLOR_POOL[len(_dynamic_colors) % len(COLOR_POOL)]
    return _dynamic_colors[name]


def status_color(status):
    return STATUS_COLORS.get(status, '#888888')


# ── HTML helpers ───────────────────────────────────────────────────────────────

def pill(text, color):
    if not text:
        return ''
    bg = color + '1a'
    border = color + '40'
    return (
        f'<span class="pill" style="color:{color};background:{bg};border-color:{border}">'
        f'{text}</span>'
    )


def fmt_date(iso):
    if not iso:
        return ''
    try:
        d = datetime.fromisoformat(iso.split('T')[0])
        return d.strftime('%b %-d')
    except Exception:
        return iso


# ── Data fetchers ──────────────────────────────────────────────────────────────

# ── Live data cache ────────────────────────────────────────────────────────────

_live_cache: dict = {}
_live_cache_lock  = threading.Lock()


def _bg_refresh():
    """Background thread: refreshes Notion data every 30s into _live_cache."""
    _time.sleep(5)  # let Flask finish starting up
    while True:
        try:
            config   = load_config()
            token    = config['notion_token']
            queue    = fetch_queue(token)
            all_rows = fetch_all_queue(token)
            eds      = fetch_editor_stats_full(token)
            today_d  = sum(e['today'] for e in eds)
            sts      = compute_stats(all_rows, today_delivered_count=today_d)
            with _live_cache_lock:
                _live_cache.update({
                    'stats':   sts,
                    'queue':   queue,
                    'editors': eds,
                    'at':      datetime.now().strftime('%b %-d, %Y · %-I:%M %p'),
                })
        except Exception as exc:
            logger.error(f'bg_refresh error: {exc}')
        _time.sleep(30)


threading.Thread(target=_bg_refresh, daemon=True, name='live-cache').start()


DEADLINES_FILE = os.path.join(BASE_DIR, 'deadlines.json')


def load_deadlines():
    """Returns {notion_page_id: entry} from deadlines.json."""
    if not os.path.exists(DEADLINES_FILE):
        return {}
    with open(DEADLINES_FILE) as f:
        data = json.load(f)
    by_page = {}
    for v in data.values():
        pid = v.get('notion_page_id')
        if pid:
            by_page[pid] = v
    return by_page


def fmt_deadline(entry):
    """Returns (text, css_color) for a deadline dict entry."""
    if not entry:
        return '', ''
    if entry.get('indefinite'):
        return 'Indefinite', '#555555'
    due_ts = entry.get('due_ts')
    if not due_ts:
        return '', ''
    diff = due_ts - datetime.now().timestamp()
    abs_h = int(abs(diff) // 3600)
    abs_m = int((abs(diff) % 3600) // 60)
    if diff < 0:
        if abs_h >= 24:
            return f'OVERDUE {abs_h // 24}d', '#ef4444'
        if abs_h >= 1:
            return f'OVERDUE {abs_h}h {abs_m}m', '#ef4444'
        return f'OVERDUE {abs_m}m', '#ef4444'
    h, m = int(diff // 3600), int((diff % 3600) // 60)
    if h >= 48:
        return f'{h // 24}d left', '#22c55e'
    if h >= 6:
        return f'{h}h left', '#eab308'
    return f'{h}h {m}m left', '#ef4444'


def fmt_age(submitted_iso):
    """Returns human-readable elapsed time from submitted ISO string."""
    if not submitted_iso:
        return ''
    try:
        dt  = datetime.fromisoformat(submitted_iso.replace('Z', ''))
        sec = int((datetime.now() - dt).total_seconds())
        if sec < 3600:
            return f'{sec // 60}m'
        if sec < 86400:
            return f'{sec // 3600}h {(sec % 3600) // 60}m'
        d = sec // 86400
        return f'{d}d {(sec % 86400) // 3600}h'
    except Exception:
        return ''


def fetch_raw_folders(token):
    """Returns unassigned (Raw) Active Queue rows with notion_page_id for assignment UI."""
    url  = f'https://api.notion.com/v1/databases/{ACTIVE_QUEUE_DB}/query'
    body = {
        'filter': {'property': 'Status', 'select': {'equals': 'Raw'}},
        'sorts':  [{'timestamp': 'created_time', 'direction': 'ascending'}],
    }
    resp = requests.post(url, headers=notion_headers(token), json=body, timeout=15)
    rows = []
    if resp.ok:
        for page in resp.json().get('results', []):
            p           = page['properties']
            title_rt    = p.get('Video', {}).get('title', [])
            folder_name = title_rt[0].get('plain_text', '') if title_rt else ''
            creator_rt  = p.get('Creator', {}).get('rich_text', [])
            client_name = creator_rt[0].get('plain_text', '') if creator_rt else ''
            notes_rt    = p.get('Notes', {}).get('rich_text', [])
            notes       = notes_rt[0].get('plain_text', '') if notes_rt else ''
            m           = re.search(r'Videos:\s*(\d+)', notes)
            video_count = int(m.group(1)) if m else 0
            drive_link  = _url(p.get('Drive Link', {}))
            m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link or '')
            folder_id   = m2.group(1) if m2 else ''
            proj_num    = p.get('Project #', {}).get('number')
            project_number = f'#{int(proj_num)}' if proj_num else ''
            rows.append({
                'notion_page_id': page['id'],
                'folder_name':    folder_name,
                'client_name':    client_name,
                'video_count':    video_count,
                'folder_id':      folder_id,
                'drive_link':     drive_link or '',
                'project_number': project_number,
                'age':            fmt_age(page.get('created_time', '')),
            })
    return rows


def fetch_editors_list(token):
    """Returns sorted list of editor names from Editor Profiles."""
    url  = f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query'
    resp = requests.post(url, headers=notion_headers(token), json={}, timeout=10)
    names = []
    if resp.ok:
        for page in resp.json().get('results', []):
            title_rt = page['properties'].get('Editor', {}).get('title', [])
            name     = title_rt[0].get('plain_text', '') if title_rt else ''
            cap      = page['properties'].get('Capacity', {}).get('number') or 0
            if name and cap:
                names.append(name)
    return sorted(names)


QUEUE_FILE = os.path.join(BASE_DIR, 'discord_queue.json')
QUEUE_LOCK = FileLock(QUEUE_FILE + '.lock')


def fetch_assignments(token):
    rows = query_db(token, ASSIGNMENTS_DB)
    out = []
    for page in rows:
        p = page['properties']
        creator = _txt(p.get('Creator/Folder', {}), 'title')
        if not creator:
            continue
        primary = _sel(p.get('Primary Editor', {}))
        backup  = (_sel(p.get('Backup Editor', {}))
                   or _sel(p.get('Backup', {}))
                   or _txt(p.get('Backup Editor', {}))
                   or _txt(p.get('Backup', {})))
        vids_raw = p.get('Vids/mo', {})
        if vids_raw.get('number') is not None:
            vids = str(int(_num(vids_raw))) if _num(vids_raw) else ''
        else:
            vids = _txt(vids_raw)
        notes = _txt(p.get('Notes', {}))
        out.append({
            'creator':      creator,
            'primary':      primary,
            'backup':       backup,
            'vids_mo':      vids,
            'notes':        notes,
            'primary_pill': pill(primary, editor_color(primary)),
            'backup_pill':  pill(backup,  editor_color(backup)),
        })
    return out


def fetch_queue(token):
    deadlines = load_deadlines()
    rows = query_db(token, ACTIVE_QUEUE_DB)
    out = []
    for page in rows:
        p      = page['properties']
        status = _sel(p.get('Status', {}))
        if status == 'Delivered':
            continue
        submitted = _dt(p.get('Submitted', {}))
        editor    = _sel(p.get('Editor', {}))
        page_id   = page['id'].replace('-', '')

        dl_entry              = deadlines.get(page['id']) or deadlines.get(page_id)
        deadline_text, dl_clr = fmt_deadline(dl_entry)
        is_overdue            = deadline_text.startswith('OVERDUE')

        drive_link  = _url(p.get('Drive Link', {}))
        m2          = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link or '')
        folder_id   = m2.group(1) if m2 else ''
        notes_rt    = p.get('Notes', {}).get('rich_text', [])
        notes_txt   = notes_rt[0].get('plain_text', '') if notes_rt else ''
        vc_m        = re.search(r'Videos:\s*(\d+)', notes_txt)
        video_count = int(vc_m.group(1)) if vc_m else 0
        proj_num    = p.get('Project #', {}).get('number')
        project_number = f'#{int(proj_num)}' if proj_num else ''

        out.append({
            'creator':        _txt(p.get('Creator', {})),
            'video':          _txt(p.get('Video', {}), 'title'),
            'editor':         editor,
            'status':         status,
            'submitted':      submitted,
            'link':           drive_link,
            'editor_pill':    pill(editor, editor_color(editor)),
            'status_pill':    pill(status, status_color(status)),
            'submitted_fmt':  fmt_date(submitted),
            'age':            fmt_age(page.get('created_time', '')),
            'deadline_text':  deadline_text,
            'deadline_clr':   dl_clr,
            'is_overdue':     is_overdue,
            'notion_page_id': page['id'],
            'folder_id':      folder_id,
            'video_count':    video_count,
            'project_number': project_number,
        })

    out.sort(key=lambda r: (not r['is_overdue'], r['submitted'] or ''))
    return out


def fetch_all_queue(token):
    """All rows including Delivered — for stat computation."""
    rows = query_db(token, ACTIVE_QUEUE_DB)
    out = []
    for page in rows:
        p      = page['properties']
        editor = _sel(p.get('Editor', {}))
        out.append({
            'status':    _sel(p.get('Status', {})),
            'submitted': _dt(p.get('Submitted', {})),
            'editor':    editor,
        })
    return out


def fetch_editor_stats_full(token):
    """
    Per-editor stats card data — sourced live from Notion.
    Adding or removing an editor in Editor Profiles is reflected on next load.
    """
    today_str = datetime.now().date().isoformat()

    # Editor Profiles: base stats
    profile_rows = query_db(token, EDITOR_PROFILES_DB)
    editors = {}
    for page in profile_rows:
        p    = page['properties']
        name = _txt(p.get('Editor', {}), 'title')
        if not name:
            continue
        active   = int(_num(p.get('Active Videos',          {})))
        capacity = int(_num(p.get('Capacity',               {}))) or 70
        pct      = min(100, round(active / capacity * 100)) if capacity else 0
        editors[name] = {
            'name':      name,
            'active':    active,
            'capacity':  capacity,
            'pct':       pct,
            'bar_color': '#ef4444' if pct >= 85 else '#eab308' if pct >= 60 else '#22c55e',
            'week':      int(_num(p.get('Delivered This Week',    {}))),
            'month':     int(_num(p.get('Delivered This Month',   {}))),
            'total':     int(_num(p.get('Total Videos Delivered', {}))),
            'today':     0,
            'color':     editor_color(name),
        }

    # Today's deliveries per editor from Delivery History
    tomorrow_str = (datetime.now().date() + timedelta(days=1)).isoformat()
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    hdrs = notion_headers(token)
    body = {
        'filter': {'and': [
            {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after':  today_str}},
            {'property': DELIVERY_DATE_PROP, 'date': {'on_or_before': tomorrow_str}},
        ]},
        'page_size': 100,
    }
    dh_rows = []
    while True:
        resp = requests.post(url, headers=hdrs, json=body, timeout=15)
        if not resp.ok:
            break
        data = resp.json()
        dh_rows.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        body['start_cursor'] = data['next_cursor']

    today_totals   = {}  # editor -> videos delivered today
    today_folders  = {}  # editor -> folder count today
    for page in dh_rows:
        p      = page['properties']
        editor = (p.get('Editor', {}).get('select') or {}).get('name', '')
        count  = p.get('Videos Completed', {}).get('number') or 0
        if editor:
            today_totals[editor]  = today_totals.get(editor, 0) + count
            today_folders[editor] = today_folders.get(editor, 0) + 1

    for name in editors:
        editors[name]['today']         = today_totals.get(name, 0)
        editors[name]['today_folders'] = today_folders.get(name, 0)

    return list(editors.values())


def compute_stats(all_rows, today_delivered_count=0):
    today    = datetime.now().date()
    week_ago = today - timedelta(days=7)
    active_count  = sum(1 for r in all_rows if r['status'] != 'Delivered')
    in_progress   = sum(1 for r in all_rows if r['status'] == 'In Progress')
    unassigned    = sum(1 for r in all_rows if r['status'] == 'Raw')
    delivered_wk  = 0
    for r in all_rows:
        if r['status'] == 'Delivered' and r['submitted']:
            try:
                sub = datetime.fromisoformat(r['submitted'].split('T')[0]).date()
                if sub >= week_ago:
                    delivered_wk += 1
            except Exception:
                pass
    return {
        'active':          active_count,
        'in_progress':     in_progress,
        'unassigned':      unassigned,
        'delivered_today': today_delivered_count,
        'delivered_wk':    delivered_wk,
    }


def fetch_velocity(token):
    """
    Returns chart-ready data for the last VELOCITY_DAYS days, per editor.
    Editors are sourced live from Editor Profiles so adding/removing editors
    in Notion is reflected on the next page load.
    """
    today  = datetime.now().date()
    cutoff = today - timedelta(days=VELOCITY_DAYS - 1)

    # Pull editor list live from Notion
    editor_rows = query_db(token, EDITOR_PROFILES_DB)
    editors = []
    for page in editor_rows:
        name = _txt(page['properties'].get('Editor', {}), 'title')
        if name:
            editors.append(name)
    editors.sort()

    # Build date labels for last VELOCITY_DAYS days
    date_labels = [(cutoff + timedelta(days=i)).isoformat() for i in range(VELOCITY_DAYS)]

    # Query Delivery History for the window
    url  = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    body = {
        'filter': {'and': [
            {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after': cutoff.isoformat()}},
            {'property': DELIVERY_DATE_PROP, 'date': {'on_or_before': today.isoformat()}},
        ]},
        'page_size': 100,
    }
    cfg     = load_config()
    headers = notion_headers(cfg['notion_token'])
    rows    = []
    while True:
        resp = requests.post(url, headers=headers, json=body, timeout=15)
        if not resp.ok:
            break
        data = resp.json()
        rows.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        body['start_cursor'] = data['next_cursor']

    # Aggregate: {editor: {date: videos}}
    agg = {e: {d: 0 for d in date_labels} for e in editors}
    for page in rows:
        p       = page['properties']
        editor  = (p.get('Editor', {}).get('select') or {}).get('name', '')
        count   = p.get('Videos Completed', {}).get('number') or 0
        date_v  = ((p.get(DELIVERY_DATE_PROP, {}).get('date') or {}).get('start') or '')[:10]
        if editor in agg and date_v in agg[editor]:
            agg[editor][date_v] += count

    # Format labels as "May 1" etc.
    display_labels = [
        (cutoff + timedelta(days=i)).strftime('%-d %b') for i in range(VELOCITY_DAYS)
    ]

    datasets = []
    for editor in editors:
        color = editor_color(editor)
        datasets.append({
            'label':           editor,
            'data':            [agg[editor][d] for d in date_labels],
            'backgroundColor': color + 'cc',
            'borderColor':     color,
            'borderWidth':     1,
            'borderRadius':    3,
        })

    return {'labels': display_labels, 'datasets': datasets}


# ── Template ───────────────────────────────────────────────────────────────────

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Editing Operations Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }

    body {
      font-family: 'Inter', sans-serif;
      background: #0a0a0a;
      color: #ffffff;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
    }

    .container {
      max-width: 1200px;
      margin: 0 auto;
      padding: 48px 28px 80px;
    }

    /* ── Header ── */
    .eyebrow {
      font-size: 11px;
      font-weight: 600;
      color: #888888;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }
    h1 {
      font-size: 26px;
      font-weight: 700;
      color: #ffffff;
      letter-spacing: -0.02em;
      margin-bottom: 6px;
    }
    .subtitle {
      font-size: 13px;
      color: #888888;
      font-weight: 400;
      margin-bottom: 40px;
    }

    /* ── Stat cards ── */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 14px;
      margin-bottom: 44px;
    }
    .stat-card {
      background: #141414;
      border: 1px solid #222222;
      border-radius: 8px;
      padding: 22px 24px 20px;
    }
    .stat-label {
      font-size: 12px;
      font-weight: 500;
      color: #888888;
      margin-bottom: 10px;
      letter-spacing: 0.01em;
    }
    .stat-value {
      font-size: 38px;
      font-weight: 700;
      color: #ffffff;
      line-height: 1;
      letter-spacing: -0.03em;
    }
    .stat-warn  { color: #eab308; }
    .stat-green { color: #22c55e; }

    /* ── Tabs nav ── */
    .tabs-nav {
      display: flex;
      border-bottom: 1px solid #222222;
      margin-bottom: 32px;
      gap: 0;
    }
    .tab-btn {
      display: flex;
      align-items: center;
      gap: 9px;
      background: none;
      border: none;
      border-bottom: 2px solid transparent;
      margin-bottom: -1px;
      padding: 10px 20px 10px 0;
      margin-right: 28px;
      color: #555555;
      font-family: 'Inter', sans-serif;
      font-size: 13.5px;
      font-weight: 500;
      cursor: pointer;
      transition: color 0.15s, border-color 0.15s;
      white-space: nowrap;
    }
    .tab-btn:hover { color: #aaaaaa; }
    .tab-btn.active {
      color: #ffffff;
      border-bottom-color: #ffffff;
    }
    .tab-num {
      font-size: 10px;
      font-weight: 600;
      color: #555555;
      background: #1e1e1e;
      border: 1px solid #2a2a2a;
      border-radius: 4px;
      padding: 2px 6px;
      letter-spacing: 0.02em;
    }
    .tab-btn.active .tab-num {
      color: #888888;
      background: #222222;
      border-color: #333333;
    }

    /* ── Tab panels ── */
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* ── Section header ── */
    .section-header { margin-bottom: 20px; }
    .section-title {
      font-size: 18px;
      font-weight: 700;
      color: #ffffff;
      margin-bottom: 4px;
      letter-spacing: -0.01em;
    }
    .section-sub {
      font-size: 13px;
      color: #555555;
      font-weight: 400;
    }

    /* ── Table ── */
    .table-wrap {
      background: #141414;
      border: 1px solid #222222;
      border-radius: 8px;
      overflow: hidden;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    thead tr {
      background: #0f0f0f;
      border-bottom: 1px solid #222222;
    }
    th {
      text-align: left;
      padding: 11px 16px;
      font-size: 11px;
      font-weight: 600;
      color: #555555;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      white-space: nowrap;
    }
    tbody tr {
      background: #111111;
      border-bottom: 1px solid #191919;
      transition: background 0.1s;
    }
    tbody tr:last-child { border-bottom: none; }
    tbody tr:hover { background: #1a1a1a; }
    td {
      padding: 12px 16px;
      font-size: 13.5px;
      color: #ffffff;
      font-weight: 400;
      vertical-align: middle;
    }
    td.dim { color: #666666; }
    tr.row-overdue { background: #1a0a0a !important; border-left: 2px solid #ef4444; }
    td.creator { font-weight: 600; }
    td.filename {
      color: #aaaaaa;
      font-size: 13px;
      max-width: 220px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    /* ── Pill badge ── */
    .pill {
      display: inline-block;
      font-size: 12px;
      font-weight: 600;
      padding: 3px 10px;
      border-radius: 100px;
      border: 1px solid transparent;
      white-space: nowrap;
      letter-spacing: 0.01em;
    }

    /* ── Drive link ── */
    .drive-link {
      color: #3b82f6;
      text-decoration: none;
      font-size: 13px;
      font-weight: 500;
      white-space: nowrap;
    }
    .drive-link:hover { text-decoration: underline; }

    /* ── Editor stat cards ── */
    .editor-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 14px;
    }
    .editor-card {
      background: #141414;
      border: 1px solid #222222;
      border-radius: 8px;
      padding: 22px 20px 20px;
    }
    .editor-card-header {
      display: flex;
      align-items: center;
      gap: 9px;
      margin-bottom: 18px;
    }
    .editor-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .editor-card-name {
      font-size: 15px;
      font-weight: 700;
      color: #ffffff;
      letter-spacing: -0.01em;
    }
    .editor-load-row {
      display: flex;
      align-items: baseline;
      gap: 8px;
      margin-bottom: 8px;
    }
    .editor-load-label {
      font-size: 11px;
      font-weight: 600;
      color: #555555;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      flex: 1;
    }
    .editor-load-count {
      font-size: 13px;
      font-weight: 600;
    }
    .editor-load-pct {
      font-size: 11px;
      font-weight: 600;
      opacity: 0.7;
    }
    .bar-bg {
      height: 3px;
      background: #222222;
      border-radius: 2px;
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      border-radius: 2px;
    }
    .editor-stat-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px 8px;
    }
    .editor-stat-cell {
      background: #0f0f0f;
      border: 1px solid #1e1e1e;
      border-radius: 6px;
      padding: 10px 12px;
    }
    .editor-stat-val {
      font-size: 22px;
      font-weight: 700;
      color: #ffffff;
      letter-spacing: -0.02em;
      line-height: 1;
      margin-bottom: 4px;
    }
    .editor-stat-lbl {
      font-size: 10px;
      font-weight: 600;
      color: #555555;
      text-transform: uppercase;
      letter-spacing: 0.07em;
    }

    /* ── Empty state ── */
    .empty {
      padding: 40px 16px;
      text-align: center;
      color: #444444;
      font-size: 13px;
    }

    /* ── Updated timestamp ── */
    .updated {
      margin-top: 48px;
      font-size: 11px;
      color: #333333;
      text-align: right;
    }

    /* ── Responsive ── */
    @media (max-width: 1100px) {
      .stats-grid { grid-template-columns: repeat(3, 1fr); }
    }
    @media (max-width: 700px) {
      .stats-grid { grid-template-columns: repeat(2, 1fr); }
      .container { padding: 32px 16px 60px; }
      h1 { font-size: 22px; }
      .stat-value { font-size: 30px; }
      .tab-btn { margin-right: 16px; font-size: 13px; }
      table { font-size: 12px; }
      td { padding: 10px 12px; }
      th { padding: 9px 12px; }
    }
    @media (max-width: 420px) {
      .stats-grid { grid-template-columns: 1fr 1fr; }
    }
    .assign-badge {
      display: inline-block;
      background: #ef4444;
      color: #fff;
      font-size: 10px;
      font-weight: 700;
      border-radius: 10px;
      padding: 1px 6px;
      margin-left: 6px;
      vertical-align: middle;
    }
    .assign-select {
      background: #111;
      color: #ccc;
      border: 1px solid #333;
      border-radius: 6px;
      padding: 5px 8px;
      font-size: 12px;
      font-family: 'Inter', sans-serif;
      min-width: 130px;
    }
    .assign-btn {
      background: #1d4ed8;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 6px 14px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      font-family: 'Inter', sans-serif;
      transition: background 0.15s;
    }
    .assign-btn:hover { background: #2563eb; }
    .assign-btn:disabled { background: #374151; color: #6b7280; cursor: default; }
    .assign-row-done td { opacity: 0.4; }
    .action-btn {
      background: #1f2937;
      color: #d1d5db;
      border: 1px solid #374151;
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      font-family: 'Inter', sans-serif;
      transition: background 0.15s, color 0.15s;
      white-space: nowrap;
    }
    .action-btn:hover { background: #374151; color: #fff; }
    .action-btn.red:hover { background: #7f1d1d; border-color: #ef4444; color: #fca5a5; }
    .action-btn.orange:hover { background: #78350f; border-color: #f59e0b; color: #fcd34d; }
    .action-btn:disabled { opacity: 0.4; cursor: default; }
    .inline-select {
      background: #111;
      color: #ccc;
      border: 1px solid #333;
      border-radius: 6px;
      padding: 4px 6px;
      font-size: 11px;
      font-family: 'Inter', sans-serif;
      min-width: 100px;
    }
    .notes-input {
      background: #111;
      color: #ccc;
      border: 1px solid #333;
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 11px;
      font-family: 'Inter', sans-serif;
      width: 140px;
    }
    .review-card {
      background: #111;
      border: 1px solid #2d2d2d;
      border-left: 3px solid #f59e0b;
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 12px;
    }
    .review-header { display:flex; align-items:flex-start; justify-content:space-between; margin-bottom:10px; flex-wrap:wrap; gap:8px; }
    .review-title { font-size:14px; font-weight:600; color:#f5f5f5; }
    .review-pills { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:10px; }
    .rpill { display:inline-flex; align-items:center; gap:4px; border-radius:20px; padding:3px 10px; font-size:11px; font-weight:600; white-space:nowrap; }
    .rpill-low  { background:#1c2a1c; color:#86efac; border:1px solid #166534; }
    .rpill-medium { background:#2d2000; color:#fbbf24; border:1px solid #92400e; }
    .rpill-high { background:#2d0a0a; color:#fca5a5; border:1px solid #7f1d1d; }
    .rpill-meta { background:#1a1a2e; color:#93c5fd; border:1px solid #1e3a5f; }
    .rpill-time { background:#1a1a1a; color:#888; border:1px solid #333; }
    .review-flags { color: #fcd34d; font-size: 12px; margin-bottom: 10px; }
    .review-meta { color: #888; font-size: 11px; margin-bottom: 10px; }
    .review-links { display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap; }
    .review-drive-link { font-size:11px; color:#60a5fa; text-decoration:none; padding:2px 8px; border:1px solid #1e3a5f; border-radius:4px; background:#0a1628; }
    .review-drive-link:hover { background:#1e3a5f; }
    .recover-card {
      background: #111;
      border: 1px solid #2d2d2d;
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .recover-info { font-size: 13px; }
    .recover-sub { font-size: 11px; color: #666; margin-top: 2px; }
    /* Editor drill-down modal */
    .editor-card { cursor:pointer; transition:transform 0.15s, box-shadow 0.15s; }
    .editor-card:hover { transform:translateY(-2px); box-shadow:0 4px 16px rgba(0,0,0,0.4); }
    .ed-modal-backdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:100; align-items:center; justify-content:center; }
    .ed-modal-backdrop.open { display:flex; }
    .ed-modal { background:#141414; border:1px solid #2d2d2d; border-radius:12px; width:min(780px,95vw); max-height:85vh; overflow-y:auto; padding:28px 32px; position:relative; }
    .ed-modal-close { position:absolute; top:16px; right:20px; background:none; border:none; color:#888; font-size:22px; cursor:pointer; line-height:1; }
    .ed-modal-close:hover { color:#f5f5f5; }
    .ed-modal-title { font-size:20px; font-weight:700; margin-bottom:4px; }
    .ed-modal-sub { font-size:12px; color:#888; margin-bottom:20px; }
    .ed-section-hdr { font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.08em; color:#666; margin:20px 0 10px; border-top:1px solid #222; padding-top:14px; }
    .ed-row { display:flex; align-items:center; gap:10px; padding:9px 0; border-bottom:1px solid #1a1a1a; font-size:13px; }
    .ed-row:last-child { border-bottom:none; }
    .ed-folder { flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:500; }
    .ed-client { color:#888; font-size:12px; min-width:80px; }
    .ed-vids { color:#888; font-size:12px; min-width:50px; text-align:right; }
    .ed-age { color:#555; font-size:11px; min-width:48px; text-align:right; }
    .ed-dl { font-size:11px; min-width:90px; text-align:right; font-weight:600; }
    .ed-empty { color:#555; font-size:13px; padding:12px 0; }
    .ed-perf { display:flex; gap:24px; margin-top:4px; }
    .ed-perf-item { text-align:center; }
    .ed-perf-val { font-size:22px; font-weight:700; }
    .ed-perf-lbl { font-size:11px; color:#666; margin-top:2px; }
    .ed-dlink { color:#60a5fa; text-decoration:none; font-size:11px; margin-left:auto; flex-shrink:0; }
    .ed-dlink:hover { text-decoration:underline; }
    .ed-status-pill { border-radius:20px; padding:2px 8px; font-size:10px; font-weight:600; white-space:nowrap; flex-shrink:0; }
  </style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="eyebrow">Creator Collective</div>
  <h1>Editing Operations Dashboard</h1>
  <div class="subtitle">Creator Collective · Managed by Vexxe · Updated live from Drive + Notion</div>

  <!-- Stat cards -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Active queue</div>
      <div class="stat-value" id="stat-active">{{ stats.active }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">In progress</div>
      <div class="stat-value" id="stat-in-progress">{{ stats.in_progress }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Unassigned</div>
      <div class="stat-value {% if stats.unassigned > 0 %}stat-warn{% endif %}" id="stat-unassigned">{{ stats.unassigned }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Delivered today</div>
      <div class="stat-value stat-green" id="stat-delivered-today">{{ stats.delivered_today }}</div>
    </div>
  </div>

  <!-- Tab nav -->
  <div class="tabs-nav">
    <button class="tab-btn {% if raw_folders %}active{% else %}active{% endif %}" data-target="tab-assign">
      <span class="tab-num">Tab 1</span>Assign
      {% if raw_folders %}<span class="assign-badge">{{ raw_folders|length }}</span>{% endif %}
    </button>
    <button class="tab-btn" data-target="tab-queue">
      <span class="tab-num">Tab 2</span>Active Queue
    </button>
    <button class="tab-btn" data-target="tab-load">
      <span class="tab-num">Tab 3</span>Editor Stats
    </button>
    <button class="tab-btn" data-target="tab-velocity">
      <span class="tab-num">Tab 4</span>Delivery Velocity
    </button>
  </div>

  <!-- Tab 1 — Assign (unassigned folders) -->
  <div id="tab-assign" class="tab-panel active">
    <div class="section-header">
      <div class="section-title">Unassigned Folders</div>
      <div class="section-sub">Select an editor for each Raw folder — assignment is sent to Discord immediately</div>
    </div>
    {% if raw_folders %}
    <div id="assign-toast" style="display:none;margin-bottom:14px;padding:10px 16px;border-radius:8px;background:#166534;color:#bbf7d0;font-size:13px;font-weight:500;"></div>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Client</th>
          <th>Folder</th>
          <th>Videos</th>
          <th>Age</th>
          <th>Link</th>
          <th>Assign To</th>
          <th></th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for row in raw_folders %}
        <tr id="assign-row-{{ loop.index }}">
          <td class="dim">{{ row.project_number or '—' }}</td>
          <td class="creator">{{ row.client_name }}</td>
          <td class="filename">{{ row.folder_name }}</td>
          <td>{{ row.video_count }}</td>
          <td class="dim">{{ row.age }}</td>
          <td>
            {% if row.drive_link %}
            <a class="drive-link" href="{{ row.drive_link }}" target="_blank" rel="noopener">Drive ↗</a>
            {% endif %}
          </td>
          <td>
            <select class="assign-select" id="sel-{{ loop.index }}">
              <option value="">— select editor —</option>
              {% for name in editor_names %}
              <option value="{{ name }}">{{ name }}</option>
              {% endfor %}
            </select>
          </td>
          <td>
            <button
              class="assign-btn"
              onclick="doAssign({{ loop.index }}, '{{ row.notion_page_id }}', '{{ row.folder_id }}', '{{ row.folder_name | replace("'", "\\'") }}', '{{ row.client_name | replace("'", "\\'") }}', {{ row.video_count }}, '{{ row.project_number }}')"
            >Assign →</button>
          </td>
          <td>
            <button
              class="action-btn red"
              title="Ignore this folder — watcher will skip it going forward"
              onclick="doIgnore({{ loop.index }}, '{{ row.folder_id }}')"
            >🚫</button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty">✅ No unassigned folders right now.</div>
    {% endif %}
  </div>

  <!-- Tab 2 — Active Queue -->
  <div id="tab-queue" class="tab-panel">
    <div class="section-header">
      <div class="section-title">Active Queue</div>
      <div class="section-sub">Live view · auto-populated from Drive uploads · editors update status</div>
    </div>
    <div class="table-wrap">
      {% if queue %}
      <table>
        <thead>
          <tr>
            <th>Creator</th>
            <th>Video</th>
            <th>Editor</th>
            <th>Status</th>
            <th>Age</th>
            <th>Deadline</th>
            <th>Link</th>
            <th>Reassign</th>
            <th>Revision</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {% for row in queue %}
          <tr id="qrow-{{ loop.index }}" data-page-id="{{ row.notion_page_id }}" {% if row.is_overdue %}class="row-overdue"{% endif %}>
            <td class="creator">{{ row.creator }}</td>
            <td class="filename" title="{{ row.video }}">{{ row.video }}</td>
            <td class="editor-cell">{{ row.editor_pill | safe }}</td>
            <td class="status-cell">{{ row.status_pill | safe }}</td>
            <td class="dim age-cell">{{ row.age }}</td>
            <td class="deadline-cell">
              {% if row.deadline_text %}
              <span style="font-size:12px;font-weight:600;color:{{ row.deadline_clr }}">{{ row.deadline_text }}</span>
              {% else %}<span class="dim">—</span>{% endif %}
            </td>
            <td>
              {% if row.link %}
              <a class="drive-link" href="{{ row.link }}" target="_blank" rel="noopener">Drive ↗</a>
              {% endif %}
            </td>
            <td>
              <div style="display:flex;gap:4px;align-items:center;">
                <select class="inline-select" id="reassign-sel-{{ loop.index }}">
                  <option value="">—</option>
                  {% for name in editor_names %}<option value="{{ name }}"{% if name == row.editor %} selected{% endif %}>{{ name }}</option>{% endfor %}
                </select>
                <button class="action-btn" onclick="doReassign({{ loop.index }}, '{{ row.notion_page_id }}', '{{ row.folder_id }}', '{{ row.video | replace("'","\\'") }}', '{{ row.creator | replace("'","\\'") }}', {{ row.video_count }}, '{{ row.editor }}', '{{ row.project_number }}')">→</button>
              </div>
            </td>
            <td>
              <div style="display:flex;gap:4px;align-items:center;">
                <input class="notes-input" id="rev-notes-{{ loop.index }}" placeholder="notes…" />
                <button class="action-btn orange" onclick="doRevision({{ loop.index }}, '{{ row.notion_page_id }}', '{{ row.folder_id }}', '{{ row.video | replace("'","\\'") }}', '{{ row.creator | replace("'","\\'") }}', {{ row.video_count }}, '{{ row.editor }}')">🔄</button>
              </div>
            </td>
            <td>
              <button class="action-btn red" onclick="doRemove({{ loop.index }}, '{{ row.notion_page_id }}', '{{ row.folder_id }}', '{{ row.video | replace("'","\\'") }}', '{{ row.creator | replace("'","\\'") }}', {{ row.video_count }}, '{{ row.editor }}', '{{ row.status }}')">🗑</button>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty">No active videos in queue.</div>
      {% endif %}
    </div>
  </div>

  <!-- Pending Reviews (flagged completions) -->
  <div id="pending-reviews-section" style="margin-top:32px;{% if not pending_reviews %}display:none;{% endif %}">
    <div class="section-header">
      <div class="section-title" style="color:#fcd34d;">⚠️ Pending Reviews (<span id="pending-reviews-count">{{ pending_reviews|length }}</span>)</div>
      <div class="section-sub">Flagged completions needing your approval before finalizing</div>
    </div>
    <div id="pending-reviews-list">
    {% for rv in pending_reviews %}
    <div class="review-card" id="rv-{{ rv.review_id }}">
      <div class="review-header">
        <div>
          <div class="review-title">{{ rv.editor_name }} → {{ rv.client_name }} / {{ rv.folder_name | trim }}</div>
        </div>
        <button class="action-btn" style="background:#166534;border-color:#22c55e;color:#bbf7d0;flex-shrink:0;"
          onclick="doApprove('{{ rv.review_id }}')">✅ Approve & Finalize</button>
      </div>
      <div class="review-pills">
        <span class="rpill rpill-meta">{{ rv.videos_done }} submitted</span>
        <span class="rpill rpill-meta">Drive: {{ rv.drive_count if rv.drive_count is not none else '—' }}</span>
        {% if rv.created_ist %}<span class="rpill rpill-time">🕐 {{ rv.created_ist }}</span>{% endif %}
        {% for cf in rv.classified_flags %}
        <span class="rpill rpill-{{ cf.severity }}">
          {% if cf.severity == 'high' %}🔴{% elif cf.severity == 'medium' %}🟡{% else %}🟢{% endif %}
          {% if 'count mismatch' in cf.text.lower() %}
            Count off ({{ rv.videos_done }} said vs {{ rv.drive_count }} in Drive)
          {% elif 'not found' in cf.text.lower() %}
            Edited folder not found
          {% elif 'mismatch' in cf.text.lower() and cf.severity == 'low' %}
            Trailing space in name (safe)
          {% elif 'mismatch' in cf.text.lower() %}
            Wrong folder name
          {% else %}
            {{ cf.text | replace('⚠️ ', '') }}
          {% endif %}
        </span>
        {% endfor %}
      </div>
      {% if rv.drive_link %}
      <div class="review-links">
        <a class="review-drive-link" href="{{ rv.drive_link }}" target="_blank" rel="noopener">📁 Raw Footage Folder ↗</a>
      </div>
      {% endif %}
    </div>
    {% endfor %}
    </div>
  </div>

  <!-- Recovered folders bin -->
  {% if removed_folders %}
  <div style="margin-top:32px;">
    <div class="section-header">
      <div class="section-title">🗑 Removed Folders ({{ removed_folders|length }})</div>
      <div class="section-sub">Archived from queue — click Recover to restore</div>
    </div>
    {% for page_id, rf in removed_folders.items() %}
    <div class="recover-card" id="recover-{{ page_id }}">
      <div>
        <div class="recover-info">{{ rf.client_name }} / {{ rf.folder_name }}</div>
        <div class="recover-sub">{{ rf.status }} · removed {{ rf.removed_at[:10] if rf.removed_at else '—' }}</div>
      </div>
      <button class="action-btn" onclick="doRecover('{{ page_id }}')">♻️ Recover</button>
    </div>
    {% endfor %}
  </div>
  {% endif %}

  <!-- Tab 3 — Editor Stats -->
  <div id="tab-load" class="tab-panel">
    <div class="section-header">
      <div class="section-title">Editor Stats</div>
      <div class="section-sub">Live from Notion · auto-reflects added or removed editors</div>
    </div>
    {% if editors %}
    <div class="editor-grid">
      {% for e in editors %}
      <div class="editor-card" data-editor="{{ e.name }}" title="Click to see {{ e.name }}'s folders">
        <div class="editor-card-header">
          <span class="editor-dot" style="background:{{ e.color }}"></span>
          <span class="editor-card-name">{{ e.name }}</span>
        </div>

        <div class="editor-load-row">
          <span class="editor-load-label">Load</span>
          <span class="editor-load-count" style="color:{{ e.bar_color }}">{{ e.active }} / {{ e.capacity }}</span>
          <span class="editor-load-pct" style="color:{{ e.bar_color }}">{{ e.pct }}%</span>
        </div>
        <div class="bar-bg" style="margin-bottom:20px">
          <div class="bar-fill" style="width:{{ e.pct }}%;background:{{ e.bar_color }}"></div>
        </div>

        <div class="editor-stat-grid">
          <div class="editor-stat-cell">
            <div class="editor-stat-val" style="color:{{ e.color }}">{{ e.today }}</div>
            <div class="editor-stat-lbl">Today</div>
          </div>
          <div class="editor-stat-cell">
            <div class="editor-stat-val">{{ e.week }}</div>
            <div class="editor-stat-lbl">This Week</div>
          </div>
          <div class="editor-stat-cell">
            <div class="editor-stat-val">{{ e.month }}</div>
            <div class="editor-stat-lbl">This Month</div>
          </div>
          <div class="editor-stat-cell">
            <div class="editor-stat-val">{{ e.total }}</div>
            <div class="editor-stat-lbl">All Time</div>
          </div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">No editor profiles found.</div>
    {% endif %}
  </div>

  <!-- Tab 4 — Delivery Velocity -->
  <div id="tab-velocity" class="tab-panel">
    <div class="section-header">
      <div class="section-title">Delivery Velocity</div>
      <div class="section-sub">Videos delivered per day · last 14 days · per editor · live from Notion</div>
    </div>
    <div class="table-wrap" style="padding: 28px 24px;">
      {% if velocity.datasets %}
      <canvas id="velocityChart" height="90"></canvas>
      {% else %}
      <div class="empty">No delivery history in the last 14 days.</div>
      {% endif %}
    </div>
  </div>

  <div class="updated">Last updated {{ updated }}</div>

</div>

<!-- Editor drill-down modal -->
<div class="ed-modal-backdrop" id="edModalBackdrop" onclick="closeEditorModal(event)">
  <div class="ed-modal" id="edModal">
    <button class="ed-modal-close" onclick="closeEditorModal(null, true)">✕</button>
    <div class="ed-modal-title" id="edModalName"></div>
    <div class="ed-modal-sub" id="edModalSub"></div>
    <div id="edModalBody"><div class="ed-empty">Loading…</div></div>
  </div>
</div>

<script>
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.target).classList.add('active');
    });
  });

  {% if velocity.datasets %}
  const velocityData = {{ velocity | tojson }};
  new Chart(document.getElementById('velocityChart'), {
    type: 'bar',
    data: velocityData,
    options: {
      responsive: true,
      plugins: {
        legend: {
          labels: { color: '#aaaaaa', font: { family: 'Inter', size: 12 } }
        },
        tooltip: {
          backgroundColor: '#1a1a1a',
          borderColor: '#333333',
          borderWidth: 1,
          titleColor: '#ffffff',
          bodyColor: '#aaaaaa',
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y} video${ctx.parsed.y !== 1 ? 's' : ''}`
          }
        }
      },
      scales: {
        x: {
          stacked: true,
          grid: { color: '#1e1e1e' },
          ticks: { color: '#666666', font: { family: 'Inter', size: 11 } }
        },
        y: {
          stacked: true,
          beginAtZero: true,
          grid: { color: '#1e1e1e' },
          ticks: {
            color: '#666666',
            font: { family: 'Inter', size: 11 },
            stepSize: 1,
            precision: 0
          }
        }
      }
    }
  });
  {% endif %}

  // ── Live data polling ──────────────────────────────────────────────────────
  const _EDITOR_COLORS = {{ EDITOR_COLORS | tojson }};
  const _STATUS_COLORS = {'Raw':'#888888','In Progress':'#eab308','Review':'#3b82f6','Delivered':'#22c55e','Revision':'#ef4444'};

  function _pill(text, color) {
    if (!text) return '';
    return `<span class="pill" style="color:${color};background:${color}1a;border-color:${color}40">${text}</span>`;
  }
  function _editorColor(name) {
    return _EDITOR_COLORS[name] || '#888888';
  }

  async function liveUpdate() {
    let d;
    try {
      const r = await fetch('/api/live');
      if (!r.ok) return;
      d = await r.json();
    } catch(e) { return; }
    if (!d.ok) return;

    // Stats numbers
    const statMap = {active:'stat-active', in_progress:'stat-in-progress', unassigned:'stat-unassigned', delivered_today:'stat-delivered-today'};
    for (const [k, id] of Object.entries(statMap)) {
      const el = document.getElementById(id);
      if (el) {
        el.textContent = d.stats[k];
        if (id === 'stat-unassigned') {
          el.classList.toggle('stat-warn', d.stats[k] > 0);
        }
      }
    }

    // Queue rows — update dynamic cells, fade out delivered/removed rows
    const queueById = {};
    for (const row of d.queue) queueById[row.notion_page_id] = row;
    document.querySelectorAll('#tab-queue tbody tr[data-page-id]').forEach(tr => {
      const pid = tr.dataset.pageId;
      const row = queueById[pid];
      if (!row) {
        tr.style.transition = 'opacity 0.5s';
        tr.style.opacity = '0.25';
        setTimeout(() => tr.remove(), 600);
        return;
      }
      const sc = tr.querySelector('.status-cell');
      if (sc) sc.innerHTML = _pill(row.status, _STATUS_COLORS[row.status] || '#888');
      const ec = tr.querySelector('.editor-cell');
      if (ec) ec.innerHTML = _pill(row.editor, _editorColor(row.editor));
      const ac = tr.querySelector('.age-cell');
      if (ac) ac.textContent = row.age;
      const dc = tr.querySelector('.deadline-cell');
      if (dc) dc.innerHTML = row.deadline_text
        ? `<span style="font-size:12px;font-weight:600;color:${row.deadline_clr}">${row.deadline_text}</span>`
        : '<span class="dim">—</span>';
      tr.classList.toggle('row-overdue', !!row.is_overdue);
    });

    // Editor cards — update load bars and stat numbers in-place
    const editorGrid = document.querySelector('#tab-load .editor-grid');
    if (editorGrid) {
      d.editors.forEach(e => {
        const card = editorGrid.querySelector(`[data-editor="${e.name}"]`);
        if (!card) return;
        const lc = card.querySelector('.editor-load-count');
        const lp = card.querySelector('.editor-load-pct');
        const bf = card.querySelector('.bar-fill');
        if (lc) { lc.textContent = `${e.active} / ${e.capacity}`; lc.style.color = e.bar_color; }
        if (lp) { lp.textContent = `${e.pct}%`; lp.style.color = e.bar_color; }
        if (bf) { bf.style.width = `${e.pct}%`; bf.style.background = e.bar_color; }
        const vals = card.querySelectorAll('.editor-stat-val');
        if (vals.length >= 4) {
          vals[0].textContent = e.today;
          vals[1].textContent = e.week;
          vals[2].textContent = e.month;
          vals[3].textContent = e.total;
        }
      });
    }

    // Timestamp
    const upd = document.querySelector('.updated');
    if (upd && d.at) upd.textContent = 'Last updated ' + d.at;
  }

  setInterval(liveUpdate, 15000);

  function showToast(msg, ok=true) {
    const t = document.getElementById('assign-toast');
    if (!t) return;
    t.style.background = ok ? '#166534' : '#7f1d1d';
    t.style.color = ok ? '#bbf7d0' : '#fca5a5';
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 4000);
  }

  async function apiPost(url, body) {
    const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    return r.json();
  }

  async function doReassign(idx, notionPageId, folderId, folderName, clientName, videoCount, oldEditor, projectNumber) {
    const newEditor = document.getElementById('reassign-sel-' + idx).value;
    if (!newEditor || newEditor === oldEditor) { alert('Select a different editor.'); return; }
    const data = await apiPost('/reassign', { notion_page_id: notionPageId, folder_id: folderId, folder_name: folderName, client_name: clientName, old_editor: oldEditor, new_editor: newEditor, video_count: videoCount, project_number: projectNumber });
    if (data.ok) { showToast('✅ ' + data.message); document.getElementById('qrow-' + idx).classList.add('assign-row-done'); }
    else { showToast('❌ ' + (data.error || 'Error'), false); }
  }

  async function doRevision(idx, notionPageId, folderId, folderName, clientName, videoCount, editorName) {
    if (!editorName) { alert('No editor set on this folder.'); return; }
    const notes = document.getElementById('rev-notes-' + idx).value.trim();
    const data = await apiPost('/revision', { notion_page_id: notionPageId, folder_id: folderId, folder_name: folderName, client_name: clientName, editor_name: editorName, video_count: videoCount, notes });
    if (data.ok) { showToast('🔄 ' + data.message); }
    else { showToast('❌ ' + (data.error || 'Error'), false); }
  }

  async function doRemove(idx, notionPageId, folderId, folderName, clientName, videoCount, editorName, status) {
    if (!confirm('Archive ' + clientName + ' / ' + folderName + '?')) return;
    const data = await apiPost('/remove', { notion_page_id: notionPageId, folder_id: folderId, folder_name: folderName, client_name: clientName, editor_name: editorName, video_count: videoCount, status });
    if (data.ok) { showToast('🗑 ' + data.message); document.getElementById('qrow-' + idx).classList.add('assign-row-done'); }
    else { showToast('❌ ' + (data.error || 'Error'), false); }
  }

  async function doApprove(reviewId) {
    const data = await apiPost('/approve', { review_id: reviewId });
    if (data.ok) {
      showToast('✅ Approved — finalizing');
      removeReviewCard(reviewId);
    } else { showToast('❌ ' + (data.error || 'Error'), false); }
  }

  function removeReviewCard(reviewId) {
    const card = document.getElementById('rv-' + reviewId);
    if (!card) return;
    card.style.transition = 'opacity 0.4s';
    card.style.opacity = '0';
    setTimeout(() => {
      card.remove();
      const remaining = document.querySelectorAll('#pending-reviews-list .review-card').length;
      const countEl = document.getElementById('pending-reviews-count');
      if (countEl) countEl.textContent = remaining;
      if (remaining === 0) {
        const sec = document.getElementById('pending-reviews-section');
        if (sec) sec.style.display = 'none';
      }
    }, 400);
  }

  // Poll pending reviews every 10s to pick up Discord approvals
  async function pollPendingReviews() {
    try {
      const resp = await fetch('/api/pending_reviews');
      if (!resp.ok) return;
      const data = await resp.json();
      if (!data.ok) return;
      const activeIds = new Set(data.reviews.map(r => r.review_id));
      document.querySelectorAll('#pending-reviews-list .review-card').forEach(card => {
        const rid = card.id.replace('rv-', '');
        if (!activeIds.has(rid)) removeReviewCard(rid);
      });
    } catch(e) {}
  }
  setInterval(pollPendingReviews, 10000);

  async function doRecover(pageId) {
    const data = await apiPost('/recover', { notion_page_id: pageId });
    if (data.ok) { showToast('♻️ Folder recovered'); document.getElementById('recover-' + pageId).style.opacity = '0.4'; }
    else { showToast('❌ ' + (data.error || 'Error'), false); }
  }

  async function doIgnore(idx, folderId) {
    if (!confirm('Ignore this folder? The watcher will skip it on all future scans.')) return;
    const data = await apiPost('/ignore', { folder_id: folderId });
    if (data.ok) {
      const row = document.getElementById('assign-row-' + idx);
      row.style.opacity = '0.35';
      row.style.transition = 'opacity 0.4s';
      setTimeout(() => row.remove(), 500);
      showToast('🚫 Folder ignored');
    } else { showToast('❌ ' + (data.error || 'Error'), false); }
  }

  // ── Editor drill-down modal ─────────────────────────────────────────────
  document.querySelectorAll('.editor-card').forEach(card => {
    card.addEventListener('click', () => openEditorModal(card.dataset.editor));
  });

  function closeEditorModal(evt, force) {
    if (force || evt.target === document.getElementById('edModalBackdrop')) {
      document.getElementById('edModalBackdrop').classList.remove('open');
    }
  }
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeEditorModal(null, true); });

  async function openEditorModal(name) {
    document.getElementById('edModalName').textContent = name;
    document.getElementById('edModalSub').textContent = 'Loading…';
    document.getElementById('edModalBody').innerHTML = '<div class="ed-empty">Loading…</div>';
    document.getElementById('edModalBackdrop').classList.add('open');

    let d;
    try {
      const resp = await fetch('/api/editor_detail', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name}),
      });
      d = await resp.json();
    } catch(e) {
      document.getElementById('edModalBody').innerHTML = '<div class="ed-empty">Failed to load.</div>';
      return;
    }
    if (!d.ok) {
      document.getElementById('edModalBody').innerHTML = '<div class="ed-empty">' + (d.error || 'Error') + '</div>';
      return;
    }

    document.getElementById('edModalSub').textContent =
      d.active.length + ' active · ' + d.deliveries.length + ' deliveries (30d)';

    const statusColor = s => {
      if (s === 'In Progress') return 'background:#1a2d1a;color:#86efac;border:1px solid #166534';
      if (s === 'Review') return 'background:#1a1f2e;color:#93c5fd;border:1px solid #1e3a5f';
      return 'background:#222;color:#888;border:1px solid #333';
    };

    let html = '';

    // Active folders section
    html += '<div class="ed-section-hdr">Active Folders (' + d.active.length + ')</div>';
    if (d.active.length === 0) {
      html += '<div class="ed-empty">No active folders right now.</div>';
    } else {
      d.active.forEach(r => {
        const dlStyle = r.overdue ? 'color:#ef4444' : (r.deadline_text ? 'color:' + (r.deadline_clr || '#888') : 'color:#555');
        html += '<div class="ed-row" style="flex-direction:column;align-items:stretch;gap:5px;">' +
          '<div style="display:flex;align-items:center;gap:10px;">' +
            '<span class="ed-folder" title="' + r.folder_name + '">' + r.folder_name + '</span>' +
            '<span class="ed-client">' + r.client + '</span>' +
            '<span class="ed-status-pill" style="' + statusColor(r.status) + '">' + r.status + '</span>' +
            '<span class="ed-vids" style="margin-left:auto">' + r.videos + ' vids</span>' +
            '<span class="ed-age">' + r.age + '</span>' +
            (r.deadline_text ? '<span class="ed-dl" style="' + dlStyle + '">' + r.deadline_text + '</span>' : '') +
          '</div>' +
          '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            (r.drive_link ? '<a class="ed-dlink" style="margin-left:0;font-size:11px;padding:2px 8px;border:1px solid #1e3a5f;border-radius:4px;background:#0a1628;" href="' + r.drive_link + '" target="_blank" rel="noopener">📁 Drive ↗</a>' : '') +
            (r.notion_page_id ? '<a class="ed-dlink" style="margin-left:0;font-size:11px;padding:2px 8px;border:1px solid #3b2f6e;border-radius:4px;background:#1a1030;color:#c4b5fd;" href="https://www.notion.so/' + r.notion_page_id.replace(/-/g, \'\') + '" target="_blank" rel="noopener">📋 Notion ↗</a>' : '') +
          '</div>' +
          '</div>';
      });
    }

    // Performance
    html += '<div class="ed-section-hdr">Performance</div>';
    html += '<div class="ed-perf">' +
      '<div class="ed-perf-item"><div class="ed-perf-val" style="color:#ef4444">' + d.revisions + '</div><div class="ed-perf-lbl">Revisions sent back</div></div>' +
      '<div class="ed-perf-item"><div class="ed-perf-val" style="color:#f59e0b">' + d.missed_deadlines + '</div><div class="ed-perf-lbl">Missed deadlines</div></div>' +
      '</div>';

    // Recent deliveries
    html += '<div class="ed-section-hdr">Recent Deliveries — last 30 days (' + d.deliveries.length + ')</div>';
    if (d.deliveries.length === 0) {
      html += '<div class="ed-empty">No deliveries in the last 30 days.</div>';
    } else {
      d.deliveries.forEach(r => {
        html += '<div class="ed-row" style="flex-direction:column;align-items:stretch;gap:5px;">' +
          '<div style="display:flex;align-items:center;gap:10px;">' +
            '<span class="ed-folder">' + r.folder_name + '</span>' +
            '<span class="ed-client">' + r.client + '</span>' +
            '<span class="ed-vids" style="margin-left:auto">' + r.videos + ' vids</span>' +
            '<span class="ed-dl" style="color:#555">' + r.date + '</span>' +
          '</div>' +
          ((r.drive_link || r.notion_page_id) ? '<div style="display:flex;gap:8px;flex-wrap:wrap;">' +
            (r.drive_link ? '<a class="ed-dlink" style="margin-left:0;font-size:11px;padding:2px 8px;border:1px solid #1e3a5f;border-radius:4px;background:#0a1628;" href="' + r.drive_link + '" target="_blank" rel="noopener">📁 Drive ↗</a>' : '') +
            (r.notion_page_id ? '<a class="ed-dlink" style="margin-left:0;font-size:11px;padding:2px 8px;border:1px solid #3b2f6e;border-radius:4px;background:#1a1030;color:#c4b5fd;" href="https://www.notion.so/' + r.notion_page_id.replace(/-/g, \'\') + '" target="_blank" rel="noopener">📋 Notion ↗</a>' : '') +
            '</div>' : '') +
          '</div>';
      });
    }

    document.getElementById('edModalBody').innerHTML = html;
  }

  async function doAssign(idx, notionPageId, folderId, folderName, clientName, videoCount, projectNumber) {
    const sel = document.getElementById('sel-' + idx);
    const btn = document.querySelector('#assign-row-' + idx + ' .assign-btn');
    const editor = sel.value;
    if (!editor) { alert('Please select an editor first.'); return; }

    btn.disabled = true;
    btn.textContent = 'Assigning…';

    try {
      const resp = await fetch('/assign', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          editor, notion_page_id: notionPageId, folder_id: folderId,
          folder_name: folderName, client_name: clientName,
          video_count: videoCount, project_number: projectNumber,
        }),
      });
      const data = await resp.json();
      if (data.ok) {
        const row = document.getElementById('assign-row-' + idx);
        row.classList.add('assign-row-done');
        btn.textContent = '✓ Sent';
        const toast = document.getElementById('assign-toast');
        toast.textContent = '✅ ' + data.message;
        toast.style.display = 'block';
        setTimeout(() => { toast.style.display = 'none'; }, 4000);
      } else {
        btn.disabled = false;
        btn.textContent = 'Assign →';
        alert('Error: ' + (data.error || 'Unknown error'));
      }
    } catch(e) {
      btn.disabled = false;
      btn.textContent = 'Assign →';
      alert('Network error: ' + e.message);
    }
  }
</script>
</body>
</html>"""


# ── Route ──────────────────────────────────────────────────────────────────────

@app.route('/assign', methods=['POST'])
def do_assign():
    data          = request.get_json(force=True) or {}
    editor        = data.get('editor', '').strip()
    notion_page_id = data.get('notion_page_id', '').strip()
    folder_id     = data.get('folder_id', '').strip()
    folder_name   = data.get('folder_name', '').strip()
    client_name   = data.get('client_name', '').strip()
    video_count   = int(data.get('video_count', 0))
    project_number = data.get('project_number', '').strip()

    if not all([editor, folder_id, folder_name, client_name]):
        return jsonify({'ok': False, 'error': 'Missing required fields'}), 400

    try:
        with QUEUE_LOCK:
            try:
                with open(QUEUE_FILE) as f:
                    queue = json.load(f)
            except Exception:
                queue = []
            queue.append({
                'client_name':        client_name,
                'folder_name':        folder_name,
                'video_count':        video_count,
                'folder_id':          folder_id,
                'editor_name':        editor,
                'notion_queue_page_id': notion_page_id,
                'project_number':     project_number,
            })
            with open(QUEUE_FILE, 'w') as f:
                json.dump(queue, f, indent=2)
        logger.info(f"dashboard /assign: {client_name}/{folder_name} → {editor}")
        return jsonify({'ok': True, 'message': f'Assigned {folder_name} → {editor}'})
    except Exception as e:
        logger.error(f"dashboard /assign error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


def _enqueue(item):
    """Write one item to discord_queue.json for discord_bot to process."""
    with QUEUE_LOCK:
        try:
            with open(QUEUE_FILE) as f:
                q = json.load(f)
        except Exception:
            q = []
        q.append(item)
        with open(QUEUE_FILE, 'w') as f:
            json.dump(q, f, indent=2)


@app.route('/reassign', methods=['POST'])
def do_reassign():
    d = request.get_json(force=True) or {}
    required = ['notion_page_id', 'folder_id', 'folder_name', 'client_name', 'new_editor']
    if not all(d.get(k, '').strip() for k in required):
        return jsonify({'ok': False, 'error': 'Missing required fields'}), 400
    try:
        _enqueue({
            'type':           'dashboard_reassign',
            'notion_page_id': d['notion_page_id'],
            'folder_id':      d['folder_id'],
            'folder_name':    d['folder_name'],
            'client_name':    d['client_name'],
            'old_editor':     d.get('old_editor', ''),
            'new_editor':     d['new_editor'],
            'video_count':    int(d.get('video_count', 0)),
            'project_number': d.get('project_number', ''),
        })
        logger.info(f"dashboard /reassign: {d['client_name']}/{d['folder_name']} → {d['new_editor']}")
        return jsonify({'ok': True, 'message': f"Reassigned {d['folder_name']} → {d['new_editor']}"})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/revision', methods=['POST'])
def do_revision():
    d = request.get_json(force=True) or {}
    required = ['notion_page_id', 'folder_name', 'client_name', 'editor_name']
    if not all(d.get(k, '').strip() for k in required):
        return jsonify({'ok': False, 'error': 'Missing required fields'}), 400
    try:
        _enqueue({
            'type':           'dashboard_revision',
            'notion_page_id': d['notion_page_id'],
            'folder_id':      d.get('folder_id', ''),
            'folder_name':    d['folder_name'],
            'client_name':    d['client_name'],
            'editor_name':    d['editor_name'],
            'video_count':    int(d.get('video_count', 0)),
            'notes':          d.get('notes', '').strip(),
        })
        logger.info(f"dashboard /revision: {d['client_name']}/{d['folder_name']} → {d['editor_name']}")
        return jsonify({'ok': True, 'message': f"Revision opened for {d['folder_name']}"})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/approve', methods=['POST'])
def do_approve():
    d = request.get_json(force=True) or {}
    review_id = d.get('review_id', '').strip()
    if not review_id:
        return jsonify({'ok': False, 'error': 'Missing review_id'}), 400
    try:
        _enqueue({'type': 'dashboard_approve', 'review_id': review_id})
        logger.info(f"dashboard /approve: {review_id}")
        return jsonify({'ok': True, 'message': 'Approved and finalizing'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/remove', methods=['POST'])
def do_remove():
    d = request.get_json(force=True) or {}
    if not d.get('notion_page_id', '').strip():
        return jsonify({'ok': False, 'error': 'Missing notion_page_id'}), 400
    try:
        _enqueue({'type': 'dashboard_remove', **d})
        logger.info(f"dashboard /remove: {d.get('folder_name')}")
        return jsonify({'ok': True, 'message': f"Removed {d.get('folder_name')}"})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/recover', methods=['POST'])
def do_recover():
    d = request.get_json(force=True) or {}
    notion_page_id = d.get('notion_page_id', '').strip()
    if not notion_page_id:
        return jsonify({'ok': False, 'error': 'Missing notion_page_id'}), 400
    try:
        _enqueue({'type': 'dashboard_recover', 'notion_page_id': notion_page_id})
        logger.info(f"dashboard /recover: {notion_page_id}")
        return jsonify({'ok': True, 'message': 'Folder recovered'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/editor_detail', methods=['POST'])
def api_editor_detail():
    d = request.get_json(force=True) or {}
    name = d.get('name', '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Missing name'}), 400

    config = load_config()
    token  = config['notion_token']
    deadlines = load_deadlines()

    # Active Queue rows for this editor (non-delivered)
    active_rows = []
    for page in query_db(token, ACTIVE_QUEUE_DB):
        p      = page['properties']
        editor = _sel(p.get('Editor', {}))
        if editor != name:
            continue
        status = _sel(p.get('Status', {}))
        if status == 'Delivered':
            continue
        drive_link   = _url(p.get('Drive Link', {}))
        m2           = re.search(r'/folders/([a-zA-Z0-9_-]+)', drive_link or '')
        folder_id    = m2.group(1) if m2 else ''
        notes_rt     = p.get('Notes', {}).get('rich_text', [])
        notes_txt    = notes_rt[0].get('plain_text', '') if notes_rt else ''
        vc_m         = re.search(r'Videos:\s*(\d+)', notes_txt)
        video_count  = int(vc_m.group(1)) if vc_m else 0
        dl_entry     = deadlines.get(page['id']) or deadlines.get(page['id'].replace('-', ''))
        dl_text, dl_clr = fmt_deadline(dl_entry)
        active_rows.append({
            'folder_name':    _txt(p.get('Video', {}), 'title'),
            'client':         _txt(p.get('Creator', {})),
            'status':         status,
            'videos':         video_count,
            'age':            fmt_age(page.get('created_time', '')),
            'deadline_text':  dl_text,
            'deadline_clr':   dl_clr,
            'overdue':        dl_text.startswith('OVERDUE'),
            'drive_link':     drive_link or '',
            'notion_page_id': page['id'],
        })
    active_rows.sort(key=lambda r: (not r['overdue'], r['age']))

    # Delivery History last 30 days for this editor
    cutoff = (datetime.now().date() - timedelta(days=30)).isoformat()
    today  = (datetime.now().date() + timedelta(days=1)).isoformat()
    url    = f'https://api.notion.com/v1/databases/{DELIVERY_HISTORY_DB}/query'
    hdrs   = notion_headers(token)
    body   = {
        'filter': {'and': [
            {'property': 'Editor', 'select': {'equals': name}},
            {'property': DELIVERY_DATE_PROP, 'date': {'on_or_after':  cutoff}},
            {'property': DELIVERY_DATE_PROP, 'date': {'on_or_before': today}},
        ]},
        'sorts': [{'property': DELIVERY_DATE_PROP, 'direction': 'descending'}],
        'page_size': 50,
    }
    dh_rows = []
    while True:
        resp = requests.post(url, headers=hdrs, json=body, timeout=15)
        if not resp.ok:
            break
        data = resp.json()
        dh_rows.extend(data.get('results', []))
        if not data.get('has_more'):
            break
        body['start_cursor'] = data['next_cursor']

    deliveries = []
    for page in dh_rows:
        p       = page['properties']
        date_v  = ((p.get(DELIVERY_DATE_PROP, {}).get('date') or {}).get('start') or '')[:10]
        count   = p.get('Videos Completed', {}).get('number') or 0
        folder_tt = p.get('Folder', {}).get('title', [])
        folder_n  = folder_tt[0].get('plain_text', '') if folder_tt else ''
        client_rt = p.get('Client', {}).get('rich_text', [])
        client_n  = client_rt[0].get('plain_text', '') if client_rt else ''
        drive_link = (p.get('Drive Link', {}) or {}).get('url', '') or ''
        try:
            d_label = datetime.strptime(date_v, '%Y-%m-%d').strftime('%b %-d')
        except Exception:
            d_label = date_v
        deliveries.append({'folder_name': folder_n, 'client': client_n, 'date': d_label, 'videos': count, 'drive_link': drive_link, 'notion_page_id': page['id']})

    # Performance counters
    perf = {}
    try:
        counters_file = os.path.join(BASE_DIR, 'editor_counters.json')
        with open(counters_file) as f:
            perf = json.load(f).get(name, {})
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'name': name,
        'active': active_rows,
        'deliveries': deliveries,
        'revisions': perf.get('revisions', 0),
        'missed_deadlines': perf.get('missed_deadlines', 0),
    })


@app.route('/api/live', methods=['GET'])
def api_live():
    with _live_cache_lock:
        if not _live_cache:
            return jsonify({'ok': False, 'error': 'cache warming up'})
        return jsonify({
            'ok':      True,
            'stats':   _live_cache['stats'],
            'queue':   _live_cache['queue'],
            'editors': _live_cache['editors'],
            'at':      _live_cache.get('at', ''),
        })


@app.route('/api/pending_reviews', methods=['GET'])
def api_pending_reviews():
    pending_reviews_file = os.path.join(BASE_DIR, 'pending_reviews.json')
    try:
        with open(pending_reviews_file) as f:
            raw = json.load(f)
    except Exception:
        return jsonify({'ok': True, 'reviews': []})
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    reviews = []
    for rv in raw.values():
        rv = dict(rv)
        fid = rv.get('folder_id', '')
        rv['drive_link'] = f'https://drive.google.com/drive/folders/{fid}' if fid else ''
        try:
            ts = datetime.fromisoformat(rv['created_at']).astimezone(IST)
            rv['created_ist'] = ts.strftime('%-I:%M %p IST, %b %-d')
        except Exception:
            rv['created_ist'] = ''
        reviews.append({'review_id': rv.get('review_id', ''), 'created_ist': rv['created_ist']})
    return jsonify({'ok': True, 'reviews': reviews})


@app.route('/ignore', methods=['POST'])
def do_ignore():
    d = request.get_json(force=True) or {}
    folder_id = d.get('folder_id', '').strip()
    if not folder_id:
        return jsonify({'ok': False, 'error': 'Missing folder_id'}), 400
    try:
        ignored_file = os.path.join(BASE_DIR, 'ignored_folders.json')
        from filelock import FileLock
        lock = FileLock(ignored_file + '.lock')
        with lock:
            try:
                with open(ignored_file) as f:
                    ids = json.load(f)
            except Exception:
                ids = []
            if folder_id not in ids:
                ids.append(folder_id)
            with open(ignored_file, 'w') as f:
                json.dump(ids, f, indent=2)
        logger.info(f"dashboard /ignore: {folder_id}")
        return jsonify({'ok': True, 'message': 'Folder ignored'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/')
def index():
    config      = load_config()
    token       = config['notion_token']
    queue       = fetch_queue(token)
    all_rows    = fetch_all_queue(token)
    editors     = fetch_editor_stats_full(token)
    velocity    = fetch_velocity(token)
    raw_folders  = fetch_raw_folders(token)
    editor_names = fetch_editors_list(token)
    today_delivered = sum(e['today'] for e in editors)
    stats        = compute_stats(all_rows, today_delivered_count=today_delivered)
    updated      = datetime.now().strftime('%b %-d, %Y · %-I:%M %p')

    # Pending reviews (flagged completions needing approval)
    pending_reviews_file = os.path.join(BASE_DIR, 'pending_reviews.json')
    try:
        with open(pending_reviews_file) as f:
            raw_reviews = json.load(f).values()
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
        pending_reviews = []
        for rv in raw_reviews:
            rv = dict(rv)
            # Drive link from folder_id
            fid = rv.get('folder_id', '')
            rv['drive_link'] = f'https://drive.google.com/drive/folders/{fid}' if fid else ''
            # IST timestamp
            try:
                from datetime import datetime as _dt
                ts = _dt.fromisoformat(rv['created_at']).astimezone(IST)
                rv['created_ist'] = ts.strftime('%-I:%M %p IST, %b %-d')
            except Exception:
                rv['created_ist'] = ''
            # Classify each flag
            classified = []
            for flag in rv.get('flags', []):
                if 'trailing' in flag.lower() or (
                    'mismatch' in flag.lower() and
                    flag.lower().replace("'", "").replace('"', '').endswith(flag.lower().rstrip("' ").rstrip('"').rstrip())
                ):
                    # Distinguish trailing-space mismatch from real mismatch
                    if 'not found' in flag:
                        severity = 'high'
                    elif 'count mismatch' in flag.lower():
                        severity = 'medium'
                    else:
                        # Check if it's just a trailing-space difference
                        import re as _re
                        m = _re.search(r"editor said '(.+?)' but assigned folder was '(.+?)'", flag)
                        if m and m.group(1).strip() == m.group(2).strip():
                            severity = 'low'
                        else:
                            severity = 'high'
                else:
                    if 'not found' in flag:
                        severity = 'high'
                    elif 'count mismatch' in flag.lower():
                        severity = 'medium'
                    else:
                        severity = 'low'
                classified.append({'text': flag, 'severity': severity})
            rv['classified_flags'] = classified
            pending_reviews.append(rv)
    except Exception:
        pending_reviews = []

    # Removed folders (recoverable)
    removed_folders_file = os.path.join(BASE_DIR, 'removed_folders.json')
    try:
        with open(removed_folders_file) as f:
            removed_folders = json.load(f)
    except Exception:
        removed_folders = {}

    return render_template_string(
        TEMPLATE,
        stats=stats,
        queue=queue,
        editors=editors,
        velocity=velocity,
        raw_folders=raw_folders,
        editor_names=editor_names,
        pending_reviews=pending_reviews,
        removed_folders=removed_folders,
        updated=updated,
        EDITOR_COLORS=EDITOR_COLORS,
    )


VALID_SERVICES = {'discord_bot', 'notion_bridge', 'gdrive_watcher', 'drive_webhook', 'dashboard'}

@app.route('/logs')
def view_logs():
    service = request.args.get('service', 'discord_bot')
    lines   = min(int(request.args.get('lines', 50)), 500)

    if service not in VALID_SERVICES:
        return Response(f'Unknown service. Valid: {", ".join(sorted(VALID_SERVICES))}', status=400, mimetype='text/plain')

    log_file = os.path.join(BASE_DIR, 'logs', f'{service}.log')
    if not os.path.exists(log_file):
        return Response(f'No log file found for {service}', status=404, mimetype='text/plain')

    with open(log_file, encoding='utf-8') as f:
        all_lines = f.readlines()

    tail = ''.join(all_lines[-lines:])
    return Response(tail, mimetype='text/plain')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)

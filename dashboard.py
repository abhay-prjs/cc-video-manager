"""
dashboard.py
Flask dashboard for Editing Operations — port 8080.
"""

import json
import os
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, Response
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

        out.append({
            'creator':       _txt(p.get('Creator', {})),
            'video':         _txt(p.get('Video', {}), 'title'),
            'editor':        editor,
            'status':        status,
            'submitted':     submitted,
            'link':          _url(p.get('Drive Link', {})),
            'editor_pill':   pill(editor, editor_color(editor)),
            'status_pill':   pill(status, status_color(status)),
            'submitted_fmt': fmt_date(submitted),
            'age':           fmt_age(page.get('created_time', '')),
            'deadline_text': deadline_text,
            'deadline_clr':  dl_clr,
            'is_overdue':    is_overdue,
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
      <div class="stat-value">{{ stats.active }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">In progress</div>
      <div class="stat-value">{{ stats.in_progress }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Unassigned</div>
      <div class="stat-value {% if stats.unassigned > 0 %}stat-warn{% endif %}">{{ stats.unassigned }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Delivered today</div>
      <div class="stat-value stat-green">{{ stats.delivered_today }}</div>
    </div>
  </div>

  <!-- Tab nav -->
  <div class="tabs-nav">
    <button class="tab-btn active" data-target="tab-queue">
      <span class="tab-num">Tab 1</span>Active Queue
    </button>
    <button class="tab-btn" data-target="tab-load">
      <span class="tab-num">Tab 2</span>Editor Stats
    </button>
    <button class="tab-btn" data-target="tab-velocity">
      <span class="tab-num">Tab 3</span>Delivery Velocity
    </button>
  </div>

  <!-- Tab 1 — Active Queue -->
  <div id="tab-queue" class="tab-panel active">
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
          </tr>
        </thead>
        <tbody>
          {% for row in queue %}
          <tr {% if row.is_overdue %}class="row-overdue"{% endif %}>
            <td class="creator">{{ row.creator }}</td>
            <td class="filename" title="{{ row.video }}">{{ row.video }}</td>
            <td>{{ row.editor_pill | safe }}</td>
            <td>{{ row.status_pill | safe }}</td>
            <td class="dim">{{ row.age }}</td>
            <td>
              {% if row.deadline_text %}
              <span style="font-size:12px;font-weight:600;color:{{ row.deadline_clr }}">{{ row.deadline_text }}</span>
              {% else %}
              <span class="dim">—</span>
              {% endif %}
            </td>
            <td>
              {% if row.link %}
              <a class="drive-link" href="{{ row.link }}" target="_blank" rel="noopener">Drive ↗</a>
              {% endif %}
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

  <!-- Tab 3 — Editor Stats -->
  <div id="tab-load" class="tab-panel">
    <div class="section-header">
      <div class="section-title">Editor Stats</div>
      <div class="section-sub">Live from Notion · auto-reflects added or removed editors</div>
    </div>
    {% if editors %}
    <div class="editor-grid">
      {% for e in editors %}
      <div class="editor-card">
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

  setTimeout(() => location.reload(), 60000);
</script>
</body>
</html>"""


# ── Route ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    config      = load_config()
    token       = config['notion_token']
    queue       = fetch_queue(token)
    all_rows    = fetch_all_queue(token)
    editors     = fetch_editor_stats_full(token)
    velocity    = fetch_velocity(token)
    today_delivered = sum(e['today'] for e in editors)
    stats       = compute_stats(all_rows, today_delivered_count=today_delivered)
    updated     = datetime.now().strftime('%b %-d, %Y · %-I:%M %p')
    return render_template_string(
        TEMPLATE,
        stats=stats,
        queue=queue,
        editors=editors,
        velocity=velocity,
        updated=updated,
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

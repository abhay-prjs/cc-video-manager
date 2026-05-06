"""
dashboard.py
Flask dashboard for Editing Operations — port 8080.
"""

import json
import os
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template_string

app = Flask(__name__)

BASE_DIR           = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE        = os.path.join(BASE_DIR, 'config.json')
ACTIVE_QUEUE_DB    = '44593fbf-4276-47f0-bd12-27289dcb78fd'
ASSIGNMENTS_DB     = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'

EDITOR_COLORS = {
    'Vex':   '#a855f7',
    'Jied':  '#3b82f6',
    'Karlo': '#22c55e',
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

COLOR_POOL = ['#3b82f6', '#22c55e', '#f97316', '#ec4899', '#eab308', '#a855f7']
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
        _dynamic_colors[name] = COLOR_POOL[len(_dynamic_colors) % len(COLOR_POOL)]
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
    rows = query_db(token, ACTIVE_QUEUE_DB)
    out = []
    for page in rows:
        p = page['properties']
        status = _sel(p.get('Status', {}))
        if status == 'Delivered':
            continue
        submitted = _dt(p.get('Submitted', {}))
        editor = _sel(p.get('Editor', {}))
        out.append({
            'creator':      _txt(p.get('Creator', {})),
            'video':        _txt(p.get('Video', {}), 'title'),
            'editor':       editor,
            'status':       status,
            'submitted':    submitted,
            'link':         _url(p.get('Drive Link', {})),
            'editor_pill':  pill(editor, editor_color(editor)),
            'status_pill':  pill(status, status_color(status)),
            'submitted_fmt': fmt_date(submitted),
        })
    out.sort(key=lambda r: r['submitted'] or '')
    return out


def fetch_all_queue(token):
    """All rows including Delivered — for stat computation."""
    rows = query_db(token, ACTIVE_QUEUE_DB)
    out = []
    for page in rows:
        p = page['properties']
        out.append({
            'status':    _sel(p.get('Status', {})),
            'submitted': _dt(p.get('Submitted', {})),
        })
    return out


def fetch_editors(token):
    rows = query_db(token, EDITOR_PROFILES_DB)
    out = []
    for page in rows:
        p = page['properties']
        name     = _txt(p.get('Editor', {}), 'title')
        active   = int(_num(p.get('Active Videos', {})))
        capacity = int(_num(p.get('Capacity', {}))) or 70
        if not name:
            continue
        pct = min(100, round(active / capacity * 100)) if capacity else 0
        bar_color = '#ef4444' if pct >= 85 else '#eab308' if pct >= 60 else '#22c55e'
        out.append({
            'name':      name,
            'active':    active,
            'capacity':  capacity,
            'pct':       pct,
            'bar_color': bar_color,
        })
    return out


def compute_stats(all_rows):
    today    = datetime.now().date()
    week_ago = today - timedelta(days=7)
    active_count = sum(1 for r in all_rows if r['status'] != 'Delivered')
    in_progress  = sum(1 for r in all_rows if r['status'] == 'In Progress')
    delivered_wk = 0
    turnarounds  = []
    for r in all_rows:
        if r['status'] == 'Delivered' and r['submitted']:
            try:
                sub = datetime.fromisoformat(r['submitted'].split('T')[0]).date()
                if sub >= week_ago:
                    delivered_wk += 1
                days = (today - sub).days
                if 0 <= days <= 30:
                    turnarounds.append(days)
            except Exception:
                pass
    avg_turn = f"{sum(turnarounds)/len(turnarounds):.1f}d" if turnarounds else '—'
    return {
        'active':       active_count,
        'in_progress':  in_progress,
        'delivered_wk': delivered_wk,
        'avg_turn':     avg_turn,
    }


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
      grid-template-columns: repeat(4, 1fr);
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

    /* ── Editor load cards ── */
    .editor-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
    }
    .editor-card {
      background: #141414;
      border: 1px solid #222222;
      border-radius: 8px;
      padding: 26px 24px 22px;
      flex: 1 1 200px;
      min-width: 180px;
    }
    .editor-card-name {
      font-size: 15px;
      font-weight: 700;
      color: #ffffff;
      margin-bottom: 6px;
      letter-spacing: -0.01em;
    }
    .editor-card-count {
      font-size: 24px;
      font-weight: 600;
      color: #ffffff;
      letter-spacing: -0.02em;
      margin-bottom: 14px;
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
    @media (max-width: 900px) {
      .stats-grid { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 600px) {
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
      .editor-grid { flex-direction: column; }
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
      <div class="stat-label">Delivered this wk</div>
      <div class="stat-value">{{ stats.delivered_wk }}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg turnaround</div>
      <div class="stat-value">{{ stats.avg_turn }}</div>
    </div>
  </div>

  <!-- Tab nav -->
  <div class="tabs-nav">
    <button class="tab-btn active" data-target="tab-assignments">
      <span class="tab-num">Tab 1</span>Creator Assignments
    </button>
    <button class="tab-btn" data-target="tab-queue">
      <span class="tab-num">Tab 2</span>Active Queue
    </button>
    <button class="tab-btn" data-target="tab-load">
      <span class="tab-num">Tab 3</span>Editor Load
    </button>
  </div>

  <!-- Tab 1 — Creator Assignments -->
  <div id="tab-assignments" class="tab-panel active">
    <div class="section-header">
      <div class="section-title">Creator Assignments</div>
      <div class="section-sub">Master view · one row per creator · Vex updates weekly</div>
    </div>
    <div class="table-wrap">
      {% if assignments %}
      <table>
        <thead>
          <tr>
            <th>Creator</th>
            <th>Primary Editor</th>
            <th>Backup</th>
            <th>Vids/mo</th>
            <th>Notes</th>
          </tr>
        </thead>
        <tbody>
          {% for row in assignments %}
          <tr>
            <td class="creator">{{ row.creator }}</td>
            <td>{{ row.primary_pill | safe }}</td>
            <td>{{ row.backup_pill | safe }}</td>
            <td class="dim">{{ row.vids_mo }}</td>
            <td class="dim">{{ row.notes }}</td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% else %}
      <div class="empty">No assignment rows found.</div>
      {% endif %}
    </div>
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
            <th>Submitted</th>
            <th>Link</th>
          </tr>
        </thead>
        <tbody>
          {% for row in queue %}
          <tr>
            <td class="creator">{{ row.creator }}</td>
            <td class="filename" title="{{ row.video }}">{{ row.video }}</td>
            <td>{{ row.editor_pill | safe }}</td>
            <td>{{ row.status_pill | safe }}</td>
            <td class="dim">{{ row.submitted_fmt }}</td>
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

  <!-- Tab 3 — Editor Load -->
  <div id="tab-load" class="tab-panel">
    <div class="section-header">
      <div class="section-title">Editor Load</div>
      <div class="section-sub">Live count · Vex rebalances when anyone trends red</div>
    </div>
    {% if editors %}
    <div class="editor-grid">
      {% for e in editors %}
      <div class="editor-card">
        <div class="editor-card-name">{{ e.name }}</div>
        <div class="editor-card-count">{{ e.active }} / {{ e.capacity }}</div>
        <div class="bar-bg">
          <div class="bar-fill" style="width:{{ e.pct }}%;background:{{ e.bar_color }}"></div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty">No editor profiles found.</div>
    {% endif %}
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

  setTimeout(() => location.reload(), 60000);
</script>
</body>
</html>"""


# ── Route ──────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    config      = load_config()
    token       = config['notion_token']
    assignments = fetch_assignments(token)
    queue       = fetch_queue(token)
    all_rows    = fetch_all_queue(token)
    editors     = fetch_editors(token)
    stats       = compute_stats(all_rows)
    updated     = datetime.now().strftime('%b %-d, %Y · %-I:%M %p')
    return render_template_string(
        TEMPLATE,
        stats=stats,
        assignments=assignments,
        queue=queue,
        editors=editors,
        updated=updated,
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)

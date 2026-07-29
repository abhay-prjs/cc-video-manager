"""
reconcile_dashboard_names.py

One-off audit before turning on the dashboard bridge. The only thing joining
this bot to trycreatorcollective.com is a person's name, normalized to
[a-z0-9], so anything that doesn't match cleanly silently breaks:

  * unmatched EDITOR  → the ticket mirrors, but lands unassigned for staff
  * unmatched CREATOR → the dashboard hard-422s and the assignment never
                        mirrors at all. This is the one that hurts.
  * dashboard editor missing from Notion → the reverse direction can't find a
                        Discord channel, so the assignment never reaches them.

Run it anywhere both credentials are available (nothing is written):

    NOTION_TOKEN=... \
    SUPABASE_URL=https://ykbqkymfylokukuisvac.supabase.co \
    SUPABASE_SERVICE_ROLE_KEY=... \
    python3 reconcile_dashboard_names.py

Both fall back to config.json (notion_token, supabase_url,
supabase_service_role_key) when run on the bot box.
"""

import os
import re
import sys
import json

import requests

ACTIVE_QUEUE_DB = '44593fbf-4276-47f0-bd12-27289dcb78fd'
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'
CREATOR_ASSIGNMENTS_DB = 'cead1699-21dc-4b0c-b0b6-00cf31c5fa29'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def name_key(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _config():
    try:
        with open(os.path.join(BASE_DIR, 'config.json')) as f:
            return json.load(f)
    except Exception:
        return {}


def setting(env_name, config_key):
    """Env wins, config.json fills in — so this runs the same on the bot box
    (where the credentials already live) as anywhere else."""
    return os.environ.get(env_name) or _config().get(config_key, '')


def notion_query_all(token, db_id):
    headers = {
        'Authorization': f'Bearer {token}',
        'Notion-Version': '2022-06-28',
        'Content-Type': 'application/json',
    }
    results, cursor = [], None
    while True:
        body = {'start_cursor': cursor} if cursor else {}
        res = requests.post(
            f'https://api.notion.com/v1/databases/{db_id}/query',
            headers=headers, json=body, timeout=30,
        )
        res.raise_for_status()
        data = res.json()
        results.extend(data.get('results', []))
        if not data.get('has_more'):
            return results
        cursor = data.get('next_cursor')


def prop_text(props, key):
    """Pull a plain string out of a title / rich_text / select property."""
    p = props.get(key) or {}
    for kind in ('title', 'rich_text'):
        if p.get(kind):
            return p[kind][0].get('plain_text', '')
    if p.get('select'):
        return p['select'].get('name', '')
    return ''


def supabase_headers(key):
    """Supabase has two key formats and they authenticate differently.

    Legacy service_role keys are JWTs ("eyJ..."), accepted in both the apikey
    header and as a Bearer token. The newer secret keys ("sb_secret_...") are
    NOT JWTs — passing one as Bearer makes PostgREST try to parse it as an
    end-user token and 401 the whole request. Those go in apikey alone.
    """
    headers = {'apikey': key}
    if key.startswith('eyJ'):
        headers['authorization'] = f'Bearer {key}'
    return headers


def supabase_profiles(url, key, role):
    res = requests.get(
        f'{url.rstrip("/")}/rest/v1/profiles',
        headers=supabase_headers(key),
        params={'select': 'id,full_name,email,role', 'role': f'eq.{role}'},
        timeout=30,
    )
    if res.status_code == 401:
        raise SystemExit(
            '401 from Supabase. The key is wrong, revoked, or truncated on '
            'paste — check its length matches the value in .env.local '
            '(sb_secret_ keys are 41 chars).'
        )
    res.raise_for_status()
    return res.json()


def report(title, notion_names, profiles, hard_fail_note):
    print(f'\n{"=" * 72}\n{title}\n{"=" * 72}')
    by_key = {}
    for p in profiles:
        k = name_key(p.get('full_name'))
        if k:
            by_key.setdefault(k, []).append(p)

    matched, ambiguous, missing = [], [], []
    for n in sorted(notion_names):
        k = name_key(n)
        hits = by_key.get(k, [])
        if len(hits) == 1:
            matched.append((n, hits[0]))
        elif hits:
            ambiguous.append((n, hits))
        else:
            loose = [
                p for kk, ps in by_key.items() for p in ps
                if kk and (kk in k or k in kk)
            ]
            if len(loose) == 1:
                matched.append((n, loose[0]))
            elif loose:
                ambiguous.append((n, loose))
            else:
                missing.append(n)

    print(f'\n✅ matched ({len(matched)})')
    for n, p in matched:
        exact = name_key(n) == name_key(p.get("full_name"))
        print(f'   {n:<28} → {p.get("full_name")}{"" if exact else "   [loose match]"}')

    if ambiguous:
        print(f'\n⚠️  ambiguous ({len(ambiguous)}) — resolves to nobody, treated as missing')
        for n, ps in ambiguous:
            print(f'   {n:<28} → {", ".join(p.get("full_name") or "?" for p in ps)}')

    if missing:
        print(f'\n❌ no dashboard profile ({len(missing)})')
        for n in missing:
            print(f'   {n}')
        print(f'\n   {hard_fail_note}')

    return missing + [n for n, _ in ambiguous]


def main():
    token = setting('NOTION_TOKEN', 'notion_token')
    url = setting('SUPABASE_URL', 'supabase_url')
    key = setting('SUPABASE_SERVICE_ROLE_KEY', 'supabase_service_role_key')
    missing = [
        name
        for name, value in (
            ('NOTION_TOKEN / notion_token', token),
            ('SUPABASE_URL / supabase_url', url),
            ('SUPABASE_SERVICE_ROLE_KEY / supabase_service_role_key', key),
        )
        if not value
    ]
    if missing:
        sys.exit('missing: ' + ', '.join(missing))

    editors = notion_query_all(token, EDITOR_PROFILES_DB)
    editor_names = {
        prop_text(p['properties'], 'Editor') for p in editors
    } - {''}

    # Clients appear on every Active Queue row; Creator Assignments is the
    # canonical list but Active Queue catches anyone missing from it.
    creators = set()
    for p in notion_query_all(token, CREATOR_ASSIGNMENTS_DB):
        for k in ('Creator', 'Client', 'Name'):
            v = prop_text(p['properties'], k)
            if v:
                creators.add(v)
                break
    for p in notion_query_all(token, ACTIVE_QUEUE_DB):
        v = prop_text(p['properties'], 'Client')
        if v:
            creators.add(v)
    creators -= {''}

    profile_editors = supabase_profiles(url, key, 'editor')
    profile_students = supabase_profiles(url, key, 'student')

    bad_editors = report(
        'EDITORS — Notion Editor Profiles vs dashboard profiles(role=editor)',
        editor_names, profile_editors,
        'fix: create these as placeholder editor profiles in the dashboard '
        '(is_placeholder=true) so assignments can attach to them.',
    )
    bad_creators = report(
        'CREATORS — Notion clients vs dashboard profiles(role=student)',
        creators, profile_students,
        'fix: these BLOCK mirroring entirely (422). Correct full_name in '
        'Supabase profiles, or rename the client in Notion to match.',
    )

    # Reverse direction: a dashboard editor with no Notion row can be assigned
    # in the UI, but the bot will have nowhere to post the Discord embed.
    notion_keys = {name_key(n) for n in editor_names}
    orphan_editors = [
        p.get('full_name') for p in profile_editors
        if name_key(p.get('full_name')) and name_key(p.get('full_name')) not in notion_keys
    ]
    print(f'\n{"=" * 72}\nDASHBOARD EDITORS MISSING FROM NOTION\n{"=" * 72}')
    if orphan_editors:
        print(
            '\n   assigning a ticket to these in the dashboard will retry 5x '
            'then give up:\n'
        )
        for n in sorted(orphan_editors):
            print(f'   {n}')
    else:
        print('\n   none — every dashboard editor has a Notion row.')

    print(f'\n{"=" * 72}')
    blocking = len(bad_creators)
    print(
        f'summary: {blocking} creator name(s) will hard-fail, '
        f'{len(bad_editors)} editor name(s) will land unassigned, '
        f'{len(orphan_editors)} dashboard editor(s) unreachable from the bot.'
    )
    print('=' * 72)


if __name__ == '__main__':
    main()

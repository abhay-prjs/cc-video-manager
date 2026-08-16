"""One-off: assign the 14 pending website-native batches per the plan Vex
approved in chat (2026-08-15). Kermit Thomas's "INVO — hook + demo x76"
ticket carries a glitched video_count of 412 in the dashboard feed (does not
match its own format count, ×76) — Vex confirmed the real number is ~42, so
this script sends video_count=42 to the dashboard bridge for that one entry
only (plan baked into /tmp/web_assign_plan_20260815.json).

Zyon (off today) and Jewel (158v hidden in active batches already) get
nothing this pass.

Follows the exact real /assign path for website batches (see discord_bot.py
assign_command, the WEBSITE_BATCH_PREFIX branch): POST to the dashboard's
assign endpoint via post_dashboard_assignment-equivalent payload, then drop
the entry from pending_ops_assigns.json and tidy the #assignments dropdown
message to a plain "Assigned" embed. The dashboard site is expected to POST
back a 'notify' command shortly after, which the running discord-bot picks
up and turns into the actual editor/creator Discord pings + the
dashboard_batches.json active entry (handle_cc_dashboard_notify) — this
script does not send those pings directly.

Dry-run by default; pass --live to actually write.
"""
import argparse
import json
import os
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_OPS_ASSIGNS_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')

with open('/tmp/web_assign_plan_20260815.json') as f:
    PLAN = json.load(f)

EDITOR_DISCORD_IDS = {
    'Josh': '1486290181915934731', 'AJ': '450656575376457728',
    'Gabo': '574044603326660609', 'Storm': '737970480962600960',
    'Danie': '876781619250356264', 'Steven': '574822666024779796',
    'Aki': '728166498597601363', 'Jill': '727298088393244672',
    'Naomi': '749162007596498964',
}

EDITOR_CHANNEL_IDS = {
    'Josh': '1533171017210396682', 'AJ': '1533160508499165254',
    'Gabo': '1529907225475416174', 'Storm': '1523935821969752086',
    'Danie': '1521870638703312979', 'Steven': '1519154569312079924',
    'Aki': '1513878774050062469', 'Jill': '1498666772637814927',
    'Naomi': '1498666723535224842',
}

USER_AGENT = 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)'


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def post_dashboard_assign(config, payload):
    url = config.get('dashboard_url')
    secret = config.get('dashboard_secret')
    if not url or not secret:
        print('  ! dashboard_url/dashboard_secret missing from config.json — cannot post')
        return False
    resp = requests.post(
        url, headers={'Authorization': f'Bearer {secret}'}, json=payload, timeout=15
    )
    if not resp.ok:
        print(f'  ! dashboard POST failed: {resp.status_code} {resp.text[:300]}')
    return resp.ok


def tidy_ops_assign_message(config, row):
    bot_token = config['discord_bot_token']
    headers = {
        'Authorization': f'Bot {bot_token}',
        'Content-Type': 'application/json',
        'User-Agent': USER_AGENT,
    }
    embed = {
        'title': f"✅ Assigned to {row['editor']}",
        'color': 0x2ECC71,
        'fields': [
            {'name': 'Creator', 'value': row['student_name'], 'inline': True},
            {'name': 'Batch', 'value': row['folder_name'], 'inline': True},
        ],
    }
    url = f"https://discord.com/api/v10/channels/{row['channel_id']}/messages/{row['msg_id']}"
    resp = requests.patch(url, headers=headers, json={'embeds': [embed], 'components': []}, timeout=15)
    if not resp.ok:
        print(f"  ! Discord message tidy failed for {row['msg_id']}: {resp.status_code} {resp.text[:200]}")
    return resp.ok


def remove_pending_entry(msg_id):
    if not os.path.exists(PENDING_OPS_ASSIGNS_FILE):
        return
    with open(PENDING_OPS_ASSIGNS_FILE) as f:
        data = json.load(f)
    data.pop(str(msg_id), None)
    with open(PENDING_OPS_ASSIGNS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()

    print(f'Plan: {len(PLAN)} website batches.')
    for row in PLAN:
        flag = '  <- corrected from 412 (dashboard glitch)' if row['ticket_id'] == 'd6c16fd6-bc27-440d-9d84-2e5a67cb5ff2' else ''
        print(f"  {row['student_name']:22} / {row['folder_name']:45} ({row['video_count']:>3}v) -> {row['editor']}{flag}")

    if not args.live:
        print('\nDry run — no writes. Re-run with --live to execute.')
        return

    config = load_config()
    print()
    for row in PLAN:
        uid = EDITOR_DISCORD_IDS.get(row['editor'], '')
        ok = post_dashboard_assign(config, {
            'ticket_id':         row['ticket_id'],
            'creator_name':      row['student_name'],
            'folder_name':       row['folder_name'],
            'editor_name':       row['editor'],
            'editor_discord_id': uid,
            'video_count':       row['video_count'],
        })
        if not ok:
            print(f"  SKIP (dashboard post failed): {row['folder_name']} -> {row['editor']}")
            continue
        remove_pending_entry(row['msg_id'])
        tidy_ops_assign_message(config, row)
        print(f"  OK: {row['folder_name']} -> {row['editor']}")
        time.sleep(1.0)

    print(f'\nPosted {len(PLAN)} website-batch assignments to the dashboard bridge.')


if __name__ == '__main__':
    main()

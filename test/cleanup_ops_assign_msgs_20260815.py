"""One-off: after bulk-assigning the 2026-08-15 backlog
(test/bulk_assign_20260815.py), edit each corresponding ops-assign Discord
message to a green "Assigned" embed with the dropdown removed, then drop the
entry from pending_ops_assigns.json. Follows the required cleanup step in
CLAUDE.md "Known Gotchas" (bulk-assign ops-assign message cleanup).

12s gaps between edits to avoid Discord rate limits on messages >1h old.
Requires a User-Agent header or Cloudflare 403s the request before Discord
ever sees it (see CLAUDE.md gotcha).
"""
import json
import os
import time

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')

MSG_IDS = [
    '1537341707409031320', '1537362320630484994', '1537365287450579026',
    '1537628546392916052', '1537646050691776713', '1537673856716767244',
    '1537777958314319893', '1537779009981784264', '1537780943069253785',
    '1537886723441299649', '1537901775095336960', '1537902790066577470',
    '1537927467438702734', '1537939561773273088', '1537972736188620900',
    '1537989706556379177', '1537992430845173861', '1537995384146890827',
    '1538003016848777258', '1538003052353560709',
]


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def main():
    config = load_config()
    token = config['discord_bot_token']
    headers = {
        'Authorization': f'Bot {token}',
        'Content-Type': 'application/json',
        'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
    }

    with open(PENDING_FILE) as f:
        pending = json.load(f)

    for i, msg_id in enumerate(MSG_IDS):
        item = pending.get(msg_id)
        if not item:
            print(f'  ! {msg_id}: not in pending_ops_assigns.json, skipping')
            continue
        channel_id = item['channel_id']
        editor = None  # filled in below from the assign plan label
        label = f"{item['client_name']} / {item['folder_name']}"

        embed = {
            'title': '✅ Assigned',
            'description': f'**{label}** has been assigned.',
            'color': 0x2ecc71,
        }
        url = f'https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}'
        resp = requests.patch(url, headers=headers, json={'embeds': [embed], 'components': []}, timeout=15)
        if resp.ok:
            print(f'  OK: {label} ({msg_id})')
            del pending[msg_id]
        else:
            print(f'  ! FAILED {label} ({msg_id}): {resp.status_code} {resp.text[:200]}')

        if i < len(MSG_IDS) - 1:
            time.sleep(12)

    with open(PENDING_FILE, 'w') as f:
        json.dump(pending, f, indent=2)
    print(f'\npending_ops_assigns.json now has {len(pending)} entries remaining.')


if __name__ == '__main__':
    main()

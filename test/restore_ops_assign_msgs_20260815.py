"""One-off: restore the 11 ops-assign messages that got edited to a plain
"Assigned" embed (dropdown stripped) before test/rollback_bulk_assign_20260815.py
undid the underlying assignment. Rebuilds the exact embed + component
structure handle_ops_assign_request()/AssignEditorView produce (same
custom_id format: ops_assign_{folder_id[:60]}, ops_ignore_{folder_id[:60]})
so the live discord-bot process's on_ready view re-registration (keyed by
message_id from pending_ops_assigns.json, which was never touched/deleted)
picks these back up as working dropdowns after a bot restart.
"""
import json
import time

import requests

with open('/home/ubuntu/gdrive_watcher/config.json') as f:
    config = json.load(f)
with open('/home/ubuntu/gdrive_watcher/pending_ops_assigns.json') as f:
    pending = json.load(f)

EDITOR_NAMES = sorted(['AJ', 'Aki', 'Danie', 'Gabo', 'Jewel', 'Jill', 'Josh',
                        'Naomi', 'Steven', 'Storm', 'Zyon'])

MSG_IDS = [
    '1537341707409031320', '1537362320630484994', '1537365287450579026',
    '1537628546392916052', '1537646050691776713', '1537673856716767244',
    '1537777958314319893', '1537780943069253785', '1537901775095336960',
    '1537939561773273088', '1537989706556379177',
]

headers = {
    'Authorization': f'Bot {config["discord_bot_token"]}',
    'Content-Type': 'application/json',
    'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
}

for i, mid in enumerate(MSG_IDS):
    item = pending[mid]
    pnum = item.get('project_number', '')
    title = f'📁 New Folder — Assign Editor  {pnum}' if pnum else '📁 New Folder — Assign Editor'
    embed = {
        'title': title,
        'color': 0xf1c40f,
        'fields': [
            {'name': 'Client', 'value': item['client_name'], 'inline': True},
            {'name': 'Folder', 'value': item['folder_name'], 'inline': True},
            {'name': 'Videos', 'value': str(item['video_count']), 'inline': True},
        ],
    }
    folder_id = item.get('folder_id', 'unknown')[:60]
    components = [
        {
            'type': 1,
            'components': [{
                'type': 3,
                'custom_id': f'ops_assign_{folder_id}',
                'placeholder': 'Select an editor to assign...',
                'min_values': 1,
                'max_values': 1,
                'options': [{'label': name, 'value': name} for name in EDITOR_NAMES],
            }],
        },
        {
            'type': 1,
            'components': [{
                'type': 2,
                'style': 2,
                'label': '🚫 Ignore',
                'custom_id': f'ops_ignore_{folder_id}',
            }],
        },
    ]
    url = f'https://discord.com/api/v10/channels/{item["channel_id"]}/messages/{mid}'
    resp = requests.patch(url, headers=headers, json={'embeds': [embed], 'components': components}, timeout=15)
    if resp.ok:
        print(f'OK restored: {item["client_name"]} / {item["folder_name"]} ({mid})')
    else:
        print(f'FAIL {mid}: {resp.status_code} {resp.text[:200]}')
    if i < len(MSG_IDS) - 1:
        time.sleep(12)

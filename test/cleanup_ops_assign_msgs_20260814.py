"""One-off: after bulk_assign_pending_20260814.py, clean up the ops-assign
Discord messages for the 36 folders just assigned (required step per
CLAUDE.md "Bulk-assign ops-assign message cleanup").

For each matching pending_ops_assigns.json entry: PATCH the Discord message to
a green "Assigned" embed with no components, then drop the entry from
pending_ops_assigns.json. 12s gap between edits to avoid Discord rate limits
on messages >1h old (per CLAUDE.md gotcha). Requires a User-Agent header or
Cloudflare 403s the request before it reaches Discord (also documented).
"""
import json
import os
import time
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
PENDING_FILE = os.path.join(BASE_DIR, 'pending_ops_assigns.json')

# folder_id -> editor, same mapping bulk_assign_pending_20260814.py used
FOLDER_ID_TO_EDITOR = {
    '15Q-0LSbGxQM5y6D91M0S5SQn0Ek8T6G8': 'Jill', '1a4Ta236ZyM_WlwJkjLoGFsB-FfF1PsbL': 'Jill',
    '1dOareGb29M10a_o2jdaBvtVmTH7wM0Oy': 'Danie', '1xN6oLIdYmdlaERL0cWijnLayblki9UK6': 'Danie',
    '1VL-IlSQtNZB6V316yq1pnDmCzgx6QbJP': 'Danie', '1UyQ_XZR4mabnazi9bm8lRNQHYqW2lpQc': 'Danie',
    '1TezkRmbDy3bQ8F7FllSVGvk8hz9C_-wr': 'Danie', '1N5WHeICeNG_TK2PC2CxB0BAs9Q91VFdw': 'Danie',
    '16RI1NPVk4Ggu4BUGearVWqZYNLJQ8pGw': 'Danie',
    '1yr9ig59omMKPuoqfeLwyvNHRGX2TyZLN': 'Storm', '1Kf4NhaQRyD9KEH2tUXw_s_lgOo781kKy': 'Storm',
    '18GZm_wlJAjgxT5SfruBQVfxZmceceFEj': 'Storm', '1yzVTVFp4emz3V3P5JMMKuAwDJYCoR5RA': 'Storm',
    '1NRgaBgbFdKmcQyjJOas33kRtC2Q9dUPu': 'Naomi', '1Pb03j_hcQepcTtS2frqVEE2keG7qaO2v': 'Naomi',
    '1VF71g77D3qo5AQP3LqBbuKy4fk4l3av0': 'Naomi', '1LM0ux9nP47toOfJp6iqXMYhf7OxFEcuU': 'Naomi',
    '1R6m56guFcPhf-1rD2IRNvOT6uk23aEoi': 'Naomi', '1yN9ejiHK1w9wfY5_B_RCrQthws_8d9Vd': 'Naomi',
    '1s-RvXf653i1kuS8BEem_AzbRDBp8lXxH': 'Naomi',
    '1t8A-W8K_GxNVNWkuW65BxAtL3ruVC3cd': 'Aki', '1WgbShuRYZhTZAdBX3cghwrBDr7qZZXF-': 'Aki',
    '1maifVopGxYCB2XAtlWYf7Wr9jJijZcHz': 'Aki',
    '1kunMVT75a4ZSdaj2kCwHgf9V07xaw6hq': 'Josh', '1_L7veN1N4tltwKccKD0_WgtekhtUA0-g': 'Josh',
    '1kvZfvfrJX7ulqXKDcg9bflgzgE_HVkqa': 'Josh', '1kg7sYzELut1vYOxIjt3tyjwrUWXP1Ok2': 'Josh',
    '1gnrAHyAGf1jHPn7tVKOEQZnsMg_2D-iA': 'Josh', '1aqqImeWebZu5p40RyDQBYHdaaK5u22H2': 'Josh',
    '1OM5e1-YehxICenrPIB8DiqESuXIHJKQu': 'Josh',
    '1Kv8OjjUfY04XwShGbO8lgZqNNj39w6_z': 'Jewel', '1oDFrz_WuTS2lbX1S6_n3rwwPFQlbO5fN': 'Jewel',
    '1oBeqk94yZw690i9GFX436XaqUm_wA6KH': 'Jewel',
    '18fMcWmpBa3EYxFU6qN6I90QMUgpOryQc': 'Zyon', '1h62EVmk7WpF0RamV-B5-4p34Z0gNIQXo': 'Zyon',
    '11XYkzbP3ZW6QUvJf2BQg1OebqQJzo4E4': 'Zyon',
}

MATCHED_IDS = [
    "1537244411031986237","1537244932002291833","1537244958388391986","1537244991792095343",
    "1537245456638283879","1537290274290532395","1537292245718536245","1537307384584601600",
    "1537307867755843611","1537309929319170060","1537314280876019733","1537318117095768195",
    "1537323964752199751","1537329542555770981","1537341725448736808","1537341752002879558",
    "1537341783141253183","1537352270205816833","1537360770889818174","1537362289278328893",
    "1537362770742353970","1537487292661501962","1537516335968419947","1537518822544900147",
    "1537520279746773052","1537550451929645056","1537557623052705965","1537568548463972424",
    "1537570082417549507","1537570565403967619","1537570597515694121","1537571086487654451",
    "1537571111212941322","1537613458969862204","1537613489995128873","1537614938607198260",
]


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def patch_message(token, channel_id, msg_id, editor_name, folder_name):
    url = f'https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}'
    body = {
        'embeds': [{
            'title': f'✅ Assigned to {editor_name}',
            'color': 0x2ecc71,
            'fields': [{'name': 'Folder', 'value': folder_name, 'inline': True}],
        }],
        'components': [],
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method='PATCH', headers={
        'Authorization': f'Bot {token}',
        'Content-Type': 'application/json',
        'User-Agent': 'DiscordBot (https://github.com/vexxefx/ccvm, 1.0)',
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, ''
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def main():
    config = load_config()
    token = config['discord_bot_token']

    with open(PENDING_FILE) as f:
        pending = json.load(f)

    ok, failed = 0, 0
    for i, mid in enumerate(MATCHED_IDS):
        item = pending.get(mid)
        if not item:
            print(f'{mid}: not in pending_ops_assigns.json anymore, skip')
            continue
        editor = FOLDER_ID_TO_EDITOR.get(item.get('folder_id'), '(assigned)')
        status, body = patch_message(
            token, item['channel_id'], mid,
            editor, item.get('folder_name', '')
        )
        if status == 200:
            print(f'{mid}: OK ({item.get("folder_name")})')
            del pending[mid]
            ok += 1
        else:
            print(f'{mid}: FAILED {status} {body}')
            failed += 1

        with open(PENDING_FILE, 'w') as f:
            json.dump(pending, f, indent=2)

        if i < len(MATCHED_IDS) - 1:
            time.sleep(12)

    print(f'\nDone. ok={ok} failed={failed}. pending_ops_assigns.json entries remaining: {len(pending)}')


if __name__ == '__main__':
    main()

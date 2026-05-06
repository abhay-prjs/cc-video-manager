"""Reset Delivered This Week to 0 for all editors in Notion Editor Profiles."""

import json
import logging
import os
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
EDITOR_PROFILES_DB = 'a18d5c16-f359-4a2b-a620-6c837aa04232'

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def notion_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Notion-Version': '2022-06-28',
    }


def main():
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    token = config['notion_token']

    resp = requests.post(
        f'https://api.notion.com/v1/databases/{EDITOR_PROFILES_DB}/query',
        headers=notion_headers(token),
        json={},
        timeout=15,
    )
    if not resp.ok:
        logger.error(f'Failed to fetch editors: {resp.text}')
        return

    pages = resp.json().get('results', [])
    for page in pages:
        page_id = page['id']
        name_rt = page['properties'].get('Editor', {}).get('title', [])
        name = name_rt[0].get('plain_text', '') if name_rt else page_id
        requests.patch(
            f'https://api.notion.com/v1/pages/{page_id}',
            headers=notion_headers(token),
            json={'properties': {'Delivered This Week': {'number': 0}}},
            timeout=15,
        )
        logger.info(f'Reset weekly for {name}')


if __name__ == '__main__':
    main()

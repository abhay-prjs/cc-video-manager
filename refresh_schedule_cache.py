"""Refresh schedule_cache.json from Editor Profiles. Run via cron every 2 hours."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_ops

config = json.load(open(os.path.join(os.path.dirname(__file__), 'config.json')))
raw = ai_ops._fetch_raw_schedules(config['notion_token'])
print(f'Schedule cache refreshed: {len(raw)} editors')

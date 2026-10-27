"""The editor's "New batch" card as one line, and the site's repeat:<id> keys
kept in their own small pool (2026-10-26). Real bot functions, no Discord."""
import ast
import json
import logging
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def load(names, ns):
    tree = ast.parse((ROOT / 'discord_bot.py').read_text())
    picked = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert len(picked) == len(names), [n.name for n in picked]
    exec(compile(ast.Module(body=picked, type_ignores=[]), str(ROOT / 'discord_bot.py'), 'exec'), ns)
    return ns


class FakeEmbed:
    def __init__(self, title=None, description=None, colour=None):
        self.title, self.description, self.colour, self.url, self.fields = title, description, colour, None, []

    def add_field(self, **kw):
        self.fields.append(kw)


class NotifyCardTests(unittest.TestCase):
    def setUp(self):
        self.chat = None
        self.ns = load({'_notify_card_embed', 'creator_label'}, {
            'discord': SimpleNamespace(Embed=FakeEmbed),
            'creator_chat_link': lambda item: self.chat,
            '_parse_dashboard_assigned_at': lambda raw: 1761500000.0,
        })
        # eddie's heygen folder, as the site sent it on 2026-10-26
        self.item = {
            'folder_name': 'heygen · talking head ×6', 'client_name': 'heygen', 'formats': 'talking head ×6',
            'student_name': 'Eddie Zhao', 'student_username': 'eddiezhao',
            'ticket_url': 'https://www.tryvelocitycreators.com/dashboard/editor/tickets/t1',
        }

    def card(self, item=None, count=6, backfill=False):
        return self.ns['_notify_card_embed'](self.item if item is None else item, count, backfill)

    def test_one_line_with_the_link_on_the_title(self):
        e = self.card()
        self.assertEqual(e.title, '🆕 New batch')
        self.assertEqual(e.url, 'https://www.tryvelocitycreators.com/dashboard/editor/tickets/t1')
        self.assertEqual(e.description, 'Eddie Zhao (@eddiezhao) · heygen · 6 videos · talking head ×6')
        self.assertEqual(e.fields, [])

    def test_their_chat_and_a_backfill_time_ride_one_second_line(self):
        self.chat = '[#eddie-edits](https://discord.com/channels/1/2)'
        e = self.card(backfill=True, count=1)
        self.assertEqual(e.title, '📋 Already on your desk')
        self.assertEqual(e.description.split('\n'), [
            'Eddie Zhao (@eddiezhao) · heygen · 1 video · talking head ×6',
            'assigned <t:1761500000:R> · their chat: [#eddie-edits](https://discord.com/channels/1/2)',
        ])

    def test_a_drive_folder_name_leads_when_it_says_more(self):
        item = dict(self.item, folder_name='Chris - Cal AI - Batch 3', is_reassign=True)
        e = self.card(item)
        self.assertEqual(e.title, '🆕 Reassigned to you')
        self.assertTrue(e.description.startswith('Chris - Cal AI - Batch 3 · Eddie Zhao'))

    def test_nothing_known_still_reads(self):
        e = self.card({}, count=0)
        self.assertEqual(e.description, 'Untitled batch')
        self.assertIsNone(e.url)


class RepeatKeyPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'threads.json'
        self.ns = load({'_save_dashboard_thread'}, dict(
            os=os, json=json, datetime=datetime, timezone=timezone, logger=logging.getLogger('test'),
            DASHBOARD_THREADS_FILE=str(self.path), DASHBOARD_THREADS_LOCK=threading.Lock()))

    def test_repeat_keys_never_push_a_long_lived_key_out(self):
        data = {f'review-bot:{i}': {'updated_at': f'2020-{i:04}'} for i in range(400)}
        data.update({f'repeat:{i}': {'updated_at': f'2021-{i:04}'} for i in range(300)})
        self.path.write_text(json.dumps(data))
        self.ns['_save_dashboard_thread']('repeat:new', '1', '2')
        saved = json.loads(self.path.read_text())
        self.assertEqual(sum(k.startswith('review-bot:') for k in saved), 400)
        self.assertEqual(sum(k.startswith('repeat:') for k in saved), 200)
        self.assertIn('repeat:new', saved)


if __name__ == '__main__':
    unittest.main()

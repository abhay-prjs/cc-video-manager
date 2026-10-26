"""Regression tests for real bot functions without starting a Discord client."""
import ast
import asyncio
import json
import logging
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
NAMES = {'_load_dashboard_thread', '_save_dashboard_thread', 'clear_dashboard_thread', '_delete_message_quiet'}

class NotFound(Exception):
    pass

class ThreadCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'threads.json'
        self.message = SimpleNamespace(delete=AsyncMock())
        self.channel = SimpleNamespace(fetch_message=AsyncMock(return_value=self.message))
        self.ns = dict(os=os, json=json, datetime=datetime, timezone=timezone,
                       logger=logging.getLogger('test'), DASHBOARD_THREADS_FILE=str(self.path),
                       DASHBOARD_THREADS_LOCK=threading.Lock(), discord=SimpleNamespace(NotFound=NotFound),
                       bot=SimpleNamespace(get_channel=lambda _: self.channel))
        tree = ast.parse((ROOT / 'discord_bot.py').read_text())
        selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in NAMES]
        self.assertEqual(len(selected), len(NAMES))
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(ROOT / 'discord_bot.py'), 'exec'), self.ns)
        self.key = 'auto-edit-notice:one'
        self.ns['_save_dashboard_thread'](self.key, '123', '456')

    def saved(self):
        return json.loads(self.path.read_text())

    def clear(self):
        return asyncio.run(self.ns['clear_dashboard_thread'](self.key))

    def test_more_than_500_unresolved_notices_survive(self):
        data = {f'auto-edit-notice:{i}': {'updated_at': '2020', 'message_id': str(i)} for i in range(1001)}
        data.update({f'other:{i}': {'updated_at': f'{i:04}'} for i in range(500)})
        self.path.write_text(json.dumps(data))
        self.ns['_save_dashboard_thread']('other:new', '123', '789')
        saved = self.saved()
        self.assertEqual(sum(k.startswith('auto-edit-notice:') for k in saved), 1001)
        self.assertEqual(sum(k.startswith('other:') for k in saved), 500)

    def test_success_removes_only_requested_notice(self):
        self.ns['_save_dashboard_thread']('auto-edit-notice:new-version', '123', '999')
        self.assertTrue(self.clear())
        self.channel.fetch_message.assert_awaited_once_with(456)
        self.assertNotIn(self.key, self.saved())
        self.assertIn('auto-edit-notice:new-version', self.saved())

    def test_failed_delete_retains_key_and_retry_succeeds(self):
        self.message.delete.side_effect = RuntimeError('temporary Discord failure')
        with self.assertRaisesRegex(RuntimeError, 'temporary'):
            self.clear()
        self.assertIn(self.key, self.saved())
        self.message.delete.side_effect = None
        self.assertTrue(self.clear())
        self.assertNotIn(self.key, self.saved())

    def test_already_deleted_is_success(self):
        self.channel.fetch_message.side_effect = NotFound()
        self.assertTrue(self.clear())
        self.assertNotIn(self.key, self.saved())

    def test_replaced_key_during_delete_is_preserved(self):
        async def replace():
            self.ns['_save_dashboard_thread'](self.key, '123', '999')
        self.message.delete.side_effect = replace
        self.assertTrue(self.clear())
        self.assertEqual(self.saved()[self.key]['message_id'], '999')

    def test_unrelated_quiet_delete_keeps_existing_behavior(self):
        self.message.delete.side_effect = RuntimeError('failure')
        asyncio.run(self.ns['_delete_message_quiet']('123', '456'))

if __name__ == '__main__':
    unittest.main()

import json
import logging
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from editor_dm_relay import EditorDMRelay


async def items(values):
    for value in values:
        yield value


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.route = dict(name='Editor', user_id='1', dm_channel_id='2', channel_id='3', after_message_id='100')
        self.config = os.path.join(self.temp.name, 'routes.json')
        self.state = os.path.join(self.temp.name, 'state.json')
        with open(self.config, 'w') as f:
            json.dump(dict(enabled=True, guild_id='4', category_id='5', routes=[self.route]), f)
        self.member = NS(id=1)
        self.everyone = object()
        self.guild = NS(id=4, default_role=self.everyone, get_member=lambda _: self.member)
        self.source = NS(id=2, type=discord.ChannelType.private, recipient=self.member,
                         history=lambda **kw: items([]))
        self.copies = []
        async def send(**kwargs):
            msg = NS(id=1000+len(self.copies), author=NS(id=9), embeds=[kwargs['embed']],
                     edit=AsyncMock(), delete=AsyncMock())
            self.copies.append(msg)
            return msg
        self.target = NS(id=3, type=discord.ChannelType.text, guild=self.guild, category_id=5,
                         permissions_for=lambda obj: NS(view_channel=obj is self.member),
                         history=lambda **kw: items(self.copies), send=AsyncMock(side_effect=send),
                         get_partial_message=lambda mid: next(m for m in self.copies if m.id == mid))
        self.bot = NS(user=NS(id=9), get_channel=lambda cid: {2:self.source,3:self.target}[cid])
        self.relay = self.new_relay()

    def new_relay(self):
        return EditorDMRelay(self.bot, self.config, self.state, logging.getLogger('relay-test'))

    def message(self, mid=101, content='Ready'):
        return NS(id=mid, content=content, guild=None, author=NS(id=1, bot=False),
                  channel=self.source, attachments=[], stickers=[], reference=None,
                  created_at=datetime.now(timezone.utc))

    async def test_only_authorized_source_after_disclosure(self):
        for change in [dict(id=100), dict(guild=self.guild), dict(author=NS(id=7,bot=False)),
                       dict(author=NS(id=1,bot=True)), dict(channel=NS(id=8))]:
            msg=self.message()
            msg.__dict__.update(change)
            await self.relay.receive(msg)
        self.target.send.assert_not_called()

    async def test_wrong_destination_or_permissions_rejected(self):
        for field, value in [('category_id',7), ('guild',NS(id=7)), ('type',discord.ChannelType.private)]:
            original=getattr(self.target,field)
            setattr(self.target,field,value)
            with self.assertRaises(ValueError):
                await self.relay.receive(self.message())
            setattr(self.target,field,original)
        self.target.permissions_for=lambda _: NS(view_channel=True)
        with self.assertRaises(ValueError):
            await self.relay.receive(self.message())
        self.target.send.assert_not_called()

    async def test_send_restart_dedupe_and_edit(self):
        msg=self.message()
        await self.relay.receive(msg)
        self.relay=self.new_relay()
        await self.relay.receive(msg)
        self.assertEqual(self.target.send.await_count,1)
        msg.content='Blocked: upload failed'
        await self.relay.receive(msg)
        self.copies[0].edit.assert_awaited_once()
        self.assertEqual(self.copies[0].edit.call_args.kwargs['embed'].description,msg.content)

    async def test_crash_recovery_by_footer(self):
        await self.relay.receive(self.message())
        self.relay.state['messages']={}
        self.relay.save()
        await self.relay.receive(self.message())
        self.assertEqual(self.target.send.await_count,1)

    async def test_long_message_attachments_and_shrinking_edit(self):
        msg=self.message(content='a'*5000)
        msg.attachments=[NS(filename='cut.mp4', url='https://cdn.discordapp.com/cut.mp4')]
        await self.relay.receive(msg)
        self.assertEqual(len(self.copies),2)
        self.assertIn('cut.mp4',self.copies[-1].embeds[0].description)
        msg.content='Short'; msg.attachments=[]
        await self.relay.receive(msg)
        self.copies[1].delete.assert_awaited_once()

    async def test_failed_send_retried_without_advancing_cursor(self):
        msg=self.message()
        self.source.history=lambda **kw: items([msg])
        sender=self.target.send.side_effect
        self.target.send.side_effect=RuntimeError('temporary failure')
        await self.relay.sweep()
        self.assertNotIn('1',self.relay.state['cursors'])
        self.target.send.side_effect=sender
        await self.relay.sweep()
        self.assertEqual(self.relay.state['cursors']['1'],'101')
        self.assertEqual(len(self.copies),1)

    async def test_atomic_state_preserves_volume_symlink(self):
        volume=os.path.join(self.temp.name,'volume-state.json')
        os.symlink(volume,self.state)
        await self.relay.receive(self.message())
        self.assertTrue(os.path.islink(self.state))
        with open(volume) as f:
            self.assertIn('101',json.load(f)['messages'])

if __name__ == '__main__':
    unittest.main()

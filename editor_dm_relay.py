"""Forward opted-in editor bot-DM replies into verified private editor rooms.

Only explicit routes after their disclosure message are read. A persisted cursor
catches replies during restarts; receipts keep retries from duplicating replies.
The gateway handles new messages/edits promptly, with a 30s catch-up poll.
"""
import asyncio
import hashlib
import json
import os

import discord


class EditorDMRelay:
    def __init__(self, bot, routes_path, state_path, logger):
        self.bot = bot
        self.logger = logger
        self.state_path = state_path
        with open(routes_path) as f:
            self.config = json.load(f)
        self.routes = self.config.get('routes', []) if self.config.get('enabled') else []
        if len({r['user_id'] for r in self.routes}) != len(self.routes):
            raise ValueError('Duplicate editor DM relay route')
        if os.path.exists(state_path):
            with open(state_path) as f:
                self.state = json.load(f)
        else:
            self.state = {'cursors': {}, 'messages': {}}
        self.lock = asyncio.Lock()
        self.task = None
        self.validated_routes = set()

    def save(self):
        # Resolve the boot shim's volume symlink before atomically replacing it.
        target = os.path.realpath(self.state_path)
        temp = target + '.tmp'
        with open(temp, 'w') as f:
            json.dump(self.state, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, target)

    def start(self):
        if self.routes and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run())
            self.logger.info('editor_dm_relay started: %s explicit routes', len(self.routes))

    def route_for(self, message):
        if message.guild is not None or message.author.bot:
            return None
        return next((r for r in self.routes
                     if str(message.author.id) == r['user_id']
                     and str(message.channel.id) == r['dm_channel_id']
                     and message.id > int(r['after_message_id'])), None)

    async def channels(self, route):
        source_id, target_id = int(route['dm_channel_id']), int(route['channel_id'])
        source = self.bot.get_channel(source_id) or await self.bot.fetch_channel(source_id)
        target = self.bot.get_channel(target_id) or await self.bot.fetch_channel(target_id)
        if (getattr(source, 'type', None) != discord.ChannelType.private
                or str(getattr(source.recipient, 'id', '')) != route['user_id']):
            raise ValueError('Source is not the intended editor DM')
        if (getattr(target, 'type', None) != discord.ChannelType.text
                or str(target.guild.id) != self.config['guild_id']
                or str(target.category_id) != self.config['category_id']):
            raise ValueError('Destination is not the configured editor 1-to-1')
        member = target.guild.get_member(int(route['user_id']))
        if member is None:
            member = await target.guild.fetch_member(int(route['user_id']))
        if (not target.permissions_for(member).view_channel
                or target.permissions_for(target.guild.default_role).view_channel):
            raise ValueError('Editor room permissions do not match the private route')
        if route['user_id'] not in self.validated_routes:
            self.logger.info('editor_dm_relay route ready: %s -> %s', route['name'], target.id)
            self.validated_routes.add(route['user_id'])
        return source, target

    @staticmethod
    def pages(message):
        text = message.content or ''
        if message.attachments:
            text += '\n\nAttachments:\n' + '\n'.join(
                f'[{discord.utils.escape_markdown(a.filename)}]({a.url})'
                for a in message.attachments)
        if message.stickers:
            text += '\n\nStickers:\n' + '\n'.join(str(s.url) for s in message.stickers)
        if message.reference and message.reference.message_id:
            text += f'\n\nReply to DM message {message.reference.message_id}.'
        text = text.strip() or '(Message without text; open the attachment in Discord.)'
        return [text[i:i + 3800] for i in range(0, len(text), 3800)]

    async def mirror(self, message, route, target):
        if self.route_for(message) != route:
            return
        key = str(message.id)
        pages = self.pages(message)
        fingerprint = hashlib.sha256(json.dumps(pages).encode()).hexdigest()
        rec = self.state['messages'].setdefault(key, {
            'channel_id': route['channel_id'], 'copies': [], 'fingerprint': None})
        if rec['channel_id'] != route['channel_id']:
            raise ValueError('A receipt cannot be reused in a different room')
        if rec['fingerprint'] == fingerprint:
            return
        self.save()
        # If a send succeeded just before process death, recover it by its
        # stable footer rather than posting the same reply again.
        recent = None
        for i, page in enumerate(pages):
            marker = f'editor-dm:{message.id}:part:{i}'
            embed = discord.Embed(title=f'{route["name"]} replied in DM',
                                  description=page, color=0xED4245,
                                  timestamp=message.created_at)
            embed.set_footer(text=f'DM reply shared with this 1-to-1 • {marker}')
            if i < len(rec['copies']):
                try:
                    await target.get_partial_message(int(rec['copies'][i])).edit(
                        embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    continue
                except discord.NotFound:
                    rec['copies'] = rec['copies'][:i]
            if recent is None:
                recent = [m async for m in target.history(limit=100)]
            recovered = next((m for m in recent if m.author.id == self.bot.user.id
                              and any(marker == str(e.footer.text or '').split(' • ')[-1]
                                      for e in m.embeds)), None)
            if recovered:
                await recovered.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                copy = recovered
            else:
                copy = await target.send(embed=embed,
                                         allowed_mentions=discord.AllowedMentions.none())
            rec['copies'].append(str(copy.id))
            self.save()
        for copy_id in rec['copies'][len(pages):]:
            try:
                await target.get_partial_message(int(copy_id)).delete()
            except discord.NotFound:
                pass
        rec['copies'] = rec['copies'][:len(pages)]
        rec['fingerprint'] = fingerprint
        self.save()
        self.logger.info('editor_dm_relay mirrored source=%s editor=%s target=%s copies=%s',
                         message.id, route['name'], route['channel_id'], len(rec['copies']))

    async def receive(self, message):
        route = self.route_for(message)
        if route:
            async with self.lock:
                _, target = await self.channels(route)
                await self.mirror(message, route, target)
                # Only an ordered history pass may advance the cursor: gateway
                # callbacks can arrive out of order.

    async def receive_edit(self, payload):
        route = next((r for r in self.routes if str(payload.channel_id) == r['dm_channel_id']
                      and payload.message_id > int(r['after_message_id'])), None)
        if route:
            async with self.lock:
                source, target = await self.channels(route)
                message = await source.fetch_message(payload.message_id)
                await self.mirror(message, route, target)

    async def sweep(self):
        for route in self.routes:
            try:
                async with self.lock:
                    source, target = await self.channels(route)
                    uid = route['user_id']
                    cursor = max(int(route['after_message_id']),
                                 int(self.state['cursors'].get(uid, 0)))
                    async for message in source.history(limit=200, after=discord.Object(id=cursor),
                                                        oldest_first=True):
                        await self.mirror(message, route, target)
                        self.state['cursors'][uid] = str(message.id)
                        self.save()
                    # Catch edits missed while disconnected without reposting.
                    async for message in source.history(limit=30):
                        if str(message.id) in self.state['messages']:
                            await self.mirror(message, route, target)
            except Exception:
                self.logger.exception('editor_dm_relay route failed for %s; retrying next pass',
                                      route['name'])

    async def run(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await self.sweep()
            await asyncio.sleep(max(15, int(self.config.get('poll_seconds', 30))))

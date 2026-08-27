"""
One-shot purge of the bot's own assignment-notification messages.

Deliberately a standalone script, not a change to discord_bot.py: the bot is a
systemd service, and nothing here needs it restarted or redeployed. Run it,
read the preview, run it again with --yes.

What it deletes
---------------
Only messages that are ALL of:
  * authored by this bot (never a human's, never another app's),
  * matching one of NOISE_PATTERNS below (checked against the message content
    AND every embed title/description) — the assign card, the "your batch was
    assigned" pings, and drive folder-detection posts,
  * older than --min-age-hours, so a purge running mid-shift cannot delete
    something that just landed,
  * carrying no dropdown or button, UNLESS --include-cards is passed.

The site stopped posting all of these on 2026-08-27, so nothing this deletes
will come back.

What it never touches
---------------------
  * anything from anyone but the bot,
  * anything pinned,
  * anything newer than the age floor,
  * anything in a channel you did not name,
  * the assign cards, unless you pass --include-cards.

Usage
-----
  # preview — prints what it WOULD delete, deletes nothing
  python purge_assign_noise.py --channel 123456789 --days 30

  # same again, actually deleting
  python purge_assign_noise.py --channel 123456789 --days 30 --yes

  # everything, dropdown cards included
  python purge_assign_noise.py --channel 123456789 --days 60 --include-cards --yes

  # several channels at once
  python purge_assign_noise.py --channel 123 --channel 456 --days 30 --yes

Notes
-----
Messages older than 14 days cannot be bulk-deleted by Discord, so every delete
is individual and paced. Expect roughly one per 1.2s; a thousand messages is
about twenty minutes. It is resumable — run it again and it picks up whatever
is left.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

# Substrings that mark a message as assignment noise. Matched case-insensitively
# against the content and against every embed title and description.
#
# Keep these tight. Anything matching here is deleted, so a pattern like
# "assign" on its own would also eat the card headers.
NOISE_PATTERNS: list[str] = [
    # the assign card itself — header and dropdown placeholder
    r"new website batch",
    r"assign an? editor",
    r"select an editor",
    # "Your batch was assigned" / reassignment pings
    r"your batch was assigned",
    r"assigned to you",
    r"you.{0,3}ve been assigned",
    r"reassigned to",
    r"batch (?:was )?assigned",
    r"new assignment",
    r"picked up (?:your|the) batch",
    # drive-side folder detection posts
    r"new (?:drive )?folder",
    r"folder detected",
    r"new folder assigned",
]

# Never delete a message matching one of these, even if a noise pattern hits.
#
# Empty as of 2026-08-27: the assign card is now noise too ("i want them
# removed all of em"), so the only thing still protecting a message is the
# components check below — and that no longer applies, since the cards this
# purge is aimed at are exactly the ones carrying a dropdown. See
# --include-cards.
KEEP_PATTERNS: list[str] = []

NOISE_RE = re.compile("|".join(NOISE_PATTERNS), re.IGNORECASE)
KEEP_RE = re.compile("|".join(KEEP_PATTERNS), re.IGNORECASE) if KEEP_PATTERNS else None

# Discord will rate-limit a tight delete loop. One delete per this many seconds.
DELETE_INTERVAL_SECONDS = 1.2


def load_token() -> str:
    """Token from config.json, falling back to the env var the service uses."""
    for key in ("bot_token", "discord_token", "token"):
        cfg = Path(__file__).with_name("config.json")
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict) and data.get(key):
                return str(data[key])
    for env in ("DISCORD_BOT_TOKEN", "DISCORD_TOKEN", "BOT_TOKEN"):
        if os.environ.get(env):
            return os.environ[env]
    sys.exit(
        "no bot token found. put it in config.json as bot_token, "
        "or export DISCORD_BOT_TOKEN."
    )


def message_text(message: discord.Message) -> str:
    """Everything a pattern should be able to see: content plus every embed."""
    parts: list[str] = [message.content or ""]
    for embed in message.embeds:
        parts.append(embed.title or "")
        parts.append(embed.description or "")
        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")
        if embed.footer:
            parts.append(embed.footer.text or "")
        if embed.author:
            parts.append(embed.author.name or "")
    return "\n".join(p for p in parts if p)


def is_noise(
    message: discord.Message, me: discord.ClientUser, include_cards: bool
) -> bool:
    if message.author.id != me.id:
        return False
    # A message with a dropdown or button is an assign card. Those used to be
    # the one thing this script would never touch; --include-cards says to
    # take them too, which is what "all of em" means now that the site no
    # longer posts them (PR #1778). Without the flag they are still spared.
    if message.components and not include_cards:
        return False
    if message.pinned:
        return False
    text = message_text(message)
    if not text:
        return False
    if KEEP_RE is not None and KEEP_RE.search(text):
        return False
    return bool(NOISE_RE.search(text))


async def purge_channel(
    channel: discord.TextChannel,
    me: discord.ClientUser,
    after: datetime,
    before: datetime,
    limit: int | None,
    commit: bool,
    include_cards: bool,
) -> tuple[int, int]:
    scanned = 0
    hits: list[discord.Message] = []

    print(f"\n#{channel.name} ({channel.id})")
    try:
        async for message in channel.history(
            limit=limit, after=after, before=before, oldest_first=False
        ):
            scanned += 1
            if is_noise(message, me, include_cards):
                hits.append(message)
    except discord.Forbidden:
        print("  ! no permission to read this channel's history — skipped")
        return (0, 0)

    print(f"  scanned {scanned}, matched {len(hits)}")
    for m in hits[:10]:
        preview = message_text(m).replace("\n", " / ")[:110]
        print(f"    {m.created_at:%Y-%m-%d %H:%M}  {preview}")
    if len(hits) > 10:
        print(f"    … and {len(hits) - 10} more")

    if not commit:
        return (len(hits), 0)

    deleted = 0
    for m in hits:
        try:
            await m.delete()
            deleted += 1
        except discord.NotFound:
            pass  # already gone; a rerun of an interrupted purge
        except discord.Forbidden:
            print("  ! missing Manage Messages here — stopping this channel")
            break
        except discord.HTTPException as exc:
            print(f"  ! {exc} — pausing 5s")
            await asyncio.sleep(5)
            continue
        if deleted % 25 == 0:
            print(f"  deleted {deleted}/{len(hits)}")
        await asyncio.sleep(DELETE_INTERVAL_SECONDS)
    print(f"  deleted {deleted}")
    return (len(hits), deleted)


async def run(args: argparse.Namespace) -> None:
    intents = discord.Intents.default()
    intents.message_content = True  # needed to read content on non-embed posts
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        assert client.user is not None
        now = datetime.now(timezone.utc)
        after = now - timedelta(days=args.days)
        before = now - timedelta(hours=args.min_age_hours)
        print(
            f"signed in as {client.user} — window "
            f"{after:%Y-%m-%d %H:%M} → {before:%Y-%m-%d %H:%M} UTC"
        )
        if not args.yes:
            print("DRY RUN — nothing will be deleted. add --yes to commit.")

        total_hits = total_deleted = 0
        for channel_id in args.channel:
            channel = client.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await client.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    print(f"\n{channel_id}: not visible to this bot — skipped")
                    continue
            if not isinstance(channel, discord.TextChannel):
                print(f"\n{channel_id}: not a text channel — skipped")
                continue
            hits, deleted = await purge_channel(
                channel,
                client.user,
                after,
                before,
                args.limit,
                args.yes,
                args.include_cards,
            )
            total_hits += hits
            total_deleted += deleted

        print(
            f"\n{'deleted' if args.yes else 'would delete'} "
            f"{total_deleted if args.yes else total_hits} messages"
        )
        await client.close()

    await client.start(load_token())


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--channel",
        type=int,
        action="append",
        required=True,
        help="channel id to purge; repeat for more than one",
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="how far back to look (default 30)",
    )
    p.add_argument(
        "--min-age-hours",
        type=int,
        default=6,
        help="never touch anything newer than this (default 6)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after scanning this many messages per channel",
    )
    p.add_argument(
        "--include-cards",
        action="store_true",
        help=(
            "also delete messages carrying a dropdown or button (the assign "
            "cards). without this they are spared."
        ),
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="actually delete. without it this is a dry run.",
    )
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

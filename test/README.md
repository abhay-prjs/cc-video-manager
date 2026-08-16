Throwaway scripts. One-off backfills, migrations, data repairs and manual
diagnostics: written to answer one question or fix one incident, run once or
twice, never imported by a service.

The 45 scripts already committed here STAY committed. They were briefly
untracked on 2026-08-16 and that was a mistake — `git rm --cached` doesn't
"untrack" in any shared sense, it records a DELETION that every other clone
applies on pull. It wiped all 45 off this machine and would have done the
same to the bot box, including the recovery scripts
(restore_ops_assign_msgs, rollback_bulk_assign). Reverted.

New files here are gitignored (`test/*`), which is safe: ignore rules only
decide whether git starts tracking something NEW, so the existing 45 are
unaffected. Write your one-offs here and they simply won't be committed.

Conventions if you write one:
  - resolve BASE_DIR as the PARENT directory, so config.json / token.json and
    the state files still resolve from one level down:
      os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  - reimplement the few lines of posting/parking logic you need rather than
    importing discord_bot.py — that module is ~7000 lines with a live
    discord.Client and command-tree decorators that execute at import time.

If a script turns out to be worth keeping and re-running, move it to the repo
root next to the services, with a comment saying what it's for.

Throwaway scripts. Gitignored — nothing in here reaches GitHub.

One-off backfills, migrations, data repairs and manual diagnostics: written
to answer one question or fix one incident, run once or twice, never imported
by a service. 45 of them were tracked before 2026-08-16; that buried the real
codebase in files nobody would read again.

The files still exist on whatever machine wrote them, and stay runnable. They
just stop being pushed.

Conventions if you write one here:
  - resolve BASE_DIR as the PARENT directory, so config.json / token.json and
    the state files still resolve from one level down:
      os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  - reimplement the few lines of posting/parking logic you need rather than
    importing discord_bot.py — that module is ~7000 lines with a live
    discord.Client and command-tree decorators that execute at import time.

If a script turns out to be worth keeping and re-running, it has earned a
real home in the repo root next to the services, with a comment saying what
it's for and what runs it. That should be rare and deliberate.

Only this README is tracked, so the folder exists on a fresh clone.

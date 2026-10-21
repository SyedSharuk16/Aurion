#!/usr/bin/env python3
"""Container healthcheck. Exits non-zero when the feed has gone stale in an open session.

Deliberately narrow. It checks the one condition that means the process is running but
useless -- no new M1 bar during a session that should be producing them -- and ignores
everything that a restart would not fix. A healthcheck that fails on a transient API error
produces a restart loop, which is worse than the error.

It reads the database directly rather than calling into the app, so it cannot be blocked by
the very event loop it is checking.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MAX_STALENESS_S = float(os.environ.get("AURION_HEALTH_MAX_STALENESS_S", "300"))


def market_open(now: datetime) -> bool:
    """FX week: Sunday 17:00 NY to Friday 17:00 NY."""
    local = now.astimezone(NY)
    weekday = local.weekday()
    if weekday == 5:
        return False
    if weekday == 6:
        return local.hour >= 17
    if weekday == 4:
        return local.hour < 17
    return True


def db_path() -> Path:
    url = os.environ.get("AURION_DB_URL", "sqlite+aiosqlite:////app/data/aurion.sqlite")
    return Path(url.split("///")[-1]) if "///" in url else Path("/app/data/aurion.sqlite")


def main() -> int:
    path = db_path()
    if not path.exists():
        # Still starting up. start-period covers the first boot; after that an absent
        # database is a genuine failure, but not one a restart distinguishes.
        print(f"database not present yet at {path}")
        return 0

    now = datetime.now(tz=UTC)
    if not market_open(now):
        print("market closed; staleness not checked")
        return 0

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0) as conn:
            row = conn.execute("SELECT MAX(close_time) FROM candles_m1").fetchone()
    except sqlite3.Error as exc:
        print(f"database unreadable: {exc}")
        return 1

    if not row or row[0] is None:
        print("no candles stored yet")
        return 0

    raw = str(row[0])
    try:
        newest = datetime.fromisoformat(raw)
    except ValueError:
        print(f"unparseable close_time {raw!r}")
        return 1
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)

    age = (now - newest).total_seconds()
    if age > MAX_STALENESS_S:
        print(f"feed stale: newest bar is {age:.0f}s old (limit {MAX_STALENESS_S:.0f}s)")
        return 1
    print(f"ok: newest bar {age:.0f}s old")
    return 0


if __name__ == "__main__":
    sys.exit(main())

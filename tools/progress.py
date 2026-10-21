#!/usr/bin/env python3
"""How much history is stored, and what the engine can therefore do with it.

    python tools/progress.py

Answers the question a silent Telegram channel cannot: is it still backfilling, or is it
finished and simply not finding setups? Those look identical from the outside and have
completely different remedies.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.exc import OperationalError

from aurion.config.models import load_config
from aurion.core.timeframe import TimeFrame
from aurion.data.store import Store
from aurion.runtime.env import load_dotenv

#: W1 structure needs two confirmed swings per side, and a weekly fractal is not confirmed
#: until `swing_half_width` weeks have closed on both sides of it. Below roughly this much
#: history the bias gate cannot return a direction and every evaluation stops at step 1.
W1_READY_DAYS = 170


async def main() -> int:
    load_dotenv(os.environ.get("AURION_ENV_FILE", ".env"))
    cfg = load_config(os.environ.get("AURION_CONFIG_PATH", "config.yaml"))
    store = Store(os.environ.get("AURION_DB_URL", "sqlite+aiosqlite:///./data/aurion.sqlite"))
    target = cfg.data.m1_retention_days

    try:
        await store.ping()
    except OperationalError:
        # The state every new deployment starts in, so it gets a sentence rather than a
        # stack trace about a missing table.
        print("No database yet. The app has not completed a start, so nothing is stored.")
        print(f"  expected at: {os.environ.get('AURION_DB_URL', './data/aurion.sqlite')}")
        await store.dispose()
        return 1

    print(f"{'instrument':<12} {'M1 bars':>10} {'days':>7} {'of target':>10}  state")
    print("-" * 64)
    try:
        for instrument in sorted(cfg.enabled_instruments):
            candles = await store.load_m1(instrument)
            if not candles:
                print(f"{instrument:<12} {0:>10} {0:>7} {'0%':>10}  nothing stored yet")
                continue
            span = candles[-1].close_time - candles[0].open_time
            days = span / timedelta(days=1)
            bars = len(await store.load_bars(instrument, TimeFrame.W1))
            state = (
                f"W1 bars: {bars} -- enough for a bias"
                if days >= W1_READY_DAYS
                else f"W1 bars: {bars} -- too little history, bias gate will reject"
            )
            print(
                f"{instrument:<12} {len(candles):>10,} {days:>7.1f} "
                f"{100 * days / target:>9.1f}%  {state}"
            )
    finally:
        await store.dispose()

    print()
    print(f"Target is data.m1_retention_days = {target} days.")
    print(f"Below ~{W1_READY_DAYS} days the cascade stops at bias:htf_structure_unavailable")
    print("on every evaluation, which is correct behaviour and not a fault.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

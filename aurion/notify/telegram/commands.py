"""Command handlers. `docs/03-signal-engine.md` §7.4.

Commands are accepted only from the configured allow-list; anything else is logged and
dropped. ``/pause`` state is **persisted**, so a restart does not silently resume a paused
bot -- which would be the worst possible failure of a pause button.

Every `/stats` response carries the mandatory disclaimer, enforced here rather than left to
a template.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aurion import BACKTEST_DISCLAIMER
from aurion.analysis.base import Direction
from aurion.analysis.registry import AnalysisSnapshot
from aurion.backtest.metrics import breakout, trade_stats
from aurion.notify.telegram.formatters import escape, price
from aurion.sessions.windows import WindowSet
from aurion.signals.lifecycle import TrackedSignal

NY = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")

PAUSE_KEY = "notify.paused"

# MarkdownV2 fragments that need literal backslashes. Held as constants because Python 3.11
# forbids a backslash inside an f-string expression, and inlining them is a syntax error.
_NOT_PIT = " \\(not point\\-in\\-time\\)"
_PAPER_TAG = " `\\[PAPER\\]`"
_EQ = "\\="
_SR_HEADER = "*S&R \\(strength is a model output, not a measurement\\)*"


@dataclass(frozen=True, slots=True)
class HealthView:
    adapter: str
    connected: bool
    last_bar_age_s: dict[str, float | None]
    model_artifact_id: str
    config_hash: str
    feature_schema_version: int
    uptime_s: float
    paused: bool
    calendar_available: bool
    calendar_point_in_time: bool


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A resolved trade as `/stats` reads it.

    A named type rather than a positional tuple: indexing a heterogeneous tuple to build
    breakout cells loses the element types and reads as a puzzle at the call site.
    """

    session: str
    instrument: str
    weekday: str
    realised_r: float

    def dimension(self, name: str) -> str:
        return {"session": self.session, "instrument": self.instrument,
                "weekday": self.weekday}[name]


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Everything the handlers read. No handler reaches into the runtime directly."""

    health: HealthView
    windows: WindowSet
    instruments: Sequence[str]
    points: dict[str, float]
    active_signals: Sequence[TrackedSignal]
    closed_returns: Sequence[ClosedTrade]
    snapshots: dict[str, AnalysisSnapshot]
    now: datetime
    is_paper: bool = True


def status(ctx: CommandContext) -> str:
    h = ctx.health
    lines = [
        "\u2699\ufe0f *Status*",
        "",
        f"adapter  `{escape(h.adapter)}` \u2014 "
        f"{'connected' if h.connected else '*DISCONNECTED*'}",
        f"paused   `{h.paused}`",
        f"uptime   `{escape(f'{h.uptime_s / 3600:.1f}')}` h",
        f"model    `{escape(h.model_artifact_id or 'none')}`",
        f"config   `{escape(h.config_hash)}` \u00b7 features v{h.feature_schema_version}",
        f"calendar `{'available' if h.calendar_available else 'UNAVAILABLE'}`"
        + ("" if h.calendar_point_in_time else _NOT_PIT),
        "",
        "*Bar age*",
    ]
    for symbol in sorted(h.last_bar_age_s):
        age = h.last_bar_age_s[symbol]
        text = "no data" if age is None else f"{age:.0f}s"
        lines.append(f" \u2022 `{escape(symbol)}` {escape(text)}")
    active = {w.name for w in ctx.windows.active(ctx.now)}
    lines += ["", f"active windows  {escape(', '.join(sorted(active)) or 'none')}"]
    if not h.calendar_available:
        lines += [
            "",
            escape(
                "Calendar unavailable: under fail_safe=block every signal is suppressed. "
                "This is deliberate, and loud, because a silent dead bot is worse."
            ),
        ]
    return "\n".join(lines)


def active(ctx: CommandContext, current_price: Callable[[str], float | None]) -> str:
    if not ctx.active_signals:
        return "\U0001f4ed *Active* \u2014 none"
    lines = ["\U0001f4cb *Active signals*", ""]
    for signal in ctx.active_signals:
        point = ctx.points.get(signal.instrument, 0.01)
        side = "LONG" if signal.direction is Direction.UP else "SHORT"
        now_price = current_price(signal.instrument)
        unrealised = (
            f"{signal.r_of(now_price):+.2f}" if now_price is not None else "n/a"
        )
        lines += [
            f"*{escape(signal.instrument)} {side}* \u2014 `{escape(str(signal.state))}`",
            f"  entry `{price(signal.entry, point)}` \u00b7 "
            f"SL `{price(signal.effective_stop, point)}`",
            f"  realised `{escape(f'{signal.realised_r:+.2f}')}`R \u00b7 "
            f"unrealised `{escape(unrealised)}`R",
            "",
        ]
    return "\n".join(lines)


def stats(ctx: CommandContext) -> str:
    """Overall and by session, instrument and weekday, with thin cells shown as counts."""
    if not ctx.closed_returns:
        return f"\U0001f4ca *Stats* \u2014 no closed trades\n\n_{escape(BACKTEST_DISCLAIMER)}_"
    values = [t.realised_r for t in ctx.closed_returns]
    st = trade_stats(values)
    tag = _PAPER_TAG if ctx.is_paper else ""
    lines = [
        f"\U0001f4ca *Stats*{tag}",
        "",
        f"trades      `{st.n}`",
        f"win rate    `{escape(f'{st.win_rate:.1%}')}`",
        f"expectancy  `{escape(f'{st.expectancy:+.3f}')}`R",
        f"avg win     `{escape(f'{st.avg_win:+.2f}')}`R \u00b7 "
        f"avg loss `{escape(f'{st.avg_loss:+.2f}')}`R",
        f"profit factor `{escape(f'{st.profit_factor:.2f}')}`",
        f"max DD      `{escape(f'{st.max_drawdown_r:.1f}')}`R",
        f"total       `{escape(f'{st.total_r:+.1f}')}`R",
    ]
    for label in ("session", "instrument", "weekday"):
        cells = breakout(
            [(t.dimension(label), t.realised_r) for t in ctx.closed_returns]
        )
        lines += ["", f"*By {label}*"]
        for cell in cells:
            if cell.stats is None:
                lines.append(
                    f" \u2022 `{escape(cell.key)}` n{_EQ}{cell.n} \u2014 {escape(cell.note)}"
                )
            else:
                lines.append(
                    f" \u2022 `{escape(cell.key)}` n{_EQ}{cell.n} "
                    f"E{_EQ}{escape(f'{cell.stats.expectancy:+.3f}')}R"
                )
    lines += ["", f"_{escape(BACKTEST_DISCLAIMER)}_"]
    return "\n".join(lines)


def sessions(ctx: CommandContext) -> str:
    lines = ["\U0001f570\ufe0f *Sessions today*", ""]
    local = ctx.now.astimezone(NY)
    for name in ctx.windows.names:
        window = ctx.windows[name]
        start, end = window.occurrence(local.date())
        marker = "\u25b6" if window.contains(ctx.now) else " "
        emits = "signals" if window.emits_signals else "marking only"
        lines.append(
            f"{marker} `{escape(name)}`  "
            f"{escape(start.astimezone(NY).strftime('%H:%M'))}\u2013"
            f"{escape(end.astimezone(NY).strftime('%H:%M'))} NY  "
            f"\\({escape(start.astimezone(SGT).strftime('%H:%M'))}\u2013"
            f"{escape(end.astimezone(SGT).strftime('%H:%M'))} SGT\\) \u00b7 {escape(emits)}"
        )
    lines += ["", "*Whitelists*"]
    for symbol in sorted(ctx.instruments):
        allowed = [w.name for w in ctx.windows.active_for(ctx.now, symbol)]
        lines.append(f" \u2022 `{escape(symbol)}` {escape(', '.join(allowed) or 'none active')}")
    return "\n".join(lines)


def levels(ctx: CommandContext, symbol: str) -> str:
    snapshot = ctx.snapshots.get(symbol)
    if snapshot is None:
        return f"no snapshot for `{escape(symbol)}`"
    point = ctx.points.get(symbol, 0.01)
    lines = [
        f"\U0001f4d0 *Levels \u2014 {escape(symbol)}* "
        f"\\({escape(str(snapshot.timeframe))}\\)",
        "",
    ]

    if snapshot.levels:
        lines.append(_SR_HEADER)
        for level in sorted(snapshot.levels, key=lambda x: -x.strength)[:6]:
            lines.append(
                f" \u2022 `{price(level.price, point)}` "
                f"strength `{escape(f'{level.strength:.2f}')}` "
                f"touches {level.touches}"
            )
    pools = [p for p in snapshot.pools if p.available]
    if pools:
        lines += ["", "*Unswept pools*"]
        for pool in sorted(pools, key=lambda p: -p.touches)[:6]:
            lines.append(
                f" \u2022 `{price(pool.level, point)}` {escape(str(pool.side))} "
                f"\u00b7 {escape(str(pool.kind))} \u00b7 {pool.touches} touches"
            )
    swept = [p for p in snapshot.pools if not p.available]
    if swept:
        lines += ["", f"*Swept* \\({len(swept)}\\) \u2014 retained as features, not targets"]
    arrays = [f for f in snapshot.fvgs if f.available]
    if arrays:
        lines += ["", "*Unmitigated FVGs*"]
        for fvg in sorted(arrays, key=lambda f: -f.confirmed_at.timestamp())[:6]:
            lines.append(
                f" \u2022 `{price(fvg.lo, point)}`\u2013`{price(fvg.hi, point)}` "
                f"{escape(fvg.effective_direction.label)} \u00b7 "
                f"fill `{escape(f'{fvg.fill_frac:.0%}')}`"
            )
    if snapshot.dealing_range is not None:
        dr = snapshot.dealing_range
        close = snapshot.last_close
        position = dr.position(close) if close is not None else None
        lines += [
            "",
            f"*Dealing range* `{price(dr.low, point)}`\u2013`{price(dr.high, point)}`",
            f"  equilibrium `{price(dr.equilibrium, point)}`"
            + (f" \u00b7 position `{escape(f'{position:.2f}')}`" if position is not None else ""),
        ]
    return "\n".join(lines)


def pause(ctx: CommandContext) -> str:
    _ = ctx
    return escape(
        "Paused. No new signals will be emitted. Lifecycle tracking continues, and the "
        "pause survives a restart."
    )


def resume(ctx: CommandContext) -> str:
    _ = ctx
    return escape("Resumed. New signals may be emitted again.")


def unauthorised(user_id: str) -> str:
    return f"ignored command from unauthorised user `{escape(user_id)}`"

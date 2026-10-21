"""Telegram message formatting. `docs/03-signal-engine.md` §7.3.

MarkdownV2 escaping is mandatory, not cosmetic: an unescaped `.` or `-` in an interpolated
instrument name or price makes the Bot API reject the whole message, and the failure is a
400 rather than anything visible in the text. Every interpolated value therefore goes
through `escape`.

Both timezones are shown (`docs/02-definitions.md` §0.2): New York because the session
logic is anchored there, Singapore because that is where it is read. Stored values stay UTC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aurion.analysis.base import Direction
from aurion.signals.engine import Candidate
from aurion.signals.lifecycle import SignalState, TrackedSignal, Transition

NY = ZoneInfo("America/New_York")
SGT = ZoneInfo("Asia/Singapore")

#: MarkdownV2 reserves all of these. Telegram rejects a message containing any unescaped.
_RESERVED = r"_*[]()~`>#+-=|{}.!"

#: Rendered inside a code span, where brackets need no escaping.
_PAPER = "[PAPER]"

_DIRECTION_EMOJI = {Direction.UP: "\U0001f7e2", Direction.DOWN: "\U0001f534"}

_STATE_EMOJI = {
    SignalState.TRIGGERED: "▶️",
    SignalState.TP1_HIT: "\U0001f3af",
    SignalState.TP2_HIT: "\U0001f3af",
    SignalState.TP3_HIT: "\U0001f3c1",
    SignalState.BE_STOP: "\U0001f6e1️",
    SignalState.SL_HIT: "❌",
    SignalState.SL_AFTER_TP: "❌",
    SignalState.EXPIRED: "⏱️",
    SignalState.CANCELLED: "\U0001f6ab",
    SignalState.FORCE_CLOSED: "\U0001f512",
}


def paper_tag(paper: bool) -> str:
    """The `[PAPER]` marker, or nothing.

    A function rather than a constant per module: `commands.py` kept its own copy and the
    copy carried prose escaping inside a code span, so `/stats` rendered `\\[PAPER\\]`.
    """
    return f" {code(_PAPER)}" if paper else ""


def code(value: object) -> str:
    """Wrap a value in a MarkdownV2 code span, escaped by the rule for code entities.

    A different rule from `escape`, and conflating the two is a bug rather than a style
    point. Inside a code entity Telegram requires only a backslash and a backtick to be
    escaped, and requires EVERY backslash to be escaped -- so prose-escaped text in a code
    span renders its backslashes literally and can make Telegram reject the whole message
    with "can't parse entities". That was every price in every alert.
    """
    inner = str(value).replace("\\", "\\\\").replace("`", "\\`")
    return f"`{inner}`"


def escape(text: object) -> str:
    """Escape every MarkdownV2 reserved character. For prose, never for a code span."""
    out = str(text)
    for ch in _RESERVED:
        out = out.replace(ch, f"\\{ch}")
    return out


def both_zones(at: datetime) -> str:
    return (
        f"{escape(at.astimezone(NY).strftime('%H:%M'))} NY / "
        f"{escape(at.astimezone(SGT).strftime('%H:%M'))} SGT"
    )


def price(value: float, point: float) -> str:
    """Format to the instrument's precision, derived from its point size.

    Returns the plain number: wrap it in `code` for a code span or `escape` for prose. It
    cannot do both, and when it escaped unconditionally every price in a code span carried
    a literal backslash.
    """
    decimals = max(0, -1 * round(math.log10(point))) if point > 0 else 2
    return f"{value:.{decimals}f}"


@dataclass(frozen=True, slots=True)
class ReasonText:
    """Human strings for feature ids, so "top 3 reasons" stays auditable.

    The reasons come from the model's own log-odds attribution, not from prose. The mapping
    here only turns a feature id into readable English; it cannot invent a reason that the
    model did not weight.
    """

    mapping: dict[str, str]

    def render(self, feature: str, contribution: float) -> str:
        label = self.mapping.get(feature, feature.replace("_", " "))
        # The contribution went in raw, so a reason line carried an unescaped `+` and
        # `.` in prose -- both MarkdownV2-reserved, which makes Telegram reject the whole
        # message. A code span rather than `escape` because it is a number: it reads
        # better and cannot be under-escaped again.
        return f"{escape(label)} \\({code(f'{contribution:+.2f}')} log\\-odds\\)"


DEFAULT_REASONS = ReasonText(
    {
        "sweep_depth_atr": "swept liquidity depth",
        "bars_since_sweep": "recency of the sweep",
        "break_is_mss": "the break was a market structure shift",
        "dist_nearest_fvg_atr": "distance to the nearest fair value gap",
        "nearest_fvg_fill": "fill state of that gap",
        "in_ote": "price inside the optimal trade entry band",
        "range_position": "position in the dealing range",
        "dist_nearest_level_atr": "distance to the nearest S&R level",
        "nearest_level_strength": "strength of that level",
        "atr_pct": "volatility percentile",
        "adx": "trend strength (ADX)",
        "vr_4_z": "variance-ratio statistic",
        "session_is_ny_am": "inside the New York AM kill zone",
        "session_is_london": "inside the London kill zone",
        "qt_align": "quarterly true-open alignment",
        "net_usd_exposure": "net USD exposure",
    }
)


def format_signal(
    candidate: Candidate,
    point: float,
    paper: bool = True,
    reasons: ReasonText = DEFAULT_REASONS,
    brier: float | None = None,
) -> str:
    """The signal alert.

    Confidence is reported as a **calibrated probability** with its out-of-sample Brier
    score, not as a 0-100 score: a points score invites reading a precision it does not
    have.
    """
    geometry = candidate.geometry
    if geometry is None or candidate.direction is None:
        raise ValueError("cannot format a candidate without geometry")

    emoji = _DIRECTION_EMOJI[candidate.direction]
    side = "LONG" if candidate.direction is Direction.UP else "SHORT"
    tag = paper_tag(paper)
    risk = geometry.risk

    lines = [
        f"{emoji} *{escape(candidate.instrument)} — {side}*{tag}",
        "",
        f"Entry  {code(price(geometry.entry, point))} "
        f"\\({escape(candidate.proposal.entry_array_label if candidate.proposal else 'limit')}\\)",
        f"SL     {code(price(geometry.stop_loss, point))}  "
        f"\\({escape(price(risk, point))} risk\\)",
    ]
    for i, target in enumerate(geometry.targets, start=1):
        r_multiple = target.r_multiple(geometry.entry, geometry.stop_loss)
        lines.append(
            f"TP{i}    {code(price(target.price, point))}  "
            f"\\(R {escape(f'{r_multiple:.2f}')} · {escape(target.label)}\\)"
        )

    lines += [""]
    if candidate.p_hat is not None:
        brier_text = f" · fold Brier {escape(f'{brier:.3f}')}" if brier is not None else ""
        lines.append(
            f"Confidence  {code(f'{candidate.p_hat:.2f}')} "
            f"\\(calibrated{brier_text}\\)"
        )
    if candidate.expected_r is not None:
        lines.append(f"E\\[R\\]       {code(f'{candidate.expected_r:+.2f}')}")
    if candidate.size is not None:
        if candidate.size.lots is None:
            lines.append(f"Size        {escape('size unavailable: ' + candidate.size.note)}")
        elif candidate.size.below_minimum:
            lines.append(f"Size        {code('0.00')} lots — {escape(candidate.size.note)}")
        else:
            lines.append(
                f"Size        {code(f'{candidate.size.lots:.2f}')} lots "
                f"\\(risk {escape(f'{candidate.size.risk_amount:.0f}')}\\)"
            )

    lines += [""]
    if candidate.gate is not None:
        lines.append(f"Session  {escape(candidate.gate.window_label)}")
    if candidate.reasons:
        lines += ["", "*Why*"]
        lines += [
            f" • {reasons.render(name, value)}" for name, value in candidate.reasons
        ]
    if candidate.proposal is not None:
        lines += ["", f"Invalid if  {escape(candidate.proposal.invalidation)}"]
    if candidate.expires_at is not None:
        lines.append(f"Expires     {both_zones(candidate.expires_at)}")
    lines.append(f"\n{both_zones(candidate.as_of)}")
    return "\n".join(lines)


def format_transition(
    signal: TrackedSignal, transition: Transition, point: float, paper: bool = True
) -> str:
    """A lifecycle update. One message per transition, deduped by state and revision."""
    emoji = _STATE_EMOJI.get(transition.state, "ℹ️")
    tag = paper_tag(paper)
    side = "LONG" if signal.direction is Direction.UP else "SHORT"
    label = escape(str(transition.state).replace("_", " ").upper())
    lines = [
        f"{emoji} *{escape(signal.instrument)} {side} — {label}*{tag}",
        "",
        f"at {code(price(transition.price, point))}",
        f"realised  {code(f'{signal.realised_r:+.2f}')} R",
    ]
    if transition.note:
        lines.append(f"\n{escape(transition.note)}")
    lines.append(f"\n{both_zones(transition.at)}")
    return "\n".join(lines)


def format_rejection_digest(by_reason: dict[str, int], total: int) -> str:
    """Admin-channel funnel digest. Counts, because the counts are the point."""
    lines = [f"\U0001f4ca *Candidate funnel* — {total} evaluated", ""]
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1])[:10]:
        lines.append(f" • {code(reason)} {n}")
    return "\n".join(lines)

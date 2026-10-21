"""Annotated chart rendering. `docs/03-signal-engine.md` §7.3.

Rendering runs in the single-worker thread pool and is **optional**: if it exceeds the
timeout the text message is sent without it rather than delaying the alert. A chart is a
convenience; the alert is the product.

matplotlib is imported lazily and forced onto the Agg backend. Importing it at module load
would cost ~150 MB of RSS on a 1-2 GB box for a feature that may be disabled, and the
default backend tries to find a display that does not exist on a VPS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from aurion.analysis.base import Direction
from aurion.data.types import Bar

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence


@dataclass(frozen=True, slots=True)
class ChartAnnotation:
    """What to draw on top of the candles."""

    entry: float | None = None
    stop_loss: float | None = None
    targets: tuple[float, ...] = ()
    swept_level: float | None = None
    sweep_extremity: float | None = None
    arrays: tuple[tuple[float, float, str], ...] = ()
    levels: tuple[float, ...] = ()
    title: str = ""
    direction: Direction | None = None
    notes: list[str] = field(default_factory=list)


def render_signal_chart(
    bars: Sequence[Bar],
    annotation: ChartAnnotation,
    out_path: str | Path,
    max_bars: int = 120,
) -> Path | None:
    """Render candles with levels, arrays, the sweep and entry/SL/TP.

    Returns ``None`` when rendering is impossible (no bars, matplotlib unavailable) rather
    than raising: a chart failure must not stop a signal going out.
    """
    window = list(bars)[-max_bars:]
    if not window:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except Exception:  # pragma: no cover - charting is optional by design
        return None

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=110)
    try:
        for i, bar in enumerate(window):
            o = bar.ohlc
            up = o.c >= o.o
            colour = "#2e7d32" if up else "#c62828"
            ax.vlines(i, o.l, o.h, color=colour, linewidth=0.8)
            ax.add_patch(
                Rectangle(
                    (i - 0.3, min(o.o, o.c)),
                    0.6,
                    max(abs(o.c - o.o), (o.h - o.l) * 1e-3),
                    facecolor=colour,
                    edgecolor=colour,
                    linewidth=0.4,
                )
            )

        span = len(window)
        for lo, hi, label in annotation.arrays:
            ax.add_patch(
                Rectangle(
                    (0, lo), span, max(hi - lo, 1e-9),
                    facecolor="#1565c0", alpha=0.12, edgecolor="none",
                )
            )
            ax.text(span * 0.01, hi, label, fontsize=7, color="#1565c0", va="bottom")

        for level in annotation.levels:
            ax.axhline(level, color="#9e9e9e", linewidth=0.6, linestyle=":")
        if annotation.swept_level is not None:
            ax.axhline(
                annotation.swept_level, color="#f9a825", linewidth=1.1, linestyle="--"
            )
            ax.text(0, annotation.swept_level, " swept pool", fontsize=7, color="#f9a825",
                    va="bottom")
        if annotation.sweep_extremity is not None:
            ax.axhline(
                annotation.sweep_extremity, color="#f9a825", linewidth=0.7, alpha=0.6
            )
        if annotation.entry is not None:
            ax.axhline(annotation.entry, color="#1565c0", linewidth=1.2)
            ax.text(span * 0.75, annotation.entry, " entry", fontsize=8, color="#1565c0",
                    va="bottom")
        if annotation.stop_loss is not None:
            ax.axhline(annotation.stop_loss, color="#c62828", linewidth=1.2)
            ax.text(span * 0.75, annotation.stop_loss, " SL", fontsize=8, color="#c62828",
                    va="top")
        for i, target in enumerate(annotation.targets, start=1):
            ax.axhline(target, color="#2e7d32", linewidth=1.0, alpha=0.85)
            ax.text(span * 0.75, target, f" TP{i}", fontsize=8, color="#2e7d32", va="bottom")

        ax.set_title(annotation.title, fontsize=10)
        ax.set_xlim(-1, span)
        ax.grid(alpha=0.15, linewidth=0.5)
        ax.set_xticks([])
        ax.tick_params(labelsize=8)
        if annotation.notes:
            ax.text(
                0.01, 0.02, "\n".join(annotation.notes), transform=ax.transAxes,
                fontsize=7, color="#555555", va="bottom",
            )
        fig.tight_layout()
        fig.savefig(path, bbox_inches="tight")
    finally:
        plt.close(fig)
    return path

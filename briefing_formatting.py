"""Money and weight formatting shared by the briefing modules."""

from __future__ import annotations


def fmt_money(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.2f}M".replace(".00M", "M")
    return f"${x:,.0f}"


def fmt_weight(w: float) -> str:
    return f"{w:.1f}%"

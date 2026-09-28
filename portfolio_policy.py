"""Load and validate the user-editable portfolio decision policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_POLICY_PATH = PROJECT_ROOT / "portfolio_policy.json"


@dataclass(frozen=True)
class PortfolioPolicy:
    schema_version: int
    single_name_cap_pct: float
    cluster_do_not_increase_pct: float
    cluster_soft_cap_pct: float
    analyze_weight_floor_pct: float
    analyze_market_value_floor: float
    material_loss_dollars: float
    material_loss_pct: float
    harvest_loss_dollars: float
    risk_off_glide_pct: float
    st_gain_flag_dollars: float
    lt_gain_flag_dollars: float
    cash_symbols: frozenset[str]
    sleeves: dict[str, tuple[str, ...]]
    aliases: dict[str, str]
    marginal_examples: tuple[tuple[str, str | None, str], ...]


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> PortfolioPolicy:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError(f"Unsupported policy schema in {path}")
    required_sleeves = {
        "direct_ai_semi", "broad_ai_cycle", "crypto", "payments", "broad_index"
    }
    sleeves = raw.get("sleeves") or {}
    missing = required_sleeves - set(sleeves)
    if missing:
        raise ValueError(f"Missing policy sleeves: {', '.join(sorted(missing))}")
    examples = []
    for item in raw.get("marginal_examples") or ():
        label = str(item["label"])
        symbol = item.get("symbol")
        symbol = str(symbol).upper() if symbol else None
        examples.append((label, symbol, str(item.get("note") or "")))
    if not examples:
        examples = (
            ("VOO", "VOO", "already top-5; diversifies factor"),
            ("MSFT", "MSFT", "broad AI only — not direct semi"),
            ("GOOGL", "GOOGL", "top-5 + broad AI-cycle"),
            ("NVDA", "NVDA", "raises direct stack"),
            ("New non-AI diversifier", None, "improves concentration / factor"),
        )
    return PortfolioPolicy(
        schema_version=raw["schema_version"],
        single_name_cap_pct=float(raw["single_name_cap_pct"]),
        cluster_do_not_increase_pct=float(raw["cluster_do_not_increase_pct"]),
        cluster_soft_cap_pct=float(raw["cluster_soft_cap_pct"]),
        analyze_weight_floor_pct=float(raw["analyze_weight_floor_pct"]),
        analyze_market_value_floor=float(raw["analyze_market_value_floor"]),
        material_loss_dollars=float(raw["material_loss_dollars"]),
        material_loss_pct=float(raw["material_loss_pct"]),
        harvest_loss_dollars=float(raw["harvest_loss_dollars"]),
        risk_off_glide_pct=float(raw["risk_off_glide_pct"]),
        st_gain_flag_dollars=float(raw.get("st_gain_flag_dollars", 500.0)),
        lt_gain_flag_dollars=float(raw.get("lt_gain_flag_dollars", 2000.0)),
        cash_symbols=frozenset(str(s).upper() for s in raw["cash_symbols"]),
        sleeves={k: tuple(str(s).upper() for s in v) for k, v in sleeves.items()},
        aliases={str(k).upper(): str(v) for k, v in (raw.get("aliases") or {}).items()},
        marginal_examples=tuple(examples),
    )


_active: PortfolioPolicy | None = None


def active_policy() -> PortfolioPolicy:
    """Return the policy in effect, loading portfolio_policy.json on first use."""
    global _active
    if _active is None:
        _active = load_policy()
    return _active


def set_active_policy(policy: PortfolioPolicy) -> PortfolioPolicy:
    """Replace the policy in effect (CLI --policy, tests). Returns the previous one."""
    global _active
    previous = active_policy()
    _active = policy
    return previous


def __getattr__(name: str):
    # Backwards-compatible `from portfolio_policy import POLICY`, resolved lazily
    # so importing this module never reads the JSON file by itself.
    if name == "POLICY":
        return active_policy()
    raise AttributeError(name)

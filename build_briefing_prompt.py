#!/usr/bin/env python3
"""
Build a ready-to-paste Daily Portfolio Action Briefing prompt
with LIVE E*TRADE portfolio values already filled in (no static placeholders).

The instruction layer is a portfolio decision engine (constraints → factor
budget → thesis/valuation → tax/lot → opportunity cost), not a stock checklist.
"""

from __future__ import annotations

import json
import re
import sys
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from get_portfolio import (
    account_tail,
    atomic_write_text,
    collect_tax_flags,
    copy_to_clipboard,
    fetch_portfolio_block,
    format_lot_table,
    is_cash_symbol,
    parse_portable_portfolio_text,
    read_clipboard_text,
    save_portfolio_block,
)
from portfolio_policy import POLICY

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "prompts"
BRIEFINGS_DIR = PROJECT_ROOT / "briefings"
CONTEXT_PATH = PROJECT_ROOT / "portfolio_context.json"


@dataclass(frozen=True)
class BriefingBuildResult:
    prompt: str
    weights_snapshot: dict
    observation_snapshot: dict


@dataclass(frozen=True)
class BriefingOutputPolicy:
    mode: str
    material_symbols: tuple[str, ...]
    refresh_candidates: bool
    include_tax_detail: bool

# User-editable decision policy (portfolio_policy.json).
AI_SEMI_CLUSTER = POLICY.sleeves["direct_ai_semi"]
BROAD_AI_LIQUID = POLICY.sleeves["broad_ai_cycle"]
CRYPTO_CLUSTER = POLICY.sleeves["crypto"]
PAYMENTS_CLUSTER = POLICY.sleeves["payments"]
BROAD_INDEX = POLICY.sleeves["broad_index"]
ANALYZE_WEIGHT_FLOOR = POLICY.analyze_weight_floor_pct
ANALYZE_MV_FLOOR = POLICY.analyze_market_value_floor
MATERIAL_LOSS_DOLLARS = POLICY.material_loss_dollars
MATERIAL_LOSS_PCT = POLICY.material_loss_pct
HARVEST_LOSS_DOLLARS = POLICY.harvest_loss_dollars
RISK_OFF_GLIDE = POLICY.risk_off_glide_pct
SINGLE_NAME_CAP = POLICY.single_name_cap_pct
CLUSTER_NO_ADD = POLICY.cluster_do_not_increase_pct
CLUSTER_SOFT_CAP = POLICY.cluster_soft_cap_pct
ALIASES = POLICY.aliases
MARGINAL_EXAMPLES = POLICY.marginal_examples


def load_portfolio_context(path: Path = CONTEXT_PATH) -> tuple[dict, str]:
    """Load optional, user-maintained research and decision continuity data."""
    if not path.exists():
        return {}, "_No portfolio_context.json found; use the example file to add research continuity._"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"_portfolio_context.json was not usable: {exc}. Treat context as unavailable._"
    if not isinstance(data, dict):
        return {}, "_portfolio_context.json must contain a JSON object; treat context as unavailable._"
    return data, f"_Loaded local portfolio context updated {data.get('updated_at', 'date not recorded')}._"


def format_owner_profile(context: dict) -> str:
    profile = context.get("owner_profile")
    if not isinstance(profile, dict):
        return "_Not supplied. Treat horizon, liquidity reserve, withdrawals, tax rate, external balance sheet, and hard limits as UNKNOWN._"
    fields = [
        ("Horizon", "horizon"),
        ("Liquidity reserve", "liquidity_reserve"),
        ("Planned withdrawals", "planned_withdrawals"),
        ("Marginal tax rate", "marginal_tax_rate"),
        ("External assets / liabilities", "external_assets_liabilities"),
        ("Hard loss limit", "hard_loss_limit"),
        ("Hard position limit", "hard_position_limit"),
    ]
    return "\n".join(
        f"- **{label}:** {profile.get(key) or 'UNKNOWN'}" for label, key in fields
    )


def format_research_records(context: dict) -> str:
    records = context.get("research")
    if not isinstance(records, dict) or not records:
        return "_No maintained research records supplied. Do not label a holding KEEP because its thesis is presumed intact._"
    rows = []
    for ticker, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        rows.append(
            "| {ticker} | {reviewed} | {view} | {basis} | {valuation} | {sources} | {trigger} | {missing} |".format(
                ticker=ticker.upper(),
                reviewed=record.get("reviewed_at") or "UNKNOWN",
                view=record.get("fundamental_view") or "UNKNOWN",
                basis=record.get("decision_basis") or "UNKNOWN",
                valuation=record.get("valuation_or_entry") or "UNKNOWN",
                sources="; ".join(record.get("sources", [])) or "UNKNOWN",
                trigger=record.get("decision_trigger") or "UNKNOWN",
                missing=record.get("missing_information") or "none recorded",
            )
        )
    if not rows:
        return "_No usable research records supplied._"
    return (
        "| Ticker | Reviewed | Fundamental view | Decision basis | Valuation / entry | Sources | Decision trigger | Missing information |\n"
        "|---|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
    )


def format_decision_history(context: dict) -> str:
    history = context.get("decision_history")
    if not isinstance(history, list) or not history:
        return "_No prior analytical proposal was captured. Use NOT CAPTURED; do not infer it from broker changes._"
    rows = []
    for item in history[-12:]:
        if not isinstance(item, dict):
            continue
        rows.append(
            "| {date} | {scope} | {action} | {basis} | {rationale} | {trigger} | {approval} |".format(
                date=item.get("as_of") or "UNKNOWN",
                scope=item.get("scope") or "UNKNOWN",
                action=item.get("portfolio_action") or "UNKNOWN",
                basis=item.get("decision_basis") or "UNKNOWN",
                rationale=item.get("rationale") or "UNKNOWN",
                trigger=item.get("decision_trigger") or "UNKNOWN",
                approval=item.get("approval_state") or "NOT RECORDED",
            )
        )
    if not rows:
        return "_No usable prior analytical proposal was captured._"
    return (
        "| As of | Scope | Proposal | Decision basis | Rationale | Trigger | Approval state |\n"
        "|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
    )


def sleeve_member_text(symbols: tuple[str, ...]) -> str:
    return " ".join(symbols)


def broad_ai_sleeve_label() -> str:
    extras = [s for s in BROAD_AI_LIQUID if s not in AI_SEMI_CLUSTER]
    if extras:
        return "direct + " + " ".join(extras)
    return "direct"


def consolidate(results, grand_total: float):
    by_sym: dict[str, dict] = defaultdict(
        lambda: {
            "market_value": 0.0,
            "price": 0.0,
            "qty": 0.0,
            "total_cost": 0.0,
            "total_gain": 0.0,
            "cost_known": False,
            "gain_known": False,
            "cost_complete": True,
            "gain_complete": True,
            "price_paid_num": 0.0,
            "price_paid_den": 0.0,
            "lots": [],
            "tax_buckets": set(),
            "taxable_mv": 0.0,
            "ira_mv": 0.0,
            "roth_mv": 0.0,
        }
    )
    accounts = []
    for r in results:
        if r.get("error"):
            continue
        bucket = r.get("tax_bucket") or "taxable"
        accounts.append(
            {
                "label": r["label"],
                "total": r["total_value"],
                "tax_bucket": bucket,
            }
        )
        for h in r["holdings"]:
            s = h["symbol"]
            d = by_sym[s]
            d["market_value"] += h["market_value"]
            d["price"] = h["price"]
            qty = h.get("quantity") or 0.0
            d["qty"] += qty
            if h.get("total_cost"):
                d["total_cost"] += h["total_cost"]
                d["cost_known"] = True
            else:
                d["cost_complete"] = False
            if h.get("total_gain") is not None:
                d["total_gain"] += h["total_gain"]
                d["gain_known"] = True
            else:
                d["gain_complete"] = False
            paid = h.get("price_paid")
            if paid and qty:
                d["price_paid_num"] += paid * qty
                d["price_paid_den"] += qty
            if h.get("lots"):
                d["lots"].extend(h["lots"])
            elif h.get("date_acquired"):
                d["lots"].append(
                    {
                        "qty": qty,
                        "total_cost": h.get("total_cost") or 0.0,
                        "total_gain": h.get("total_gain") or 0.0,
                        "market_value": h.get("market_value") or 0.0,
                        "acquired": h["date_acquired"],
                    }
                )
            d["tax_buckets"].add(bucket)
            if bucket == "taxable":
                d["taxable_mv"] += h["market_value"]
            elif bucket == "roth":
                d["roth_mv"] += h["market_value"]
            else:
                d["ira_mv"] += h["market_value"]

    holdings = []
    for sym, d in sorted(by_sym.items(), key=lambda x: -x[1]["market_value"]):
        mv = d["market_value"]
        w = (mv / grand_total * 100) if grand_total else 0
        qty = d["qty"]
        cost_complete = d["cost_known"] and d["cost_complete"]
        gain_complete = d["gain_known"] and d["gain_complete"]
        cps = (d["total_cost"] / qty) if qty and cost_complete else None
        paid = d["price_paid_num"] / d["price_paid_den"] if d["price_paid_den"] else None
        tgp = (
            100.0 * d["total_gain"] / d["total_cost"]
            if cost_complete and gain_complete and d["total_cost"]
            else None
        )
        holdings.append(
            {
                "symbol": sym,
                "market_value": mv,
                "weight": w,
                "price": d["price"],
                "alias": ALIASES.get(sym),
                "quantity": qty,
                "cost_per_share": cps,
                "price_paid": paid,
                "total_cost": d["total_cost"] if cost_complete else None,
                "total_gain": d["total_gain"] if gain_complete else None,
                "total_gain_pct": tgp,
                "lots": d["lots"],
                "tax_buckets": d["tax_buckets"],
                "taxable_mv": d["taxable_mv"],
                "ira_mv": d["ira_mv"],
                "roth_mv": d["roth_mv"],
            }
        )
    return holdings, accounts


def fmt_money(x: float) -> str:
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:.2f}M".replace(".00M", "M")
    return f"${x:,.0f}"


def fmt_weight(w: float) -> str:
    return f"{w:.1f}%"


def holding_line(h: dict, as_of) -> str:
    alias = f" ({h['alias']})" if h.get("alias") else ""
    sym = h["symbol"]
    if is_cash_symbol(sym):
        return f"- Cash ({sym}) ≈ {fmt_money(h['market_value'])} (~{fmt_weight(h['weight'])})"
    bits = []
    tgp = h.get("total_gain_pct")
    if tgp is not None:
        bits.append(f"{tgp:+.0f}%")
    loc = []
    if h.get("taxable_mv"):
        loc.append(f"taxable {fmt_money(h['taxable_mv'])}")
    if h.get("ira_mv"):
        loc.append(f"IRA {fmt_money(h['ira_mv'])}")
    if h.get("roth_mv"):
        loc.append(f"Roth {fmt_money(h['roth_mv'])}")
    if len(loc) > 1:
        bits.append(" + ".join(loc))
    elif h.get("ira_mv") and not h.get("taxable_mv") and not h.get("roth_mv"):
        bits.append("IRA")
    elif h.get("roth_mv") and not h.get("taxable_mv") and not h.get("ira_mv"):
        bits.append("Roth")
    glance = f"  {'  '.join(bits)}" if bits else ""
    return (
        f"- {sym}{alias} {fmt_weight(h['weight'])}  "
        f"({fmt_money(h['market_value'])} @ ${h['price']:.2f}){glance}"
    )


def sleeve_stats(holdings: list[dict], symbols: tuple[str, ...], total: float):
    members = [h for h in holdings if h["symbol"] in symbols]
    mv = sum(h["market_value"] for h in members)
    w = (mv / total * 100) if total else 0.0
    return mv, w, [h["symbol"] for h in members]


def taxable_harvest_reviews(results) -> list[dict]:
    """Return account-level taxable-loss reviews from the source positions.

    This deliberately avoids consolidated ticker P/L: an IRA gain in the same
    ticker must not hide a taxable loss that still requires review.
    """
    reviews = []
    for result in results or []:
        if result.get("error") or result.get("tax_bucket") != "taxable":
            continue
        label = result.get("label") or "Taxable account"
        for holding in result.get("holdings") or []:
            symbol = holding.get("symbol") or ""
            if not symbol or is_cash_symbol(symbol):
                continue
            market_value = holding.get("market_value") or 0.0
            total_gain = holding.get("total_gain")
            if (
                market_value >= ANALYZE_MV_FLOOR
                and total_gain is not None
                and total_gain < HARVEST_LOSS_DOLLARS
            ):
                reviews.append(
                    {
                        "symbol": symbol,
                        "account": account_tail(label) or label,
                        "quantity": holding.get("quantity") or 0.0,
                        "market_value": market_value,
                        "total_gain": total_gain,
                    }
                )
    return reviews


def format_observable_reviews(harvest_reviews: list[dict], breaches: list[dict]) -> str:
    """Format deterministic review flags. These are never approvals or orders."""
    lines = []
    for row in harvest_reviews:
        loss = fmt_money(abs(row["total_gain"]))
        lines.append(
            f"- **{row['symbol']} / {row['account']} — OPEN_TLH_REVIEW:** "
            f"qty {row['quantity']:,.4g}; MV {fmt_money(row['market_value'])}; "
            f"unrealized loss -{loss}. Review only — not an approved sale or order."
        )
    for holding in breaches:
        lines.append(
            f"- **{holding['symbol']} — CONCENTRATION_REVIEW:** "
            f"{fmt_weight(holding['weight'])} exceeds the "
            f"{SINGLE_NAME_CAP:g}% soft maximum. Review only — not an approved sale."
        )
    if not lines:
        return "- No automated TLH or single-name concentration review flags."
    return "\n".join(lines)


def must_analyze_holdings(
    holdings: list[dict], required_harvest_symbols: set[str] | None = None
) -> list[dict]:
    """Names that must appear in the briefing position table.

    Harvest and material-loss names stay in even when they sit under the weight floor.
    """
    out = []
    for h in holdings:
        sym = h.get("symbol") or ""
        if is_cash_symbol(sym):
            continue
        reasons: list[str] = []
        mv = h.get("market_value") or 0.0
        if (h.get("weight") or 0.0) >= ANALYZE_WEIGHT_FLOOR:
            reasons.append("weight")
        tg = h.get("total_gain")
        tgp = h.get("total_gain_pct")
        sized = mv >= ANALYZE_MV_FLOOR
        if required_harvest_symbols is not None:
            harvest_required = sym in required_harvest_symbols
        else:
            taxable_mv = h.get("taxable_mv") or 0.0
            harvest_required = (
                sized and taxable_mv > 0 and tg is not None and tg < HARVEST_LOSS_DOLLARS
            )
        if harvest_required:
            reasons.append("harvest")
        if (
            sized
            and tg is not None
            and (tg <= MATERIAL_LOSS_DOLLARS or (tgp is not None and tgp <= MATERIAL_LOSS_PCT))
        ):
            reasons.append("loss")
        if reasons:
            row = dict(h)
            row["analyze_reasons"] = reasons
            out.append(row)
    return out


def format_must_analyze(rows: list[dict]) -> str:
    weight = [r["symbol"] for r in rows if "weight" in r["analyze_reasons"]]
    extra = [r for r in rows if "weight" not in r["analyze_reasons"]]
    lines = []
    if weight:
        lines.append(f"- Weight ≥ {ANALYZE_WEIGHT_FLOOR:g}%: " + ", ".join(weight))
    if extra:
        bits = []
        for row in extra:
            tags = []
            if "harvest" in row["analyze_reasons"]:
                tags.append("taxable harvest")
            tgp, tg = row.get("total_gain_pct"), row.get("total_gain")
            if "loss" in row["analyze_reasons"]:
                tags.append(f"{tgp:+.0f}%" if tgp is not None else fmt_money(tg))
            bits.append(row["symbol"] + (f" ({', '.join(tags)})" if tags else ""))
        lines.append("- Additional required analysis: " + ", ".join(bits))
    if not lines:
        return "- None"
    return "\n".join(lines)


def _position_key(row: dict) -> tuple[str, str]:
    return str(row.get("account_ref") or ""), str(row.get("symbol") or "")


def _lot_identity(row: dict) -> list[tuple]:
    return [
        (
            lot.get("acquired"),
            round(float(lot.get("quantity") or 0.0), 8),
            round(float(lot.get("total_cost") or 0.0), 2),
        )
        for lot in row.get("lots") or []
    ]


def _changed_weight_symbols(today_snap: dict, prior_snap: dict | None) -> set[str]:
    if not prior_snap:
        return set()
    previous = {row["symbol"]: row for row in prior_snap.get("holdings") or []}
    current = {row["symbol"]: row for row in today_snap.get("holdings") or []}
    changed = set()
    for symbol in set(previous) | set(current):
        if is_cash_symbol(symbol):
            continue
        old = previous.get(symbol)
        new = current.get(symbol)
        if old is None or new is None:
            changed.add(symbol)
            continue
        if abs((new.get("weight") or 0.0) - (old.get("weight") or 0.0)) >= 0.5:
            changed.add(symbol)
    return changed


def _changed_observation_symbols(today: dict, prior: dict | None) -> set[str]:
    if not prior:
        return set()
    previous = {_position_key(row): row for row in prior.get("positions") or []}
    current = {_position_key(row): row for row in today.get("positions") or []}
    changed = set()
    for key in set(previous) | set(current):
        old = previous.get(key)
        new = current.get(key)
        row = new or old or {}
        symbol = row.get("symbol") or ""
        if not symbol or is_cash_symbol(symbol):
            continue
        if old is None or new is None:
            changed.add(symbol)
            continue
        old_qty = float(old.get("quantity") or 0.0)
        new_qty = float(new.get("quantity") or 0.0)
        tolerance = max(1e-6, abs(old_qty) * 1e-8)
        if abs(new_qty - old_qty) > tolerance or _lot_identity(old) != _lot_identity(new):
            changed.add(symbol)
    previous_reviews = {
        (row.get("kind"), row.get("symbol"), row.get("account"))
        for row in prior.get("reviews") or []
    }
    current_reviews = {
        (row.get("kind"), row.get("symbol"), row.get("account"))
        for row in today.get("reviews") or []
    }
    changed.update(row[1] for row in previous_reviews ^ current_reviews if row[1])
    return changed


def briefing_output_policy(
    holdings: list[dict],
    harvest_reviews: list[dict],
    breaches: list[dict],
    today_snap: dict,
    prior_snap: dict | None,
    today_observation: dict,
    prior_observation: dict | None,
    as_of: datetime,
) -> BriefingOutputPolicy:
    """Choose compact daily output or expanded decision-day output."""
    weight_changes = _changed_weight_symbols(today_snap, prior_snap)
    observation_changes = _changed_observation_symbols(today_observation, prior_observation)

    first_run = prior_snap is None and prior_observation is None
    decision_day = first_run or bool(weight_changes or observation_changes)
    if prior_snap:
        prior_breaches = set(prior_snap.get("breaches") or [])
        current_breaches = {row["symbol"] for row in breaches}
        decision_day = decision_day or prior_breaches != current_breaches

    previous_date = None
    if prior_snap:
        raw_date = prior_snap.get("date") or prior_snap.get("as_of")
        if raw_date:
            try:
                previous_date = datetime.fromisoformat(str(raw_date)).date()
            except ValueError:
                previous_date = None
    gap = (as_of.date() - previous_date).days if previous_date else None
    refresh_candidates = first_run or (gap is not None and gap >= 7) or decision_day
    include_tax_detail = bool(harvest_reviews) and decision_day
    required = must_analyze_holdings(
        holdings, {row["symbol"] for row in harvest_reviews}
    )
    material = (
        {row["symbol"] for row in required}
        if decision_day
        else {row["symbol"] for row in harvest_reviews}
    )
    material.update(weight_changes | observation_changes)
    material.update(row["symbol"] for row in breaches)
    return BriefingOutputPolicy(
        mode="DECISION DAY" if decision_day else "ROUTINE DAY",
        material_symbols=tuple(sorted(material)),
        refresh_candidates=refresh_candidates,
        include_tax_detail=include_tax_detail,
    )


def min_cut_to_cap(mv: float, total: float, cap_pct: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, mv - total * cap_pct / 100.0)


def snapshot_dict(holdings, metrics: dict, as_of: datetime) -> dict:
    return {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "date": as_of.strftime("%Y-%m-%d"),
        "grand_total": metrics["grand_total"],
        "cluster_w": metrics["cluster_w"],
        "broad_w": metrics["broad_w"],
        "cash_w": metrics["cash_w"],
        "top5_w": metrics["top5_w"],
        "breaches": metrics.get("breaches") or [],
        "holdings": [
            {
                "symbol": h["symbol"],
                "weight": h["weight"],
                "market_value": h["market_value"],
                "price": h["price"],
            }
            for h in holdings
            if h.get("symbol")
        ],
    }


def save_weights_snapshot(snap: dict, as_of: datetime) -> Path:
    BRIEFINGS_DIR.mkdir(exist_ok=True)
    dated = BRIEFINGS_DIR / f"weights_{as_of.strftime('%Y-%m-%d')}.json"
    latest = BRIEFINGS_DIR / "weights_latest.json"
    text = json.dumps(snap, indent=2)
    atomic_write_text(dated, text)
    atomic_write_text(latest, text)
    return dated


def _account_ref(result: dict) -> str:
    """Stable non-secret account reference for local snapshot comparisons."""
    if result.get("account_ref"):
        return str(result["account_ref"])
    key = str(result.get("account_id_key") or result.get("label") or "unknown")
    return sha256(key.encode("utf-8")).hexdigest()[:12]


def _snapshot_date(value) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if value:
        return str(value)
    return None


def observation_snapshot_dict(
    results,
    harvest_reviews: list[dict],
    breaches: list[dict],
    as_of: datetime,
) -> dict:
    """Build per-account position state for next-run execution reconciliation."""
    positions = []
    for result in results or []:
        if result.get("error"):
            continue
        account_ref = _account_ref(result)
        account = account_tail(result.get("label") or "") or "account"
        tax_bucket = result.get("tax_bucket") or "taxable"
        for holding in result.get("holdings") or []:
            symbol = holding.get("symbol") or ""
            if not symbol:
                continue
            lots = []
            for lot in holding.get("lots") or []:
                lots.append(
                    {
                        "quantity": lot.get("qty") or 0.0,
                        "acquired": _snapshot_date(lot.get("acquired")),
                        "term_code": lot.get("term_code"),
                        "total_cost": lot.get("total_cost") or 0.0,
                    }
                )
            lots.sort(
                key=lambda lot: (
                    lot.get("acquired") or "",
                    lot.get("quantity") or 0.0,
                    lot.get("total_cost") or 0.0,
                )
            )
            positions.append(
                {
                    "account_ref": account_ref,
                    "account": account,
                    "tax_bucket": tax_bucket,
                    "position_id": str(holding.get("position_id") or ""),
                    "symbol": symbol,
                    "quantity": holding.get("quantity") or 0.0,
                    "price": holding.get("price") or 0.0,
                    "market_value": holding.get("market_value") or 0.0,
                    "total_cost": holding.get("total_cost"),
                    "total_gain": holding.get("total_gain"),
                    "lots": lots,
                }
            )
    positions.sort(key=lambda row: (row["account_ref"], row["symbol"]))
    reviews = [
        {
            "kind": "OPEN_TLH_REVIEW",
            "symbol": row["symbol"],
            "account": row["account"],
        }
        for row in harvest_reviews
    ]
    reviews.extend(
        {
            "kind": "CONCENTRATION_REVIEW",
            "symbol": holding["symbol"],
            "account": "consolidated",
        }
        for holding in breaches
    )
    reviews.sort(key=lambda row: (row["kind"], row["symbol"], row["account"]))
    return {
        "schema_version": 1,
        "as_of": as_of.isoformat(),
        "date": as_of.strftime("%Y-%m-%d"),
        "positions": positions,
        "reviews": reviews,
    }


def save_observation_snapshot(snap: dict, as_of: datetime) -> Path:
    BRIEFINGS_DIR.mkdir(exist_ok=True)
    dated = BRIEFINGS_DIR / f"observations_{as_of.strftime('%Y-%m-%d')}.json"
    latest = BRIEFINGS_DIR / "observations_latest.json"
    text = json.dumps(snap, indent=2)
    atomic_write_text(dated, text)
    atomic_write_text(latest, text)
    return dated


def load_prior_observation(as_of: datetime) -> tuple[dict | None, str]:
    today = as_of.strftime("%Y-%m-%d")
    paths = sorted(BRIEFINGS_DIR.glob("observations_20*.json"), reverse=True)
    for path in paths:
        if today in path.name:
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8")), path.name
        except (OSError, json.JSONDecodeError):
            continue
    return None, ""


def format_observed_delta(today: dict, prior: dict | None, prior_label: str) -> str:
    """Describe observable position changes without inferring user intent."""
    if not prior:
        return (
            "- No prior observation snapshot yet. Quantity/lot account-change "
            "reconciliation begins on the next run."
        )

    label = prior.get("date") or prior.get("as_of") or prior_label
    lines = [f"Compared with **{label}** (`{prior_label}`):"]
    previous = {_position_key(row): row for row in prior.get("positions") or []}
    current = {_position_key(row): row for row in today.get("positions") or []}
    events = []
    for key in sorted(set(previous) | set(current)):
        old = previous.get(key)
        new = current.get(key)
        row = new or old or {}
        symbol = row.get("symbol") or "?"
        account = row.get("account") or "account"
        if is_cash_symbol(symbol):
            continue
        if old is None:
            events.append(
                f"- **POSITION_APPEARED:** {symbol} / {account}, "
                f"qty {float(new.get('quantity') or 0.0):,.4g}. Possible buy, "
                "transfer, or corporate action; execution is not confirmed."
            )
            continue
        if new is None:
            events.append(
                f"- **POSITION_DISAPPEARED:** {symbol} / {account}, prior qty "
                f"{float(old.get('quantity') or 0.0):,.4g}. Possible full sale, "
                "transfer, or corporate action; execution is not confirmed."
            )
            continue
        old_qty = float(old.get("quantity") or 0.0)
        new_qty = float(new.get("quantity") or 0.0)
        tolerance = max(1e-6, abs(old_qty) * 1e-8)
        if new_qty > old_qty + tolerance:
            events.append(
                f"- **QUANTITY_INCREASE:** {symbol} / {account}, "
                f"qty {old_qty:,.4g} → {new_qty:,.4g}. Possible buy, transfer, "
                "or corporate action; execution is not confirmed."
            )
        elif new_qty < old_qty - tolerance:
            events.append(
                f"- **QUANTITY_DECREASE:** {symbol} / {account}, "
                f"qty {old_qty:,.4g} → {new_qty:,.4g}. Possible sale, transfer, "
                "or corporate action; execution is not confirmed."
            )
        elif _lot_identity(old) != _lot_identity(new):
            events.append(
                f"- **LOT_IDENTITY_CHANGE:** {symbol} / {account} with unchanged "
                "net quantity. Treat as review-required, not a confirmed trade."
            )
    if events:
        lines.extend(events)
    else:
        lines.append(
            "- No quantity or lot-identity change detected. This means **NO "
            "EXECUTION DETECTED**; it does not establish approval, rejection, or intent."
        )

    prior_reviews = {
        (row.get("kind"), row.get("symbol"), row.get("account"))
        for row in prior.get("reviews") or []
    }
    today_reviews = {
        (row.get("kind"), row.get("symbol"), row.get("account"))
        for row in today.get("reviews") or []
    }
    for kind, symbol, account in sorted(today_reviews - prior_reviews):
        lines.append(f"- **Review opened:** {symbol} / {account} — {kind}.")
    for kind, symbol, account in sorted(prior_reviews - today_reviews):
        lines.append(
            f"- **Review cleared by observable data:** {symbol} / {account} — "
            f"{kind}. Do not infer why."
        )
    return "\n".join(lines)


_HOLDING_LINE_RE = re.compile(
    r"^- ([A-Z][A-Z0-9.]*)(?: \([^)]+\))? ([\d.]+)%\s+"
    r"\(\$?([0-9,.]+(?:M)?) @ \$([\d.]+)"
)


def _parse_money_token(s: str) -> float:
    s = s.replace(",", "")
    if s.endswith("M"):
        return float(s[:-1]) * 1_000_000
    return float(s)


def parse_snapshot_from_prompt(text: str) -> dict | None:
    holdings = []
    in_holdings = False
    for line in text.splitlines():
        if line.startswith("**Consolidated holdings"):
            in_holdings = True
            continue
        if in_holdings:
            if line.startswith("**") or line.startswith("# "):
                break
            m = _HOLDING_LINE_RE.match(line)
            if m:
                holdings.append(
                    {
                        "symbol": m.group(1),
                        "weight": float(m.group(2)),
                        "market_value": _parse_money_token(m.group(3)),
                        "price": float(m.group(4)),
                    }
                )
    if not holdings:
        return None

    def _pct(patterns) -> float | None:
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return float(m.group(1))
        return None

    cluster_w = _pct(
        [
            r"Direct AI/semi[^\n]*?\*\*([\d.]+)%\*\*",
            r"pure AI/semi cluster[^\n]*?([\d.]+)%",
        ]
    )
    broad_w = _pct([r"Broad AI-cycle liquid[^\n]*?\*\*([\d.]+)%\*\*"])
    cash_w = _pct(
        [
            r"Cash \+ cash equivalents:[^\n]*?\(([\d.]+)%\)",
            r"Cash \+ cash equivalents[^\n]*?\(([\d.]+)%\)",
            r"Cash \(SGOV\):[^\n]*?\(([\d.]+)%\)",
            r"Cash \(SGOV\)[^\n]*?~([\d.]+)%",
        ]
    )
    top5_w = _pct([r"Top-5[^\n]*?([\d.]+)%"])
    return {
        "as_of": "prior briefing",
        "date": "",
        "cluster_w": cluster_w,
        "broad_w": broad_w,
        "cash_w": cash_w,
        "top5_w": top5_w,
        "holdings": holdings,
        "breaches": [
            h["symbol"]
            for h in holdings
            if h["weight"] > SINGLE_NAME_CAP and not is_cash_symbol(h["symbol"])
        ],
    }


def load_prior_snapshot(as_of: datetime) -> tuple[dict | None, str]:
    today = as_of.strftime("%Y-%m-%d")
    json_files = sorted(BRIEFINGS_DIR.glob("weights_20*.json"), reverse=True)
    for path in json_files:
        if today in path.name:
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8")), path.name
        except (OSError, json.JSONDecodeError):
            continue

    prompt_files = sorted(OUT_DIR.glob("daily_briefing_prompt_20*.md"), reverse=True)
    for path in prompt_files:
        if today in path.name:
            continue
        try:
            parsed = parse_snapshot_from_prompt(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if parsed:
            parsed["date"] = path.stem.replace("daily_briefing_prompt_", "")
            parsed["as_of"] = parsed["date"]
            return parsed, path.name
    return None, ""


def format_daily_delta(today_snap: dict, prior: dict | None, prior_label: str) -> str:
    if not prior:
        return (
            "_No prior snapshot yet. Tomorrow’s run will show weight/limit "
            "deltas. Unchanged analysis should not be repeated._"
        )
    label = prior.get("date") or prior.get("as_of") or prior_label
    lines = [f"Compared with **{label}** (`{prior_label}`). Only material moves:"]

    prior_h = {h["symbol"]: h for h in prior.get("holdings") or []}
    today_h = {h["symbol"]: h for h in today_snap.get("holdings") or []}
    material = []
    for sym in sorted(set(prior_h) | set(today_h)):
        if is_cash_symbol(sym):
            continue
        pw = (prior_h.get(sym) or {}).get("weight")
        tw = (today_h.get(sym) or {}).get("weight")
        if pw is None:
            material.append(f"- **{sym}** new today at {fmt_weight(tw)}")
        elif tw is None:
            material.append(f"- **{sym}** exited (was {fmt_weight(pw)})")
        elif abs(tw - pw) >= 0.5:
            sign = "+" if tw > pw else ""
            material.append(
                f"- **{sym}** {fmt_weight(pw)} → {fmt_weight(tw)} ({sign}{tw - pw:.1f} pp)"
            )
    if material:
        lines.extend(material)
    else:
        lines.append("- No single-name weight change ≥ 0.5 pp.")

    def _metric(key, title):
        a, b = prior.get(key), today_snap.get(key)
        if a is None or b is None:
            return None
        if abs(b - a) < 0.15:
            return None
        sign = "+" if b > a else ""
        return f"- {title}: {a:.1f}% → {b:.1f}% ({sign}{b - a:.1f} pp)"

    for key, title in (
        ("cluster_w", "Direct AI/semi"),
        ("broad_w", "Broad AI-cycle liquid"),
        ("cash_w", "Cash"),
        ("top5_w", "Top-5"),
    ):
        row = _metric(key, title)
        if row:
            lines.append(row)

    prior_br = set(prior.get("breaches") or [])
    today_br = set(today_snap.get("breaches") or [])
    if today_br - prior_br:
        lines.append(
            f"- **New {SINGLE_NAME_CAP:g}% breach:** " + ", ".join(sorted(today_br - prior_br))
        )
    if prior_br - today_br:
        lines.append(
            f"- **{SINGLE_NAME_CAP:g}% breach cleared:** " + ", ".join(sorted(prior_br - today_br))
        )
    if today_br and today_br == prior_br:
        lines.append(f"- {SINGLE_NAME_CAP:g}% breach **unchanged:** " + ", ".join(sorted(today_br)))
    lines.append(
        "Treat these as market-move vs thesis-move in Daily Delta. "
        "Do not repeat unchanged analysis."
    )
    return "\n".join(lines)


def format_constraint_math(holdings, grand_total: float, cluster_mv: float) -> str:
    lines = []
    for h in holdings:
        if is_cash_symbol(h["symbol"]) or h["weight"] <= SINGLE_NAME_CAP:
            continue
        cut = min_cut_to_cap(h["market_value"], grand_total, SINGLE_NAME_CAP)
        new_w = (h["market_value"] - cut) / grand_total * 100 if grand_total else 0
        lines.append(
            f"- **{h['symbol']}** {fmt_weight(h['weight'])}: "
            f"min **{fmt_money(cut)}** to restore {SINGLE_NAME_CAP:g}% "
            f"(→ {fmt_weight(new_w)}). Larger is optional."
        )
    c40 = min_cut_to_cap(cluster_mv, grand_total, CLUSTER_SOFT_CAP)
    c38 = min_cut_to_cap(cluster_mv, grand_total, CLUSTER_NO_ADD)
    if c40 > 0:
        lines.append(
            f"- Direct AI/semi: **{fmt_money(c40)}** to {CLUSTER_SOFT_CAP:g}%; "
            f"**{fmt_money(c38)}** to {CLUSTER_NO_ADD:g}%. Hold-above OK if only "
            "lots are punitive ST; do not add."
        )
    if not lines:
        return "- No single-name or cluster cap currently requires a sale."
    return "\n".join(lines)


def format_marginal_10k(
    holdings,
    grand_total: float,
    cluster_mv: float,
    broad_mv: float,
    cash_mv: float,
    results=None,
) -> str:
    """Show an illustrative $10k exchange only when one account can fund it.

    The briefing is consolidated, but cash is not fungible across accounts.
    Account-level source results are required before a purchase is illustrated.
    """
    if grand_total <= 0:
        return "_Total unavailable._"
    amount = 10_000.0
    unit = amount / grand_total * 100
    cluster_ok = cluster_mv / grand_total * 100 < CLUSTER_NO_ADD

    def source_from_account(account_results):
        candidates = []
        for result in account_results or []:
            if result.get("error"):
                continue
            for holding in result.get("holdings") or []:
                if is_cash_symbol(holding.get("symbol") or "") and (
                    holding.get("market_value") or 0.0
                ) >= amount:
                    candidates.append((holding, result.get("label") or "account"))
        # Prefer SGOV when it is available, then any eligible cash equivalent.
        return next(
            ((holding, label) for holding, label in candidates if holding["symbol"] == "SGOV"),
            candidates[0] if candidates else None,
        )

    source = source_from_account(results)

    if source is None:
        return (
            f"_No single account has a verified {fmt_money(amount)} cash-equivalent "
            "source. The $10k purchase examples are unavailable: do not combine cash "
            "across accounts or assume unsettled cash can fund a trade._"
        )

    source_holding, source_account = source
    source_symbol = source_holding["symbol"]

    def top5_weight_after(destination_symbol: str | None) -> float:
        values = {h["symbol"]: h["market_value"] for h in holdings}
        values[source_symbol] = values.get(source_symbol, 0.0) - amount
        destination_key = destination_symbol or "__NEW_NON_AI_DIVERSIFIER__"
        values[destination_key] = values.get(destination_key, 0.0) + amount
        return sum(sorted(values.values(), reverse=True)[:5]) / grand_total * 100

    current_top5_w = sum(sorted((h["market_value"] for h in holdings), reverse=True)[:5])
    current_top5_w = current_top5_w / grand_total * 100

    def row(label, d_direct, d_broad, d_cash, d_top5, note):
        def fmt(x):
            if abs(x) < 0.05:
                return "0"
            return f"{x:+.2f} pp"

        return (
            f"| {label} | {fmt(d_direct)} | {fmt(d_broad)} | "
            f"{fmt(d_cash)} | {fmt(d_top5)} | {note} |"
        )

    rows = [
        (
            f"| Deploy {fmt_money(amount)} from {source_symbol} into | Δ direct | Δ broad | "
            "Δ cash | Δ top-5 | Note |"
        ),
        "|---|---:|---:|---:|---:|---|",
    ]
    specs = list(MARGINAL_EXAMPLES)
    for label, sym, note in specs:
        d_direct = unit if sym in AI_SEMI_CLUSTER else 0.0
        d_broad = unit if sym in BROAD_AI_LIQUID else 0.0
        d_top5 = top5_weight_after(sym) - current_top5_w
        extra = note
        if d_direct and not cluster_ok:
            extra += f" — **do not add** while direct ≥ {CLUSTER_NO_ADD:g}%"
        rows.append(row(label, d_direct, d_broad, -unit, d_top5, extra))

    overweight = next(
        (
            h
            for h in holdings
            if not is_cash_symbol(h["symbol"])
            and h["market_value"] / grand_total * 100 > SINGLE_NAME_CAP
        ),
        None,
    )
    if overweight:
        symbol = overweight["symbol"]
        current_weight = overweight["market_value"] / grand_total * 100
        cut_to_cap = min_cut_to_cap(overweight["market_value"], grand_total, SINGLE_NAME_CAP)
        direct_delta = -unit if symbol in AI_SEMI_CLUSTER else 0.0
        broad_delta = -unit if symbol in BROAD_AI_LIQUID else 0.0
        post_sale_values = {
            h["symbol"]: h["market_value"] - (amount if h["symbol"] == symbol else 0.0)
            for h in holdings
        }
        top5_delta = (
            sum(sorted(post_sale_values.values(), reverse=True)[:5]) / grand_total * 100
            - current_top5_w
        )
        rows.extend(
            [
                "",
                (
                    f"Sell $10k **{symbol}** → cash: Δ direct {direct_delta:+.2f} pp, "
                    f"Δ broad {broad_delta:+.2f}, Δ cash {unit:+.2f}, "
                    f"Δ top-5 {top5_delta:+.2f}. {symbol} "
                    f"{fmt_weight(current_weight)} → "
                    f"{fmt_weight(current_weight - unit)}. "
                    f"Minimum to restore {SINGLE_NAME_CAP:g}% is "
                    f"{fmt_money(cut_to_cap)}."
                ),
            ]
        )
    rows.append(
        f"$10k = **{unit:.2f} pp** of liquid ({fmt_money(grand_total)}). "
        "Top-5 is re-ranked after each exchange. "
        f"Funding account: **{source_account}**."
    )
    return "\n".join(rows)


def _snapshot_coverage(as_of: datetime, prior: dict | None, prior_label: str) -> str:
    if not prior:
        return "No earlier snapshot is available."
    raw_date = prior.get("date") or prior.get("as_of")
    if not raw_date:
        return f"Previous snapshot: {prior_label}."
    try:
        prior_date = datetime.fromisoformat(str(raw_date)).date()
    except ValueError:
        return f"Previous snapshot: {raw_date} ({prior_label})."
    gap = (as_of.date() - prior_date).days
    if gap > 1:
        return (
            f"Previous snapshot: {prior_date.isoformat()} ({gap} calendar days earlier). "
            "No snapshots were recorded for the intervening dates; changes are cumulative."
        )
    return f"Previous snapshot: {prior_date.isoformat()} ({prior_label})."


def build_briefing(
    holdings: list[dict],
    accounts: list[dict],
    grand_total: float,
    as_of: datetime,
    portfolio_block: str,
    results=None,
) -> BriefingBuildResult:
    context, context_status = load_portfolio_context()
    as_of_str = as_of.strftime("%Y-%m-%d %H:%M")
    direct_members = "+".join(AI_SEMI_CLUSTER)
    broad_members = "+".join(BROAD_AI_LIQUID)
    broad_sleeve_label = broad_ai_sleeve_label()
    crypto_label = sleeve_member_text(CRYPTO_CLUSTER)
    payments_label = sleeve_member_text(PAYMENTS_CLUSTER)
    index_label = sleeve_member_text(BROAD_INDEX)
    cluster_mv, cluster_w, _ = sleeve_stats(holdings, AI_SEMI_CLUSTER, grand_total)
    broad_mv, broad_w, _ = sleeve_stats(holdings, BROAD_AI_LIQUID, grand_total)
    crypto_mv, crypto_w, _ = sleeve_stats(holdings, CRYPTO_CLUSTER, grand_total)
    pay_mv, pay_w, _ = sleeve_stats(holdings, PAYMENTS_CLUSTER, grand_total)
    voo_mv, voo_w, _ = sleeve_stats(holdings, BROAD_INDEX, grand_total)

    cash_mv = sum(h["market_value"] for h in holdings if is_cash_symbol(h["symbol"]))
    cash_w = (cash_mv / grand_total * 100) if grand_total else 0.0
    cash_components = ", ".join(
        f"{h['symbol']} {fmt_money(h['market_value'])}"
        for h in holdings
        if is_cash_symbol(h["symbol"])
    ) or "none"

    top5 = holdings[:5]
    top5_w = sum(h["weight"] for h in top5)

    breaches = [
        h for h in holdings if h["weight"] > SINGLE_NAME_CAP and not is_cash_symbol(h["symbol"])
    ]

    cash_by_account = {}
    for result in results or []:
        if result.get("error"):
            continue
        cash_by_account[result.get("label")] = sum(
            holding.get("market_value") or 0.0
            for holding in result.get("holdings") or []
            if is_cash_symbol(holding.get("symbol") or "")
        )

    account_bits = []
    for a in accounts:
        if (a.get("total") or 0) < 1:
            continue
        label = a.get("label") or ""
        name = label.split(" / ")[0].split(" (")[0]
        tail = account_tail(label)
        loc = a.get("tax_bucket") or "taxable"
        loc_note = ""
        if loc == "taxable" and "IRA" not in name.upper() and "ROTH" not in name.upper():
            loc_note = " (taxable)"
        elif loc == "traditional" and "IRA" not in name.upper():
            loc_note = " (IRA)"
        elif loc == "roth" and "ROTH" not in name.upper():
            loc_note = " (Roth)"
        shown = f"{name} {tail}" if tail and tail not in name else name
        cash_note = ""
        if label in cash_by_account:
            cash_note = f"; cash equivalents ≈ {fmt_money(cash_by_account[label])}"
        account_bits.append(f"  - {shown}{loc_note}: ≈ {fmt_money(a['total'])}{cash_note}")
    account_lines = "\n".join(account_bits) if account_bits else "  - _No funded accounts._"

    holdings_lines = "\n".join(
        holding_line(h, as_of) for h in holdings if h["market_value"] >= 50
    )
    harvest_reviews = taxable_harvest_reviews(results)
    harvest_symbols = {row["symbol"] for row in harvest_reviews}
    analyze_rows = must_analyze_holdings(holdings, harvest_symbols)
    must_analyze_block = format_must_analyze(analyze_rows)
    observable_review_block = format_observable_reviews(harvest_reviews, breaches)

    if cluster_w >= CLUSTER_SOFT_CAP:
        cluster_status = (
            f"ABOVE {CLUSTER_SOFT_CAP:g}% ceiling — do not add; hold-above only "
            "if cutting is punitive ST or no superior immediate deployment exists"
        )
    elif cluster_w >= CLUSTER_NO_ADD:
        cluster_status = f"at {CLUSTER_NO_ADD:g}% do-not-increase — do not add"
    else:
        cluster_status = "within ceiling"

    tax_table = (
        format_lot_table(results, as_of)
        if results is not None
        else "_Lot table unavailable this run._"
    )
    tax_flag_lines = collect_tax_flags(results, as_of) if results is not None else []
    tax_flags = "\n".join(tax_flag_lines)
    tax_flag_block = f"\n**Tax flags:**\n{tax_flags}\n" if tax_flags else ""

    today_snap = snapshot_dict(
        holdings,
        {
            "grand_total": grand_total,
            "cluster_w": cluster_w,
            "broad_w": broad_w,
            "cash_w": cash_w,
            "top5_w": top5_w,
            "breaches": [h["symbol"] for h in breaches],
        },
        as_of,
    )
    prior_snap, prior_label = load_prior_snapshot(as_of)
    daily_delta = format_daily_delta(today_snap, prior_snap, prior_label)
    today_observation = observation_snapshot_dict(results, harvest_reviews, breaches, as_of)
    prior_observation, prior_observation_label = load_prior_observation(as_of)
    observed_delta = format_observed_delta(
        today_observation, prior_observation, prior_observation_label
    )
    output_policy = briefing_output_policy(
        holdings,
        harvest_reviews,
        breaches,
        today_snap,
        prior_snap,
        today_observation,
        prior_observation,
        as_of,
    )
    snapshot_coverage = _snapshot_coverage(as_of, prior_snap, prior_label)
    constraint_math = format_constraint_math(holdings, grand_total, cluster_mv)
    marginal_10k = format_marginal_10k(
        holdings, grand_total, cluster_mv, broad_mv, cash_mv, results=results
    )
    owner_profile = format_owner_profile(context)
    research_records = format_research_records(context)
    decision_history = format_decision_history(context)
    focus_symbols = ", ".join(output_policy.material_symbols) or "none"
    tax_detail = (
        f"{tax_table}{tax_flag_block}"
        if output_policy.include_tax_detail
        else "_Exact lots remain in the source data. Do not propose a tax-sensitive action without naming the account and lot._"
    )
    marginal_detail = (
        marginal_10k
        if output_policy.refresh_candidates
        else "_Recalculate the $10k example only if proposing DEPLOY or REPLACE._"
    )
    if output_policy.refresh_candidates:
        candidate_instruction = (
            "Refresh the candidate list. Include at most two non-held candidates. "
            "Use NO ACTION / WATCH unless the trade bar is met."
        )
        candidate_output = (
            "Include at most two non-held candidates. For each, give role, dated evidence, "
            "entry condition, principal risk, and why it improves the portfolio."
        )
    else:
        candidate_instruction = (
            "Do not generate new tickers today. Recheck the existing candidates in the "
            "supplied research context and report only a changed trigger or thesis."
        )
        candidate_output = (
            "Report existing candidates only when their trigger or thesis changed. "
            "Write 'No candidate change' otherwise."
        )

    prompt = f"""You are a portfolio decision-support analyst. Analyze the whole portfolio.

This is an analytical proposal for human review. It is not an approval, order, execution record, or instruction to trade. Use only supplied values and dated sources. Never invent a price, lot, tax result, approval state, or execution. If a material fact is missing, use **NO ACTION / NEEDS REVIEW**.

Allowed Portfolio Action values are **KEEP / REDUCE / REPLACE / DEPLOY / NO ACTION**. A proposed sale must name the ticker, account, lot, quantity, dollars, proceeds destination, and decision trigger.

**Output mode:** {output_policy.mode}. **Decision focus:** {focus_symbols}. **Candidate refresh:** {"yes" if output_policy.refresh_candidates else "no"}.

Title: **Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

---

# 1. Live book

**As of:** {as_of_str} {as_of.tzname() or "ET"}
**Snapshot coverage:** {snapshot_coverage}
**E*TRADE total ≈ {fmt_money(grand_total)}**
**Top-5 (E*TRADE):** {fmt_weight(top5_w)} · **Cash + cash equivalents:** {fmt_money(cash_mv)} ({fmt_weight(cash_w)})
**Cash composition:** {cash_components}

**Daily delta:**
{daily_delta}

**Observed account delta** (E*TRADE position evidence only):
{observed_delta}

**Observable review state** (generated from E*TRADE; evidence, not orders):
{observable_review_block}

Review flags and position changes do not establish intent, approval, rejection, or execution.

**Factor sleeves (do not invent %):**
- Direct AI/semi ({direct_members}): **{fmt_weight(cluster_w)}** / {fmt_money(cluster_mv)} — {cluster_status}
- Broad AI-cycle liquid ({broad_sleeve_label}): **{fmt_weight(broad_w)}** / {fmt_money(broad_mv)} (direct is a lower bound on cycle risk)
- Crypto ({crypto_label}): {fmt_weight(crypto_w)} / {fmt_money(crypto_mv)}
- Payments ({payments_label}): {fmt_weight(pay_w)} / {fmt_money(pay_mv)}
- Broad index ({index_label}): {fmt_weight(voo_w)} / {fmt_money(voo_mv)}

**Constraint math (use these dollars):**
{constraint_math}

**Marginal $10k from SGOV:**
{marginal_detail}

**Accounts:**
{account_lines}

**Funding boundary.** Account totals are not buying power. Every DEPLOY or REPLACE must name the funding account, settled source cash or security, amount, and purchasing account. Never combine cash across accounts.

**Owner profile** (user-maintained; never infer blanks):
{owner_profile}

**Research continuity** (user-maintained; use its sources and dates, then refresh only what may be stale):
{context_status}
{research_records}

**Decision history** (analytical proposals, not trade authority):
{decision_history}

**Portfolio holdings.** Use this source table for weights and calculations. Do not repeat the full table in the answer.

{holdings_lines}

**Decision focus.** Start with these names. Include another name only when new evidence or an account change makes it material.
{must_analyze_block}

**Lots** (source of truth). Avg = cost/share. Cost = dollars in. P/L = unrealized. Taxable ST = held ≤ 1 year; LT = held > 1 year. IRA/Roth = economic P/L only — not a CG event. Same ticker in two accounts = two decision buckets.

{tax_table}
{tax_flag_block}
---

# 2. Rules

**Limits.** Single-name soft max is **{SINGLE_NAME_CAP:g}%**. Direct AI/semi is no-add at **{CLUSTER_NO_ADD:g}%** and has a soft ceiling of **{CLUSTER_SOFT_CAP:g}%**. A soft breach creates review and no-add, not an automatic sale. Use the pre-computed minimum cut. Risk-Off means work toward {RISK_OFF_GLIDE:g}% with tax-aware lots.

**Authority.** E*TRADE data establishes observed positions and observable changes. It does not establish intent or approval. A prior model proposal is historical context. **NO EXECUTION DETECTED** is not approval, rejection, or deferral.

**Decision continuity.** This is a continuing portfolio review, not a fresh stock screen. Use the supplied Decision history table as the only durable proposal record. For every prior model proposal there, classify today's proposal as **UNCHANGED / MODIFIED / REVERSED / RESOLVED**. Otherwise say **NOT CAPTURED**; do not reconstruct it from position data, account changes, or snapshots.

A MODIFIED or REVERSED proposal requires at least one qualifying delta: material company-specific evidence; earnings/guidance/regulatory/competitive change; price or valuation movement material to the original thesis; portfolio-weight/factor/liquidity/tax change; observed execution; or a specific error in the prior analysis. State the prior proposal, new proposal, dated new fact, invalidated assumption, and why the change is sufficient. “Reassessment,” “updated outlook,” “fresh analysis,” and “greater upside” are not sufficient.

**View versus action.** Report Fundamental View as **ATTRACTIVE / NEUTRAL / UNATTRACTIVE / INSUFFICIENT EVIDENCE**. Report Portfolio Action separately. **KEEP** requires dated evidence that the thesis is supported. Missing or stale research means **NO ACTION / RESEARCH INCOMPLETE**.

**Owner profile and hard limits.** Use only supplied profile fields. Treat omitted fields as **UNKNOWN**. Do not infer a risk budget from the current holdings; when a missing item could change an action, use **NO ACTION / NEEDS REVIEW** and identify it.

**Tax.** IRA and Roth gains are economic P/L, not capital gains. A taxable loss is a review, not a sale. Wash-sale status is **CLEAR / POSSIBLE / UNKNOWN** and must name the scope checked. Missing spouse, automatic-purchase, options, open-order, or 61-day data means **NEEDS TAX REVIEW**. Before a taxable-loss sale, state the next 30-day restriction. A backward-looking CLEAR status does not authorize a sale or replacement.

**Hierarchy.** (1) hard concentration / drawdown that makes de-risking valuable even in cash (2) do not tidy a working thesis for a soft breach without a superior deployment (3) risk-adjusted return after the destination is included (4) tax/friction (5) deploy cash only if return beats liquidity. A hard #1 can require REDUCE to cash; a soft threshold alone cannot. #4 beats incremental #3.

**Trade bar.** Prefer **NO ACTION** when evidence, valuation, tax, or implementation is incomplete. **DEPLOY** requires account-specific funding, supported valuation, portfolio fit, and a better use than holding SGOV. **REPLACE** requires a named sale and superior named purchase. **REDUCE** requires hard risk, a broken thesis, unacceptable downside, or valuation that no longer pays for risk and tax. A soft concentration breach alone is not enough.

**Deployment preference.** Do not REDUCE an intact holding merely to create idle cash. Name the immediate destination or state the hard-risk reason cash is better. Use **KEEP** when the thesis is supported and no better deployment exists. Use **NO ACTION** when evidence or implementation is incomplete.

Evaluate the risk of continuing to hold as explicitly as the risk of trading. Owner preferences constrain recommendations; they are not evidence that a holding is attractive. If a preference limits risk reduction, explain the consequence.

**Sizing.** Size only after REDUCE or REPLACE clears the trade bar. Use the pre-computed minimum that fixes the stated problem. A breach or profit alone does not justify a sale.

**Factor.** Ticker diversity is not factor diversity. Broad members ({broad_members}) share the AI-capex cycle. Sleeve labels are lower bounds and omit look-through. Explain why a candidate improves the portfolio and compare it with the best relevant holding.

**REPLACE** names the sale and buy. Moving to cash is REDUCE; investing later is DEPLOY. State risk removed, risk added, tax status, and why the change beats holding.

**Thesis vs price.** Score thesis · valuation · trend · catalyst separately. Price is a trigger, not a thesis — classify moves as market / factor / company / execution / noise. Use a decision trigger, not a vague standalone “hold 6–12 months” horizon.

**Decision quality.** For an actionable proposal, show dated evidence, the changed risk or expectation, valuation, advantage over holding, best alternative, account-specific implementation, strongest counterargument, and invalidation trigger. Keep reporting period, publication date, quote time, and snapshot time separate. Label fragile conclusions. Do not invent precision.

**Tax-loss proposals.** A REDUCE or REPLACE proposal for tax loss must name the taxable account, lot, quantity, expected loss, wash-sale scope, replacement or cash destination, and why the after-tax benefit beats friction and opportunity cost. A usable loss alone does not clear the trade bar.

**Evidence.** Any company-specific fact that initiates, modifies, or reverses an action needs a source and event/publication date. Prefer filings, earnings releases, transcripts, and regulator/company sources. Label unsourced claims and forecasts as assumptions. If current evidence cannot be verified, use INSUFFICIENT EVIDENCE and do not reverse a prior proposal on that basis.

Keep the portfolio snapshot time, quote/session time, financial reporting period, and news publication/event date distinct. Label stale, mixed-session, or unreconciled inputs. A daily headline is not material unless it changes valuation, thesis, risk budget, deployability, or a decision trigger.

**Candidates.** {candidate_instruction} Research conviction is separate from conviction to deploy. Do not invent live prices or valuation facts.

# 3. Tax detail

{tax_detail}

# 4. Output, in this order

**Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

**0. Decision card.** State the overall action, what changed, the largest unresolved risk, the best opportunity if any, and the blocker preventing action. Use no more than five sentences. Include **Analytical proposal — not an approved or submitted trade.**

**1. Material changes.** Report at most three changes from the prior snapshot or observation. Include weight, quantity, account, lot, sleeve, cash, or review changes only when material. Say **No material change** when none exists. Never infer a trade or intent from a position change alone.

**2. Risk and action table.** Cover only the Decision focus names. Use columns **Ticker, Fundamental View, Portfolio Action, Reason, Evidence, Missing fact, Trigger**. Add exact lot and account details only for REDUCE or REPLACE.

**3. Candidate review.** {candidate_output}

**4. Implementation.** Include funding account, settled source, purchasing account, exact lots, quantity, tax, wash-sale scope, proceeds destination, and post-trade weights only when proposing REDUCE, REPLACE, or DEPLOY. Otherwise write **No implementation proposed**.

**5. Decision triggers.** List only triggers for the Decision focus names or candidate names. Use one line per trigger.

Do not repeat the full holdings table, unchanged names, or the safety rules. Keep a routine-day answer under 1,000 words. Expand only when an action, tax review, account change, or material new evidence requires it.
"""
    return BriefingBuildResult(
        prompt=prompt.strip() + "\n",
        weights_snapshot=today_snap,
        observation_snapshot=today_observation,
    )


def build_prompt(
    holdings: list[dict],
    accounts: list[dict],
    grand_total: float,
    as_of: datetime,
    portfolio_block: str,
    results=None,
) -> str:
    """Compatibility wrapper for callers that only need prompt text."""
    return build_briefing(
        holdings, accounts, grand_total, as_of, portfolio_block, results=results
    ).prompt


def save_briefing_result(
    result: BriefingBuildResult, portfolio_block: str, as_of: datetime
) -> tuple[Path, Path]:
    """Persist a fully built briefing. Snapshot writes happen last."""
    save_portfolio_block(portfolio_block, as_of=as_of)
    OUT_DIR.mkdir(exist_ok=True)
    date_str = as_of.strftime("%Y-%m-%d")
    dated = OUT_DIR / f"daily_briefing_prompt_{date_str}.md"
    latest = OUT_DIR / "daily_briefing_prompt_latest.md"
    atomic_write_text(dated, result.prompt)
    atomic_write_text(latest, result.prompt)
    save_weights_snapshot(result.weights_snapshot, as_of)
    save_observation_snapshot(result.observation_snapshot, as_of)
    return dated, latest


def main(argv=None) -> int:
    parser = ArgumentParser(description="Build a portfolio decision-support prompt.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Generate an unsafe marked prompt when an account fails.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--from-clipboard",
        action="store_true",
        help="Build offline from the portable payload on the Windows clipboard.",
    )
    source.add_argument(
        "--input-file",
        help="Build offline from a portable payload file, or '-' for stdin.",
    )
    args = parser.parse_args(argv)
    if args.from_clipboard or args.input_file:
        if args.allow_partial:
            parser.error("--allow-partial is only available for live fetching")
        print("Loading portable portfolio without contacting E*TRADE...")
        if args.from_clipboard:
            pasted = read_clipboard_text()
        elif args.input_file == "-":
            pasted = sys.stdin.read()
        else:
            pasted = Path(args.input_file).read_text(encoding="utf-8")
        formatted, grand_total, npos, results, as_of = parse_portable_portfolio_text(
            pasted
        )
    else:
        print("Fetching live E*TRADE portfolio...")
        formatted, grand_total, npos, results, as_of = fetch_portfolio_block(
            verbose=True, allow_partial=args.allow_partial
        )

    holdings, accounts = consolidate(results, grand_total)
    result = build_briefing(
        holdings, accounts, grand_total, as_of, formatted, results=results
    )
    dated, latest = save_briefing_result(result, formatted, as_of)

    clipped = copy_to_clipboard(result.prompt)

    print()
    print("=" * 60)
    print(f"Processed prompt written: {dated}")
    print(f"Also:                      {latest}")
    print(f"Total liquid:              ${grand_total:,.0f}")
    print(f"Positions:                 {npos}")
    if clipped:
        print(
            "Clipboard:                 FULL PROMPT copied — paste into your preferred model for analysis"
        )
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

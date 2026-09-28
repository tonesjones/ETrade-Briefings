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
from pathlib import Path

from briefing_formatting import fmt_money, fmt_weight
from briefing_snapshots import (  # noqa: F401 - re-exported for callers and tests
    _changed_observation_symbols,
    _changed_weight_symbols,
    _snapshot_coverage,
    format_daily_delta,
    format_observed_delta,
    load_prior_observation,
    load_prior_snapshot,
    observation_snapshot_dict,
    parse_snapshot_from_prompt,
    save_observation_snapshot,
    save_weights_snapshot,
    snapshot_dict,
)
from get_portfolio import (
    AuthExpiredError,
    account_tail,
    atomic_write_text,
    copy_to_clipboard,
    fetch_portfolio_block,
    fmt_signed_money,
    fmt_signed_pct,
    is_cash_symbol,
    lot_term_split,
    parse_portable_portfolio_text,
    read_clipboard_text,
    save_portfolio_block,
    term_from_lot,
)
from portfolio_policy import active_policy, load_policy, set_active_policy

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
    # Why each material symbol is in focus, and the sized names that are only monitored.
    focus_reasons: tuple[tuple[str, tuple[str, ...]], ...] = ()
    monitor_symbols: tuple[str, ...] = ()

# User-editable decision policy (portfolio_policy.json).


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
        ("Wash-sale scope", "wash_sale_scope"),
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


def format_context_block(context: dict) -> str:
    """Owner profile always; research notes and decision history only when supplied."""
    profile = context.get("owner_profile")
    filled = isinstance(profile, dict) and any(
        str(value).strip().upper() not in ("", "UNKNOWN") for value in profile.values()
    )
    if filled:
        parts = ["**Owner profile** (user-maintained; treat blanks as UNKNOWN):\n" + format_owner_profile(context)]
    else:
        parts = [
            "**Owner profile:** not supplied. Treat horizon, tax rate, liquidity needs, and hard "
            "limits as UNKNOWN; flag one only where it would change a REDUCE, REPLACE, or DEPLOY."
        ]
    if isinstance(context.get("research"), dict) and context["research"]:
        parts.append(
            f"**Your research notes** (updated {context.get('updated_at', 'date not recorded')}; "
            "start from these and refresh anything dated before the last snapshot):\n"
            + format_research_records(context)
        )
    if has_decision_history(context):
        parts.append(
            "**Decision history** (earlier analytical proposals, not trade authority):\n"
            + format_decision_history(context)
        )
    return "\n\n".join(parts)


def has_decision_history(context: dict) -> bool:
    history = context.get("decision_history")
    return isinstance(history, list) and any(isinstance(item, dict) for item in history)


def sleeve_member_text(symbols: tuple[str, ...]) -> str:
    return " ".join(symbols)


def broad_ai_sleeve_label() -> str:
    extras = [s for s in active_policy().sleeves["broad_ai_cycle"] if s not in active_policy().sleeves["direct_ai_semi"]]
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
                "alias": active_policy().aliases.get(sym),
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


def taxable_gain_by_term(results, as_of: datetime) -> dict[str, dict[str, float]]:
    """Unrealized taxable lot gain per symbol by term, plus value whose term is unknown.

    Lot gains are marked separately from the position, so LT + ST can differ slightly
    from the position P/L; that timing gap is not reported as an unknown term.
    """
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"LT": 0.0, "ST": 0.0, "unk_mv": 0.0})
    for result in results or []:
        if result.get("error") or (result.get("tax_bucket") or "taxable") != "taxable":
            continue
        for h in result.get("holdings") or []:
            symbol = h.get("symbol") or ""
            if not symbol or is_cash_symbol(symbol):
                continue
            split = lot_term_split(h, as_of)
            row = out[symbol]
            row["LT"] += split["lt_gain"]
            row["ST"] += split["st_gain"]
            row["unk_mv"] += split["unk_mv"]
    return out


def _held_in(h: dict) -> str:
    parts = [
        (name, h.get(key) or 0.0)
        for name, key in (("taxable", "taxable_mv"), ("IRA", "ira_mv"), ("Roth", "roth_mv"))
        if h.get(key)
    ]
    if len(parts) == 1:
        return parts[0][0]
    return " · ".join(f"{name} {fmt_money(mv)}" for name, mv in parts)


def format_holdings_table(holdings: list[dict], results, as_of: datetime) -> str:
    """One row per ticker: weight, value, P/L, account split, and taxable gain by term."""
    gains = taxable_gain_by_term(results, as_of)
    rows = []
    for h in holdings:
        symbol = h["symbol"]
        if is_cash_symbol(symbol) or h["market_value"] < 50:
            continue
        alias = f" ({h['alias']})" if h.get("alias") else ""
        tg, tgp = h.get("total_gain"), h.get("total_gain_pct")
        pl = "—"
        if tg is not None:
            pl = fmt_signed_money(tg) + (f" ({fmt_signed_pct(tgp)})" if tgp is not None else "")
        term = gains.get(symbol)
        taxable = "—"
        if term:
            parts = [
                f"{key} {fmt_signed_money(term[key])}" for key in ("LT", "ST") if abs(term[key]) >= 1
            ]
            if term["unk_mv"] >= 1:
                parts.append(f"term unknown on {fmt_money(term['unk_mv'])} of value")
            taxable = " · ".join(parts) or "≈ $0"
        rows.append(
            f"| {symbol}{alias} | {fmt_weight(h['weight'])} | {fmt_money(h['market_value'])} | "
            f"${h['price']:,.2f} | {pl} | {_held_in(h)} | {taxable} |"
        )
    if not rows:
        return "_No non-cash holdings._"
    return (
        "| Ticker | Weight | Value | Price | Unrealized P/L | Held in | Taxable gain by term |\n"
        "|---|---:|---:|---:|---:|---|---|\n" + "\n".join(rows)
    )


def format_focus_lots(results, symbols, as_of: datetime) -> str:
    """Taxable lots for focus names, so a proposed sale can name the exact lot."""
    wanted = set(symbols)
    rows = []
    for result in results or []:
        if result.get("error") or (result.get("tax_bucket") or "taxable") != "taxable":
            continue
        acct = account_tail(result.get("label") or "")
        for h in result.get("holdings") or []:
            if h.get("symbol") not in wanted:
                continue
            lots = h.get("lots") or []
            if not lots and h.get("date_acquired"):
                lots = [
                    {
                        "qty": h.get("quantity") or 0.0,
                        "total_cost": h.get("total_cost") or 0.0,
                        "total_gain": h.get("total_gain") or 0.0,
                        "acquired": h["date_acquired"],
                    }
                ]
            for lot in lots:
                qty = lot.get("qty") or 0.0
                cost = lot.get("total_cost") or 0.0
                per_share = lot.get("price") or (cost / qty if qty else None)
                acquired = lot.get("acquired")
                rows.append(
                    (
                        h["symbol"],
                        acquired.date().isoformat() if acquired else "",
                        f"| {h['symbol']} | {acct} | "
                        f"{acquired.strftime('%Y-%m-%d') if acquired else '—'} | {qty:,.4g} | "
                        f"{f'${per_share:,.2f}' if per_share else '—'} | "
                        f"{fmt_signed_money(lot.get('total_gain') or 0.0)} | "
                        f"{term_from_lot(lot, as_of)} |",
                    )
                )
    if not rows:
        return ""
    rows.sort(key=lambda row: (row[0], row[1]))
    return (
        "**Taxable lots for focus names** (identify a lot by account and acquired date):\n\n"
        "| Ticker | Acct | Acquired | Qty | Cost/share | Unrealized | Term |\n"
        "|---|---|---|---:|---:|---:|---|\n" + "\n".join(row[2] for row in rows) + "\n"
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
                market_value >= active_policy().analyze_market_value_floor
                and total_gain is not None
                and total_gain < active_policy().harvest_loss_dollars
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
            f"- **{row['symbol']} / {row['account']} — taxable loss review:** "
            f"qty {row['quantity']:,.4g}; value {fmt_money(row['market_value'])}; "
            f"unrealized loss -{loss}."
        )
    for holding in breaches:
        lines.append(
            f"- **{holding['symbol']} — concentration review:** "
            f"{fmt_weight(holding['weight'])} exceeds the "
            f"{active_policy().single_name_cap_pct:g}% soft maximum."
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
        if (h.get("weight") or 0.0) >= active_policy().analyze_weight_floor_pct:
            reasons.append("weight")
        tg = h.get("total_gain")
        tgp = h.get("total_gain_pct")
        sized = mv >= active_policy().analyze_market_value_floor
        if required_harvest_symbols is not None:
            harvest_required = sym in required_harvest_symbols
        else:
            taxable_mv = h.get("taxable_mv") or 0.0
            harvest_required = (
                sized and taxable_mv > 0 and tg is not None and tg < active_policy().harvest_loss_dollars
            )
        if harvest_required:
            reasons.append("harvest")
        if (
            sized
            and tg is not None
            and (tg <= active_policy().material_loss_dollars or (tgp is not None and tgp <= active_policy().material_loss_pct))
        ):
            reasons.append("loss")
        if reasons:
            row = dict(h)
            row["analyze_reasons"] = reasons
            out.append(row)
    return out


NEAR_LIMIT_PP = 1.0


def _loss_reason_text(row: dict) -> str:
    tgp, tg = row.get("total_gain_pct"), row.get("total_gain")
    return f"material loss ({tgp:+.0f}%)" if tgp is not None else f"material loss ({fmt_money(tg)})"


def format_focus_block(output_policy: BriefingOutputPolicy) -> str:
    """One focus list with the reason each name is in it, plus monitor-only names."""
    lines = [
        f"- **{symbol}** — {'; '.join(reasons)}"
        for symbol, reasons in output_policy.focus_reasons
    ] or ["- None today. Scan the monitor names only."]
    if output_policy.monitor_symbols:
        lines.append(
            "\n**Monitor only** (no write-up unless research finds a material, "
            "company-specific event): " + ", ".join(output_policy.monitor_symbols)
        )
    return "\n".join(lines)


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
    required = must_analyze_holdings(
        holdings, {row["symbol"] for row in harvest_reviews}
    )
    reasons: dict[str, list[str]] = defaultdict(list)
    cap = active_policy().single_name_cap_pct
    for row in breaches:
        reasons[row["symbol"]].append(f"above {cap:g}% single-name limit")
    for row in harvest_reviews:
        if "taxable loss review" not in reasons[row["symbol"]]:
            reasons[row["symbol"]].append("taxable loss review")
    for symbol in sorted(weight_changes):
        reasons[symbol].append("weight moved ≥ 0.5 pp since last snapshot")
    for symbol in sorted(observation_changes):
        reasons[symbol].append("quantity or lot change since last snapshot")
    for h in holdings:
        if not is_cash_symbol(h["symbol"]) and cap - NEAR_LIMIT_PP <= h["weight"] <= cap:
            reasons[h["symbol"]].append(f"within {NEAR_LIMIT_PP:g} pp of {cap:g}% single-name limit")
    # Material losses stay in focus every day: a large loss can mean a broken thesis
    # even where there is no tax angle. Size alone earns focus only on a decision day.
    floor = active_policy().analyze_weight_floor_pct
    for row in required:
        if "loss" in row["analyze_reasons"]:
            reasons[row["symbol"]].append(_loss_reason_text(row))
        if decision_day and "weight" in row["analyze_reasons"]:
            reasons[row["symbol"]].append(f"weight ≥ {floor:g}%")
    # Focus follows portfolio weight so the largest positions lead.
    order = {h["symbol"]: -(h.get("market_value") or 0.0) for h in holdings}
    focus = sorted(reasons, key=lambda s: (order.get(s, 0.0), s))
    monitor = [row["symbol"] for row in required if row["symbol"] not in reasons]
    return BriefingOutputPolicy(
        mode="DECISION DAY" if decision_day else "ROUTINE DAY",
        material_symbols=tuple(sorted(reasons)),
        refresh_candidates=refresh_candidates,
        focus_reasons=tuple((s, tuple(reasons[s])) for s in focus),
        monitor_symbols=tuple(monitor),
    )


def min_cut_to_cap(mv: float, total: float, cap_pct: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, mv - total * cap_pct / 100.0)


def format_constraint_math(holdings, grand_total: float, cluster_mv: float) -> str:
    lines = []
    for h in holdings:
        if is_cash_symbol(h["symbol"]) or h["weight"] <= active_policy().single_name_cap_pct:
            continue
        cut = min_cut_to_cap(h["market_value"], grand_total, active_policy().single_name_cap_pct)
        new_w = (h["market_value"] - cut) / grand_total * 100 if grand_total else 0
        lines.append(
            f"- **{h['symbol']}** {fmt_weight(h['weight'])}: "
            f"min **{fmt_money(cut)}** to restore {active_policy().single_name_cap_pct:g}% "
            f"(→ {fmt_weight(new_w)}). Larger is optional."
        )
    c40 = min_cut_to_cap(cluster_mv, grand_total, active_policy().cluster_soft_cap_pct)
    c38 = min_cut_to_cap(cluster_mv, grand_total, active_policy().cluster_do_not_increase_pct)
    if c40 > 0:
        lines.append(
            f"- Direct AI/semi: **{fmt_money(c40)}** to {active_policy().cluster_soft_cap_pct:g}%; "
            f"**{fmt_money(c38)}** to {active_policy().cluster_do_not_increase_pct:g}%. Hold-above OK if only "
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
    cluster_ok = cluster_mv / grand_total * 100 < active_policy().cluster_do_not_increase_pct

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
    specs = list(active_policy().marginal_examples)
    for label, sym, note in specs:
        d_direct = unit if sym in active_policy().sleeves["direct_ai_semi"] else 0.0
        d_broad = unit if sym in active_policy().sleeves["broad_ai_cycle"] else 0.0
        d_top5 = top5_weight_after(sym) - current_top5_w
        extra = note
        if d_direct and not cluster_ok:
            extra += f" — **do not add** while direct ≥ {active_policy().cluster_do_not_increase_pct:g}%"
        rows.append(row(label, d_direct, d_broad, -unit, d_top5, extra))

    overweight = next(
        (
            h
            for h in holdings
            if not is_cash_symbol(h["symbol"])
            and h["market_value"] / grand_total * 100 > active_policy().single_name_cap_pct
        ),
        None,
    )
    if overweight:
        symbol = overweight["symbol"]
        current_weight = overweight["market_value"] / grand_total * 100
        cut_to_cap = min_cut_to_cap(overweight["market_value"], grand_total, active_policy().single_name_cap_pct)
        direct_delta = -unit if symbol in active_policy().sleeves["direct_ai_semi"] else 0.0
        broad_delta = -unit if symbol in active_policy().sleeves["broad_ai_cycle"] else 0.0
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
                    f"Minimum to restore {active_policy().single_name_cap_pct:g}% is "
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


PROMPT_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "briefing_prompt.md"


def render_prompt_template(**fields) -> str:
    """Fill the briefing prompt wording (templates/briefing_prompt.md) with values.

    Edit the Markdown file to change instructions; edit this module only when a
    new computed value is needed. Placeholders use str.format syntax.
    """
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    # Optional blocks render empty; collapse the blank lines they leave behind.
    return re.sub(r"\n{3,}", "\n\n", template.format(**fields))


def build_briefing(
    holdings: list[dict],
    accounts: list[dict],
    grand_total: float,
    as_of: datetime,
    portfolio_block: str,
    results=None,
) -> BriefingBuildResult:
    policy = active_policy()
    context, context_status = load_portfolio_context()
    as_of_str = as_of.strftime("%Y-%m-%d %H:%M")
    direct_members = "+".join(policy.sleeves["direct_ai_semi"])
    broad_members = "+".join(policy.sleeves["broad_ai_cycle"])
    broad_sleeve_label = broad_ai_sleeve_label()
    crypto_label = sleeve_member_text(policy.sleeves["crypto"])
    payments_label = sleeve_member_text(policy.sleeves["payments"])
    index_label = sleeve_member_text(policy.sleeves["broad_index"])
    cluster_mv, cluster_w, _ = sleeve_stats(holdings, policy.sleeves["direct_ai_semi"], grand_total)
    broad_mv, broad_w, _ = sleeve_stats(holdings, policy.sleeves["broad_ai_cycle"], grand_total)
    crypto_mv, crypto_w, _ = sleeve_stats(holdings, policy.sleeves["crypto"], grand_total)
    pay_mv, pay_w, _ = sleeve_stats(holdings, policy.sleeves["payments"], grand_total)
    voo_mv, voo_w, _ = sleeve_stats(holdings, policy.sleeves["broad_index"], grand_total)

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
        h
        for h in holdings
        if h["weight"] > policy.single_name_cap_pct and not is_cash_symbol(h["symbol"])
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

    holdings_table = format_holdings_table(holdings, results, as_of)
    harvest_reviews = taxable_harvest_reviews(results)
    observable_review_block = format_observable_reviews(harvest_reviews, breaches)

    if cluster_w >= policy.cluster_soft_cap_pct:
        cluster_status = (
            f"ABOVE {policy.cluster_soft_cap_pct:g}% ceiling — do not add; hold-above only "
            "if cutting is punitive ST or no superior immediate deployment exists"
        )
    elif cluster_w >= policy.cluster_do_not_increase_pct:
        cluster_status = f"at {policy.cluster_do_not_increase_pct:g}% do-not-increase — do not add"
    else:
        cluster_status = "within ceiling"

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
    prior_snap, prior_label = load_prior_snapshot(as_of, BRIEFINGS_DIR, OUT_DIR)
    daily_delta = format_daily_delta(today_snap, prior_snap, prior_label)
    today_observation = observation_snapshot_dict(results, harvest_reviews, breaches, as_of)
    prior_observation, prior_observation_label = load_prior_observation(as_of, BRIEFINGS_DIR)
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
    context_block = format_context_block(context)
    if not context and CONTEXT_PATH.exists():
        # The file exists but could not be used; say so rather than silently ignoring it.
        context_block = f"{context_status}\n\n{context_block}"
    focus_block = format_focus_block(output_policy)
    focus_lots_block = format_focus_lots(results, output_policy.material_symbols, as_of)
    prior_date = str((prior_snap or {}).get("date") or (prior_snap or {}).get("as_of") or "")[:10]
    research_window = (
        f"since the last snapshot ({prior_date})" if prior_date else "from the last 30 days"
    )
    marginal_block = (
        f"\n**Marginal $10k from SGOV:**\n{marginal_10k}\n"
        if output_policy.refresh_candidates
        else ""
    )
    continuity_rule = (
        "**Prior proposals.** For each proposal in the decision history, mark today's as "
        "UNCHANGED / MODIFIED / REVERSED / RESOLVED. MODIFIED or REVERSED needs a dated new fact "
        "that invalidates an earlier assumption; a fresh look or a vaguely better outlook is not enough. "
        "Treat a proposal as executed only when its approval state is EXECUTED CONFIRMED.\n\n"
        if has_decision_history(context)
        else ""
    )
    if output_policy.refresh_candidates:
        candidate_instruction = (
            "Suggest at most two non-held ideas that would improve the portfolio. Don't add "
            "AI-cycle exposure while the direct AI/semi sleeve is at or above its no-add level. "
            "An idea can be worth watching without meeting the bar to buy today."
        )
        candidate_output = (
            "At most two non-held ideas. For each: role in the portfolio, dated evidence, entry "
            "condition, main risk, and the holding or cash it would be funded from."
        )
    else:
        candidate_instruction = "No new ideas today; the focus names come first."
        candidate_output = "Write **No candidate review today**."

    prompt = render_prompt_template(
        output_mode=output_policy.mode,
        candidate_refresh="yes" if output_policy.refresh_candidates else "no",
        research_window=research_window,
        focus_block=focus_block,
        focus_lots_block=focus_lots_block,
        context_block=context_block,
        continuity_rule=continuity_rule,
        marginal_block=marginal_block,
        as_of_str=as_of_str,
        as_of_tz=as_of.tzname() or "ET",
        snapshot_coverage=snapshot_coverage,
        grand_total=fmt_money(grand_total),
        top5_weight=fmt_weight(top5_w),
        cash_value=fmt_money(cash_mv),
        cash_weight=fmt_weight(cash_w),
        cash_components=cash_components,
        daily_delta=daily_delta,
        observed_delta=observed_delta,
        observable_review_block=observable_review_block,
        direct_members=direct_members,
        direct_weight=fmt_weight(cluster_w),
        direct_value=fmt_money(cluster_mv),
        cluster_status=cluster_status,
        broad_sleeve_label=broad_sleeve_label,
        broad_weight=fmt_weight(broad_w),
        broad_value=fmt_money(broad_mv),
        crypto_label=crypto_label,
        crypto_weight=fmt_weight(crypto_w),
        crypto_value=fmt_money(crypto_mv),
        payments_label=payments_label,
        payments_weight=fmt_weight(pay_w),
        payments_value=fmt_money(pay_mv),
        index_label=index_label,
        index_weight=fmt_weight(voo_w),
        index_value=fmt_money(voo_mv),
        constraint_math=constraint_math,
        account_lines=account_lines,
        holdings_table=holdings_table,
        single_name_cap=policy.single_name_cap_pct,
        cluster_no_add=policy.cluster_do_not_increase_pct,
        cluster_soft_cap=policy.cluster_soft_cap_pct,
        risk_off_glide=policy.risk_off_glide_pct,
        broad_members=broad_members,
        candidate_instruction=candidate_instruction,
        candidate_output=candidate_output,
    )
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
    save_weights_snapshot(result.weights_snapshot, as_of, BRIEFINGS_DIR)
    save_observation_snapshot(result.observation_snapshot, as_of, BRIEFINGS_DIR)
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
    parser.add_argument(
        "--policy",
        type=Path,
        help="Use this decision-policy JSON instead of portfolio_policy.json.",
    )
    args = parser.parse_args(argv)
    if args.policy:
        set_active_policy(load_policy(args.policy))
    try:
        return _run(args, parser)
    except AuthExpiredError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2


def _run(args, parser) -> int:
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

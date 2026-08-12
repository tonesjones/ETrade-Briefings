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
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from get_portfolio import (
    collect_tax_flags,
    fetch_portfolio_block,
    format_cost_suffix,
    format_taxable_cost_table,
    is_cash_symbol,
    save_portfolio_block,
    atomic_write_text,
)
from portfolio_policy import POLICY

PROJECT_ROOT = Path(__file__).resolve().parent
OUT_DIR = PROJECT_ROOT / "prompts"
BRIEFINGS_DIR = PROJECT_ROOT / "briefings"

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
        accounts.append({
            "label": r["label"],
            "total": r["total_value"],
            "tax_bucket": bucket,
        })
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
            if h.get("total_gain") is not None:
                d["total_gain"] += h["total_gain"]
                d["gain_known"] = True
            paid = h.get("price_paid")
            if paid and qty:
                d["price_paid_num"] += paid * qty
                d["price_paid_den"] += qty
            if h.get("lots"):
                d["lots"].extend(h["lots"])
            elif h.get("date_acquired"):
                d["lots"].append({
                    "qty": qty,
                    "total_cost": h.get("total_cost") or 0.0,
                    "total_gain": h.get("total_gain") or 0.0,
                    "market_value": h.get("market_value") or 0.0,
                    "acquired": h["date_acquired"],
                })
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
        cps = (d["total_cost"] / qty) if qty and d["cost_known"] else None
        paid = (
            d["price_paid_num"] / d["price_paid_den"]
            if d["price_paid_den"]
            else None
        )
        tgp = (
            100.0 * d["total_gain"] / d["total_cost"]
            if d["cost_known"] and d["total_cost"]
            else None
        )
        holdings.append({
            "symbol": sym,
            "market_value": mv,
            "weight": w,
            "price": d["price"],
            "alias": ALIASES.get(sym),
            "quantity": qty,
            "cost_per_share": cps,
            "price_paid": paid,
            "total_cost": d["total_cost"] if d["cost_known"] else None,
            "total_gain": d["total_gain"] if d["gain_known"] else None,
            "total_gain_pct": tgp,
            "lots": d["lots"],
            "tax_buckets": d["tax_buckets"],
            "taxable_mv": d["taxable_mv"],
            "ira_mv": d["ira_mv"],
            "roth_mv": d["roth_mv"],
        })
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
        return (
            f"- Cash ({sym}) ≈ {fmt_money(h['market_value'])} "
            f"(~{fmt_weight(h['weight'])})"
        )
    buckets = h.get("tax_buckets") or {"taxable"}
    if buckets == {"roth"}:
        tax_bucket = "roth"
    elif buckets == {"traditional"}:
        tax_bucket = "traditional"
    else:
        tax_bucket = "taxable"
    suffix = format_cost_suffix(h, as_of, tax_bucket=tax_bucket)
    return (
        f"- {sym}{alias} {fmt_weight(h['weight'])}  "
        f"({fmt_money(h['market_value'])} @ ${h['price']:.2f}{suffix})"
    )


def sleeve_stats(holdings: list[dict], symbols: tuple[str, ...], total: float):
    members = [h for h in holdings if h["symbol"] in symbols]
    mv = sum(h["market_value"] for h in members)
    w = (mv / total * 100) if total else 0.0
    return mv, w, [h["symbol"] for h in members]


def must_analyze_holdings(holdings: list[dict]) -> list[dict]:
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
        taxable_mv = h.get("taxable_mv") or 0.0
        sized = mv >= ANALYZE_MV_FLOOR
        if (
            sized
            and taxable_mv > 0
            and tg is not None
            and tg < HARVEST_LOSS_DOLLARS
        ):
            reasons.append("harvest")
        if sized and tg is not None and (
            tg <= MATERIAL_LOSS_DOLLARS
            or (tgp is not None and tgp <= MATERIAL_LOSS_PCT)
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
        lines.append(
            f"- Weight ≥ {ANALYZE_WEIGHT_FLOOR:g}%: " + ", ".join(weight)
        )
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
    symbols = [row["symbol"] for row in rows]
    lines.append("- **Do not skip:** " + (", ".join(symbols) if symbols else "None"))
    return "\n".join(lines)



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
                holdings.append({
                    "symbol": m.group(1),
                    "weight": float(m.group(2)),
                    "market_value": _parse_money_token(m.group(3)),
                    "price": float(m.group(4)),
                })
    if not holdings:
        return None

    def _pct(patterns) -> float | None:
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return float(m.group(1))
        return None

    cluster_w = _pct([
        r"Direct AI/semi[^\n]*?\*\*([\d.]+)%\*\*",
        r"pure AI/semi cluster[^\n]*?([\d.]+)%",
    ])
    broad_w = _pct([r"Broad AI-cycle liquid[^\n]*?\*\*([\d.]+)%\*\*"])
    cash_w = _pct([
        r"Cash \+ cash equivalents:[^\n]*?\(([\d.]+)%\)",
        r"Cash \+ cash equivalents[^\n]*?\(([\d.]+)%\)",
        r"Cash \(SGOV\):[^\n]*?\(([\d.]+)%\)",
        r"Cash \(SGOV\)[^\n]*?~([\d.]+)%",
    ])
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
            h["symbol"] for h in holdings
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
                f"- **{sym}** {fmt_weight(pw)} → {fmt_weight(tw)} "
                f"({sign}{tw - pw:.1f} pp)"
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
        lines.append(
            f"- {SINGLE_NAME_CAP:g}% breach **unchanged:** " + ", ".join(sorted(today_br))
        )
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
        new_w = (
            (h["market_value"] - cut) / grand_total * 100 if grand_total else 0
        )
        lines.append(
            f"- **{h['symbol']}** at {fmt_weight(h['weight'])}: "
            f"minimum sale to restore {SINGLE_NAME_CAP:g}% ≈ **{fmt_money(cut)}** "
            f"(→ {fmt_weight(new_w)}). A larger sale is optional — "
            "the limit does not require it."
        )
    c40 = min_cut_to_cap(cluster_mv, grand_total, CLUSTER_SOFT_CAP)
    c38 = min_cut_to_cap(cluster_mv, grand_total, CLUSTER_NO_ADD)
    if c40 > 0:
        lines.append(
            f"- Direct AI/semi: min cut to {CLUSTER_SOFT_CAP:g}% ≈ **{fmt_money(c40)}**; "
            f"to {CLUSTER_NO_ADD:g}% ≈ **{fmt_money(c38)}**. Do not force the full cut "
            "if the only lots are punitive ST — remaining above the ceiling "
            "is allowed; adding is not."
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
    top5_w: float,
) -> str:
    if grand_total <= 0:
        return "_Total unavailable._"
    unit = 10_000.0 / grand_total * 100
    top5_syms = {h["symbol"] for h in holdings[:5]}
    cluster_ok = cluster_mv / grand_total * 100 < CLUSTER_NO_ADD

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
        "| Deploy $10k from SGOV into | Δ direct | Δ broad | Δ cash | Δ top-5 | Note |",
        "|---|---:|---:|---:|---:|---|",
    ]
    specs = [
        ("VOO", "VOO", "already top-5; diversifies factor"),
        ("MSFT", "MSFT", "broad AI only — not direct semi"),
        ("GOOGL", "GOOGL", "top-5 + broad AI-cycle"),
        ("NVDA", "NVDA", "raises direct stack"),
        ("New non-AI diversifier", None, "improves concentration / factor"),
    ]
    for label, sym, note in specs:
        d_direct = unit if sym in AI_SEMI_CLUSTER else 0.0
        d_broad = unit if sym in BROAD_AI_LIQUID else 0.0
        d_top5 = unit if (sym in top5_syms) else 0.0
        extra = note
        if d_direct and not cluster_ok:
            extra += f" — **do not add** while direct ≥ {CLUSTER_NO_ADD:g}%"
        rows.append(row(label, d_direct, d_broad, -unit, d_top5, extra))

    overweight = next(
        (
            h for h in holdings
            if not is_cash_symbol(h["symbol"])
            and h["weight"] > SINGLE_NAME_CAP
        ),
        None,
    )
    if overweight:
        symbol = overweight["symbol"]
        cut_to_cap = min_cut_to_cap(
            overweight["market_value"], grand_total, SINGLE_NAME_CAP
        )
        direct_delta = -unit if symbol in AI_SEMI_CLUSTER else 0.0
        broad_delta = -unit if symbol in BROAD_AI_LIQUID else 0.0
        top5_delta = -unit if symbol in top5_syms else 0.0
        rows.extend([
            "",
            (
                f"Sell $10k **{symbol}** → cash: Δ direct {direct_delta:+.2f} pp, "
                f"Δ broad {broad_delta:+.2f}, Δ cash {unit:+.2f}, "
                f"Δ top-5 {top5_delta:+.2f}. {symbol} "
                f"{fmt_weight(overweight['weight'])} → "
                f"{fmt_weight(overweight['weight'] - unit)}. "
                f"Minimum to restore {SINGLE_NAME_CAP:g}% is "
                f"{fmt_money(cut_to_cap)}."
            ),
        ])
    rows.append(
        f"$10k = **{unit:.2f} pp** of liquid ({fmt_money(grand_total)}). "
        "Use this unit on every proposed buy/sell."
    )
    return "\n".join(rows)


def build_prompt(
    holdings: list[dict],
    accounts: list[dict],
    grand_total: float,
    as_of: datetime,
    portfolio_block: str,
    results=None,
) -> str:
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

    top5 = holdings[:5]
    top5_w = sum(h["weight"] for h in top5)

    breaches = [
        h for h in holdings if h["weight"] > SINGLE_NAME_CAP and not is_cash_symbol(h["symbol"])
    ]

    account_lines = "\n".join(
        f"  - {a['label']}: ≈ {fmt_money(a['total'])}" for a in accounts
    )

    holdings_lines = "\n".join(
        holding_line(h, as_of) for h in holdings if h["market_value"] >= 50
    )
    analyze_rows = must_analyze_holdings(holdings)
    must_analyze_block = format_must_analyze(analyze_rows)

    if cluster_w >= CLUSTER_SOFT_CAP:
        cluster_status = (
            f"ABOVE {CLUSTER_SOFT_CAP:g}% soft ceiling ({fmt_weight(cluster_w)}). "
            f"Do not increase. Remaining above {CLUSTER_SOFT_CAP:g}% is allowed only when the tax "
            "cost of cutting is punitive — never redefine the ceiling up."
        )
    elif cluster_w >= CLUSTER_NO_ADD:
        cluster_status = (
            f"at the {CLUSTER_NO_ADD:g}% do-not-increase line ({fmt_weight(cluster_w)}). "
            "Do not add to this stack."
        )
    else:
        cluster_status = "within the soft ceiling."

    risk_live = (
        f"- **Direct AI/semi** ({direct_members}) ≈ "
        f"**{fmt_weight(cluster_w)}** of liquid ({fmt_money(cluster_mv)}) — "
        f"{cluster_status}"
    )
    risk_live += (
        f"\n- **Broad AI-cycle liquid** ({broad_members}) ≈ "
        f"**{fmt_weight(broad_w)}** ({fmt_money(broad_mv)}). "
        "Direct-stack % is a lower bound on cycle risk."
    )
    if breaches:
        risk_live += "\n" + "\n".join(
            f"- **Soft single-name limit ({SINGLE_NAME_CAP:g}%) breached:** {h['symbol']} at {fmt_weight(h['weight'])}"
            for h in breaches
        )

    tax_table = (
        format_taxable_cost_table(results, as_of)
        if results is not None
        else "_Cost table unavailable this run._"
    )
    tax_flags = "\n".join(
        collect_tax_flags(results, as_of) if results is not None else []
    )

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
    save_weights_snapshot(today_snap, as_of)
    daily_delta = format_daily_delta(today_snap, prior_snap, prior_label)
    constraint_math = format_constraint_math(holdings, grand_total, cluster_mv)
    marginal_10k = format_marginal_10k(
        holdings, grand_total, cluster_mv, broad_mv, cash_mv, top5_w
    )

    prompt = f"""You are the portfolio manager for this book — not a stock screener. Every recommendation must answer one of: **Keep it. Reduce it. Replace it. Deploy cash into something better.** Every sale must name **this ticker, this account, and this tax lot** — not a consolidated symbol.

Core reasoning (always):
1. What risk am I trying to control?
2. Is it the company, valuation, factor, or position size?
3. Does the position need to be reduced?
4. What is the **smallest economically meaningful** reduction?
5. Which account and lot accomplish that most efficiently?
6. What should the proceeds fund?
7. Does a replacement improve portfolio-level diversification?
8. What does the post-trade book look like (weights + $10k marginals)?
9. What specific event makes today’s decision wrong?

Title the briefing from system date:

**Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

Use the “As of” stamp below for marks. Do not invent prices, cost, dates, or ST/LT. If a field is missing, say so.

---

# 1. Portfolio data (live — use these exact numbers)

**As of:** {as_of_str} {as_of.tzname() or 'ET'}
**E*TRADE total ≈ {fmt_money(grand_total)}**
**Top-5 (E*TRADE):** {fmt_weight(top5_w)} · **Cash + cash equivalents:** {fmt_money(cash_mv)} ({fmt_weight(cash_w)})

**Daily delta (market move vs thesis move):**
{daily_delta}

**Pre-computed factor sleeves (do not invent factor percentages):**
- Direct AI/semi ({direct_members}): **{fmt_weight(cluster_w)}** / {fmt_money(cluster_mv)}
- Broad AI-cycle liquid ({broad_sleeve_label}): **{fmt_weight(broad_w)}** / {fmt_money(broad_mv)}
- Crypto ({crypto_label}): {fmt_weight(crypto_w)} / {fmt_money(crypto_mv)}
- Payments ({payments_label}): {fmt_weight(pay_w)} / {fmt_money(pay_mv)}
- Broad index ({index_label}): {fmt_weight(voo_w)} / {fmt_money(voo_mv)}

**Constraint math (minimum necessary — use these dollars):**
{constraint_math}

**Marginal $10,000 from SGOV (pre-computed; apply the same unit to every proposed trade):**
{marginal_10k}

**Accounts (tax location is not optional):**
{account_lines}

**Consolidated holdings:**

{holdings_lines}

**Must-analyze for the position table (pre-computed — do not skip):**
{must_analyze_block}

**Cost / lots (E*TRADE):** `avg` = cost/share (else price paid). `cost` = dollars in. `P/L` = unrealized vs that cost. Taxable **ST** = held ≤ 1 year; **LT** = held > 1 year. Mixed lots show both. IRA/Roth: economic P/L only — **not** capital-gains events. Same ticker in two accounts is two decision buckets.

**Taxable lot table (ST/LT source of truth for the brokerage):**

{tax_table}

**Live cost / tax flags:**
{tax_flags}

**Per-account source block:**

```
{portfolio_block}
```

**Live risk flags:**
{risk_live}

---

# 2. Risk limits (hard)

- Liquid single-name soft max **{SINGLE_NAME_CAP:g}%**. Flag breaches. Use the **minimum necessary** dollar cut above — do not sell a {SINGLE_NAME_CAP:g}% slice of a name merely because it is slightly over.
- Direct AI/semi: **do not increase** if already ≥ {CLUSTER_NO_ADD:g}% (now {fmt_weight(cluster_w)}). Soft ceiling **{CLUSTER_SOFT_CAP:g}%**. You may *hold* above {CLUSTER_SOFT_CAP:g}% when cutting would realize punitive ST tax. You may **not** raise the ceiling, and you may **not** add. In Risk-Off or when expected forward returns no longer pay for the risk, work *toward* {RISK_OFF_GLIDE:g}% only via tax-aware lots.
- Never apply ST/LT **capital-gains tax** to Traditional IRA or Roth.
- **Wash-sale (hard):** before any taxable loss realization, check recent buys and the proposed replacement. Do not harvest and immediately repurchase the same or substantially identical security unless wash-sale implications are explicit. If unclear, say so and do not treat the loss as usable.

---

# 3. Decision engine

## Objective hierarchy

1. Avoid unacceptable portfolio-level concentration / drawdown.
2. Do not sell a working thesis just to tidy weights. **#2 cannot veto a hard #1 breach.**
3. Maximize expected risk-adjusted return.
4. Minimize unnecessary taxes and trading friction.
5. Deploy cash only when expected return justifies less liquidity.

Never sacrifice **#1** for #3. Never sacrifice **#4** for *incremental* #3. **#1 beats #4.**

## Trade threshold

Do not recommend a taxable sale because another asset looks marginally better. Compare: book benefit · tax/friction · concentration actually removed · use of proceeds · whether the cut can wait.

**Prefer no trade** when the benefit does not clearly exceed the tax cost.

A forced trade is justified when: a hard risk limit is breached; the thesis materially breaks; downside asymmetry is unacceptable; factor concentration creates unacceptable drawdown risk; **or valuation is sufficiently unfavorable that expected forward return no longer compensates for the risk and tax/friction** (not only when someone would call it “extreme”).

**Hold / no action is a valid, often high-conviction, PM decision.** Do not manufacture trades.

## Minimum necessary trade

When a limit is breached: (1) use the pre-computed **minimum dollars** to restore it or materially improve risk; (2) compare that with a larger cut; (3) prefer the **smallest trade that meaningfully fixes the problem** unless thesis/valuation independently justifies more. Do not sell more because the position is profitable.

## Factor + $10k marginals

Use the pre-computed sleeves and the $10k table. Every proposed **buy** must show Δ direct AI/semi, Δ broad AI-cycle, Δ cash, Δ top-5. The configured broad AI-cycle members ({broad_members}) share the same AI-capex tape — ticker diversity ≠ factor diversity. Do not invent a 9-factor model.

## Sizing

Conviction uses only **remaining budget** after caps. High-vol names get a tighter cap. {SINGLE_NAME_CAP:g}% is a ceiling, not a target.

## Opportunity cost (proposed trades only)

Buy: why this $10k vs the best 2 names already held (usually VOO / a core compounder / SGOV), with the marginal %s. Sale: why this reducer of the risk rather than another.

## Replacement test

**Replace** requires all of: what is reduced and why; the replacement; risk removed; new exposure introduced; tax/friction; why the replacement has superior expected risk-adjusted return. “Sell and hold cash” is **Reduce / Deploy**, not Replace.

## Thesis vs price (must-analyze names)

Score separately: thesis · valuation · trend · catalyst. “Great company” ≠ “attractive at this price.” Every action you touch must include a **horizon** (days / weeks / months / event-driven).

## Harvest (taxable losses only)

Thesis intact? Buy it today? Better replacement? Wash-sale? Tax benefit worth the economic sale? Do not harvest just because a name is red.

## Price is a trigger, not a thesis

When a zone is hit, classify the move: market-wide / factor-wide / company-specific / noise. Escalate to Sell/Exit only if supported by a material change in thesis, valuation, structure, or portfolio risk. Do not whipsaw volatile names on price alone.

## New ideas

Only if cash/proceeds exist and they do **not** raise direct AI/semi. Must pass the $10k marginal test.

---

# 4. Tax engine (Trim/Sell only — skip on Holds)

A ticker is **not** one fungible lot. AMD taxable LT ≠ AMD taxable ST ≠ IRA AMD.

1. Should **economic** exposure fall? If yes, by the **minimum necessary** dollars.
2. Choose **account and lot** — not a fixed waterfall.
3. Estimate tax (ST ≈ ordinary; LT ≈ LTCG; $0 CG on IRA/Roth — that does **not** make them the default sleeve).
4. Where do proceeds go? If claimed as Replace, pass the replacement test.
5. Recalculate weights, factor sleeves, cash, tax realized, and the $10k marginals.

## Account / lot selection

Pick the account and lot with the best combination of: risk reduction achieved · tax/friction · thesis quality of what you sell · remaining factor concentration · future tax optionality (e.g. letting ST lots age into LT).

When two feasible sleeves exist, **compare at least two** (e.g. taxable LT vs IRA vs taxable ST). Do **not** use a fixed account liquidation order. Explain why the chosen account/lot dominates the alternative.

“Tax-free to sell” ≠ “correct sleeve.” Do not gut IRA/Roth compounders to spare a taxable ST lot you were not forced to touch. If the only cheap economic cut is IRA, say so. If the only taxable cut is a fat ST gain, **prefer no trade** unless #1 forces it.

State: **Why this ticker, this account, and this lot rather than the next-best alternative?**

---

# 5. Output format (this order — scannable, short)

**Daily Portfolio Action Briefing – [Date] – Live E*TRADE Book**

**0. Executive Decision**
- Best action today: **Trade** / **No trade**
- Confidence: High / Medium / Low   (e.g. “No trade — High confidence” is a complete answer)
- Posture: Reduce risk / Maintain / Add risk  +  Risk-On / Neutral / Risk-Off
- Trades today (ticker, **account**, **lot/term**, **$ size**, **horizon**, proceeds) — or **None**
- Trades I will **not** make today, and why
- Why, in 3–5 sentences

**1. Daily Delta + Health (short)**
What changed vs the prior pull (use the pre-computed delta). Then 8–12 lines: weights, P/L territory, ST/LT, factor sleeves, cash, limit breaches. No 20-name 1d/1w/1m dump.

**2. Regime, catalysts, stress**
Only what **changes a decision**. Then a **directional** stress (no fake VaR): Nasdaq −10%; semi index −15%; AI-capex expectations fall; rates spike; broad correction *without* AI-specific damage. Which names drive the drawdown, what offsets. Qualitative unless you have real numbers.

**3. Must-analyze positions (list in §1 — configured weight floor, harvest, material losses)**
Every ticker in that list gets a row. Do not omit a name because it is under {ANALYZE_WEIGHT_FLOOR:g}% — harvest and material-loss names stay in even under the weight floor. Compact table: Action · horizon · last · avg/cost · P/L · term/account · thesis/valuation · target wt · one-line why. Tax-engine + two-sleeve compare **only** on Trim/Sell. Replace rows must pass the replacement test. Optional 1–3 new ideas if they pass factor + $10k.

**4. Cash, tax bill, post-trade book**
SGOV plan (now {fmt_money(cash_mv)}). If any trade: min-necessary $ vs $ recommended; estimated ST/LT tax; updated cash, top-5, direct %, broad %, $10k marginals of the trade. Directional Nasdaq beta: up / similar / down. **No fake expected-return or drawdown to one decimal.**

**5. Zones for the next session** (human checklist — not a live OMS)
Add / Hold / Trim / Exit on names you touched or near a limit. Price = trigger, not thesis. One line: “If X happens, today’s decision is wrong.”
"""
    return prompt.strip() + "\n"


def main(argv=None) -> int:
    parser = ArgumentParser(description="Build a portfolio decision prompt.")
    parser.add_argument("--allow-partial", action="store_true", help="Generate an unsafe marked prompt when an account fails.")
    args = parser.parse_args(argv)
    print("Fetching live E*TRADE portfolio...")
    formatted, grand_total, npos, results, as_of = fetch_portfolio_block(verbose=True, allow_partial=args.allow_partial)
    save_portfolio_block(formatted, as_of=as_of)

    holdings, accounts = consolidate(results, grand_total)
    prompt = build_prompt(
        holdings, accounts, grand_total, as_of, formatted, results=results
    )

    OUT_DIR.mkdir(exist_ok=True)
    date_str = as_of.strftime("%Y-%m-%d")
    dated = OUT_DIR / f"daily_briefing_prompt_{date_str}.md"
    latest = OUT_DIR / "daily_briefing_prompt_latest.md"
    atomic_write_text(dated, prompt)
    atomic_write_text(latest, prompt)

    # Clipboard for quick paste into grok.com
    try:
        import subprocess

        subprocess.run(
            ["clip"],
            input=prompt.encode("utf-16-le"),
            check=True,
        )
        clipped = True
    except Exception:
        clipped = False

    print()
    print("=" * 60)
    print(f"Processed prompt written: {dated}")
    print(f"Also:                      {latest}")
    print(f"Total liquid:              ${grand_total:,.0f}")
    print(f"Positions:                 {npos}")
    if clipped:
        print("Clipboard:                 FULL PROMPT copied — paste into your preferred model for analysis")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

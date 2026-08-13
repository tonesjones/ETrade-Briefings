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
    account_tail,
    collect_tax_flags,
    copy_to_clipboard,
    fetch_portfolio_block,
    format_lot_table,
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
MARGINAL_EXAMPLES = POLICY.marginal_examples


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
    if not lines:
        return "- None"
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
    specs = list(MARGINAL_EXAMPLES)
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
        account_bits.append(f"  - {shown}{loc_note}: ≈ {fmt_money(a['total'])}")
    account_lines = "\n".join(account_bits) if account_bits else "  - _No funded accounts._"

    holdings_lines = "\n".join(
        holding_line(h, as_of) for h in holdings if h["market_value"] >= 50
    )
    analyze_rows = must_analyze_holdings(holdings)
    must_analyze_block = format_must_analyze(analyze_rows)

    if cluster_w >= CLUSTER_SOFT_CAP:
        cluster_status = (
            f"ABOVE {CLUSTER_SOFT_CAP:g}% ceiling — do not add; hold-above only "
            "if cutting is punitive ST"
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
    save_weights_snapshot(today_snap, as_of)
    daily_delta = format_daily_delta(today_snap, prior_snap, prior_label)
    constraint_math = format_constraint_math(holdings, grand_total, cluster_mv)
    marginal_10k = format_marginal_10k(
        holdings, grand_total, cluster_mv, broad_mv, cash_mv, top5_w
    )

    prompt = f"""You are the PM for this book, not a screener. Actions: **Keep / Reduce / Replace / Deploy**. A sale names **this ticker, this account, this lot**. Use the As-of marks. Do not invent prices, cost, dates, or ST/LT.

Title: **Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

---

# 1. Live book

**As of:** {as_of_str} {as_of.tzname() or 'ET'}
**E*TRADE total ≈ {fmt_money(grand_total)}**
**Top-5 (E*TRADE):** {fmt_weight(top5_w)} · **Cash + cash equivalents:** {fmt_money(cash_mv)} ({fmt_weight(cash_w)})

**Daily delta:**
{daily_delta}

**Factor sleeves (do not invent %):**
- Direct AI/semi ({direct_members}): **{fmt_weight(cluster_w)}** / {fmt_money(cluster_mv)} — {cluster_status}
- Broad AI-cycle liquid ({broad_sleeve_label}): **{fmt_weight(broad_w)}** / {fmt_money(broad_mv)} (direct is a lower bound on cycle risk)
- Crypto ({crypto_label}): {fmt_weight(crypto_w)} / {fmt_money(crypto_mv)}
- Payments ({payments_label}): {fmt_weight(pay_w)} / {fmt_money(pay_mv)}
- Broad index ({index_label}): {fmt_weight(voo_w)} / {fmt_money(voo_mv)}

**Constraint math (use these dollars):**
{constraint_math}

**Marginal $10k from SGOV (apply this unit to every proposed trade):**
{marginal_10k}

**Accounts:**
{account_lines}

**Consolidated holdings:**

{holdings_lines}

**Must-analyze (do not skip):**
{must_analyze_block}

**Lots** (source of truth). Avg = cost/share. Cost = dollars in. P/L = unrealized. Taxable ST = held ≤ 1 year; LT = held > 1 year. IRA/Roth = economic P/L only — not a CG event. Same ticker in two accounts = two decision buckets.

{tax_table}
{tax_flag_block}
---

# 2. Rules

**Limits.** Single-name soft max **{SINGLE_NAME_CAP:g}%**. Use the pre-computed min $ — do not sell a {SINGLE_NAME_CAP:g}% slice of a name because it is slightly over. Direct AI/semi: do not add at ≥ {CLUSTER_NO_ADD:g}% (now {fmt_weight(cluster_w)}). Soft ceiling **{CLUSTER_SOFT_CAP:g}%**; holding above is allowed only if cutting is punitive ST — never raise the ceiling. Risk-Off: work *toward* {RISK_OFF_GLIDE:g}% via tax-aware lots only.

**Tax.** No ST/LT capital-gains tax on Traditional IRA / Roth. Wash-sale: do not harvest and immediately buy the same or substantially identical name unless implications are explicit; if unclear, do not treat the loss as usable.

**Hierarchy.** (1) concentration / drawdown (2) do not tidy a working thesis — #2 cannot veto a hard #1 (3) risk-adjusted return (4) tax/friction (5) deploy cash only if return beats liquidity. #1 beats #3 and #4. #4 beats incremental #3.

**Trade bar.** Prefer **no trade** when benefit does not clearly exceed tax/friction. Forced: hard limit; thesis break; unacceptable downside or factor drawdown; or valuation so poor that expected return no longer pays for the risk and tax. Hold is a valid high-conviction call. Do not manufacture trades.

**Sizing.** On a breach: use the pre-computed min $, compare a larger cut, pick the smallest that actually fixes the problem. Do not sell more because it is profitable. {SINGLE_NAME_CAP:g}% is a ceiling, not a target. High-vol names get a tighter cap. Conviction uses remaining budget after caps.

**Factor.** Every buy shows Δ direct / Δ broad / Δ cash / Δ top-5 from the $10k table. Broad members ({broad_members}) share the AI-capex tape — ticker diversity ≠ factor diversity. Buy: why this $10k vs the best 2 names already held. Sale: why this reducer rather than another.

**Replace** = named sale + named buy (cash is Reduce/Deploy). State risk removed, risk added, tax, and why expected R/R is better.

**Thesis vs price.** Score thesis · valuation · trend · catalyst separately. Horizon on every action. Price is a trigger, not a thesis — classify moves as market / factor / company / noise. Harvest only taxable losses, and only if thesis is weak, wash-sale is clear, and tax benefit is worth it.

**New ideas.** Only with cash/proceeds, must not raise direct AI/semi, must pass the $10k test.

# 3. Tax engine (Trim/Sell only)

A ticker is not one lot (taxable LT ≠ taxable ST ≠ IRA). Compare at least two sleeves. Pick account/lot on: risk removed · tax · thesis of what you sell · leftover factor · ST→LT optionality. Tax-free ≠ correct sleeve — do not gut IRA/Roth to spare a taxable ST lot you were not forced to touch. If the only cheap cut is IRA, say so. If the only taxable cut is a fat ST gain, prefer no trade unless #1 forces it. State: **why this ticker, account, and lot vs the next-best.** Then recompute weights, sleeves, cash, tax, $10k.

# 4. Output (this order, short)

**Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

**0. Executive Decision** — Trade / No trade · High/Medium/Low · Reduce/Maintain/Add + Risk-On/Neutral/Off · trades (ticker, account, lot/term, $, horizon, proceeds) or None · trades I will not make · why in 3–5 sentences

**1. Daily Delta + Health** — use the pre-computed delta; 8–12 lines on weights, P/L, ST/LT, sleeves, cash, breaches. No 20-name tape dump.

**2. Regime / stress** — only what changes a decision. Directional (no fake VaR): Nasdaq −10%; semi −15%; AI-capex down; rates spike; broad correction without AI damage.

**3. Must-analyze** — every name listed in §1. Do not omit a name because it is under {ANALYZE_WEIGHT_FLOOR:g}%; harvest and material-loss names stay in. Compact table: Action · horizon · last · avg/cost · P/L · term/account · thesis/valuation · target wt · one-line why. Tax-engine + two-sleeve compare only on Trim/Sell. Optional 1–3 new ideas if they pass factor + $10k.

**4. Cash / tax / post-trade** — SGOV plan (now {fmt_money(cash_mv)}). If any trade: min $ vs recommended; estimated ST/LT tax; updated cash, top-5, direct %, broad %, $10k of the trade. Nasdaq beta: up / similar / down. No fake expected-return or drawdown to one decimal.

**5. Zones** — Add / Hold / Trim / Exit on names you touched or near a limit. One line: if X happens, today’s decision is wrong.
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

    clipped = copy_to_clipboard(prompt)

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

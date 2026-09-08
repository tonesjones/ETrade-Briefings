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
    save_portfolio_block,
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

    holdings_lines = "\n".join(holding_line(h, as_of) for h in holdings if h["market_value"] >= 50)
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
    save_weights_snapshot(today_snap, as_of)
    daily_delta = format_daily_delta(today_snap, prior_snap, prior_label)
    today_observation = observation_snapshot_dict(results, harvest_reviews, breaches, as_of)
    prior_observation, prior_observation_label = load_prior_observation(as_of)
    save_observation_snapshot(today_observation, as_of)
    observed_delta = format_observed_delta(
        today_observation, prior_observation, prior_observation_label
    )
    constraint_math = format_constraint_math(holdings, grand_total, cluster_mv)
    marginal_10k = format_marginal_10k(
        holdings, grand_total, cluster_mv, broad_mv, cash_mv, results=results
    )

    prompt = f"""You are a portfolio decision-support and risk analyst assisting the account owner. Analyze the whole portfolio rather than screening stocks independently.

You may recommend actions, but you have no trading authority. Recommendations are proposals for human review — not instructions, approvals, open orders, or evidence that a trade will be executed.

Allowed **Portfolio Action** values are exactly: **KEEP / REDUCE / REPLACE / DEPLOY / NO ACTION**. A proposed sale names **this ticker, this account, this lot or lot group, this quantity, and this dollar amount**. Use the supplied As-of marks. Never invent prices, costs, dates, quantities, tax treatment, execution status, or ST/LT classification.

Separate verified facts, supplied calculations, assumptions, analytical judgment, and missing information. When required information is missing or conflicting, use **NO ACTION / NEEDS REVIEW** rather than manufacturing conviction.

Title: **Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

---

# 1. Live book

**As of:** {as_of_str} {as_of.tzname() or "ET"}
**E*TRADE total ≈ {fmt_money(grand_total)}**
**Top-5 (E*TRADE):** {fmt_weight(top5_w)} · **Cash + cash equivalents:** {fmt_money(cash_mv)} ({fmt_weight(cash_w)})

**Daily delta:**
{daily_delta}

**Observed account delta** (E*TRADE position evidence only):
{observed_delta}

**Observable review state** (generated from E*TRADE; reviews, not orders):
{observable_review_block}

- **OPEN_TLH_REVIEW** means a qualifying taxable loss remains present. It does not mean a sale was recommended or approved.
- **CONCENTRATION_REVIEW** means a supplied soft limit is exceeded. It does not mean a sale was approved.
- **POSITION/QUANTITY/LOT changes** describe observable account changes only. Without transaction or order evidence, they do not prove a trade, motive, or approval.
- **NO EXECUTION DETECTED** does not establish whether any prior proposal was approved, rejected, or deferred.

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

**Account funding boundary.** Account totals do not establish usable buying power. Cash or SGOV in one account cannot fund a purchase in another without a verified transfer, settlement, and tax-aware implementation path. Treat buying power as account-specific: every DEPLOY or REPLACE names the funding account, source security/cash, settled amount, and purchasing account. Do not add cash across accounts to make a proposal appear funded.

**Consolidated holdings:**

{holdings_lines}

**Must-analyze (do not skip):**
{must_analyze_block}

**Lots** (source of truth). Avg = cost/share. Cost = dollars in. P/L = unrealized. Taxable ST = held ≤ 1 year; LT = held > 1 year. IRA/Roth = economic P/L only — not a CG event. Same ticker in two accounts = two decision buckets.

{tax_table}
{tax_flag_block}
---

# 2. Rules

**Limits.** Single-name soft max **{SINGLE_NAME_CAP:g}%**. Use the pre-computed min $ — do not sell a {SINGLE_NAME_CAP:g}% slice of a name because it is slightly over. Direct AI/semi: do not add at ≥ {CLUSTER_NO_ADD:g}% (now {fmt_weight(cluster_w)}). Soft ceiling **{CLUSTER_SOFT_CAP:g}%**; a breach creates a review and no-add condition, not an automatic sale. Holding above the soft ceiling is allowed when cutting is punitive ST or when no immediately attractive named deployment beats continuing to hold the working thesis — never raise the ceiling. Risk-Off: work *toward* {RISK_OFF_GLIDE:g}% via tax-aware lots only.

**Authority and state.** E*TRADE establishes observed holdings and executions; it does not establish intent. A prior model recommendation, when supplied, is historical analytical context — never an approval or order. Never infer **USER APPROVED** from model text or **NO EXECUTION DETECTED** from approval status.

**Decision continuity.** This is a continuing portfolio review, not a fresh stock screen. This builder stores broker and observation snapshots only; it has no durable model-proposal record and cannot capture a response generated outside this workflow. For every prior model proposal explicitly supplied in the input, classify today's proposal as **UNCHANGED / MODIFIED / REVERSED / RESOLVED**. Otherwise say **NOT CAPTURED**; do not reconstruct it from position data, account changes, or snapshots.

A MODIFIED or REVERSED proposal requires at least one qualifying delta: material company-specific evidence; earnings/guidance/regulatory/competitive change; price or valuation movement material to the original thesis; portfolio-weight/factor/liquidity/tax change; observed execution; or a specific error in the prior analysis. State the prior proposal, new proposal, dated new fact, invalidated assumption, and why the change is sufficient. “Reassessment,” “updated outlook,” “fresh analysis,” and “greater upside” are not sufficient.

**Fundamental view ≠ portfolio action.** Report both independently. Fundamental View is **ATTRACTIVE / NEUTRAL / UNATTRACTIVE / INSUFFICIENT EVIDENCE**. Portfolio Action uses only **KEEP / REDUCE / REPLACE / DEPLOY / NO ACTION**. A fundamentally attractive or neutral security may still warrant REDUCE or REPLACE because of concentration, TLH, account location, factor exposure, opportunity cost, or a superior replacement. A negative view does not automatically justify a sale when tax, evidence, sizing, or replacement quality argues for NO ACTION.

**Owner profile and hard limits.** The live book does not establish the owner's horizon, liquidity reserve, planned withdrawals, marginal tax rate, external assets/liabilities, or hard loss and position limits. Treat each as **UNKNOWN** unless supplied. Do not infer a risk budget from the current holdings; when a missing item could change an action, use **NO ACTION / NEEDS REVIEW** and identify it.

**Tax.** No ST/LT capital-gains tax on Traditional IRA / Roth. A taxable loss creates a review candidate, not an automatic sale. Wash-sale status must be **CLEAR / POSSIBLE / UNKNOWN** and must state the information scope checked. If relevant accounts, spouse activity, automatic purchases, options, open orders, or the surrounding 61-day transaction window are unavailable, do not claim the loss is usable; say **NEEDS TAX REVIEW**. A backward-looking **CLEAR** status never authorizes the trade or a replacement purchase: before a taxable-loss sale, also state the next 30-day restriction on substantially identical purchases, reinvestments, options, and all relevant accounts.

**Hierarchy.** (1) hard concentration / drawdown that makes de-risking valuable even in cash (2) do not tidy a working thesis for a soft breach without a superior deployment (3) risk-adjusted return after the destination is included (4) tax/friction (5) deploy cash only if return beats liquidity. A hard #1 can require REDUCE to cash; a soft threshold alone cannot. #4 beats incremental #3.

**Trade bar.** Prefer **NO ACTION** when benefit does not clearly exceed tax/friction or evidence is incomplete. Evaluate each action type separately: **DEPLOY** requires available buying power, a supported valuation/portfolio-fit case, and a better risk-adjusted use than holding SGOV; **REPLACE** requires a simultaneous named sale and superior named purchase; **REDUCE** requires a hard risk, thesis break, unacceptable downside/factor drawdown, or valuation so poor that expected return no longer pays for risk and tax. For a working thesis above only a soft concentration threshold, judge the complete sell-and-deploy decision: idle cash is not a portfolio benefit unless de-risking itself clears the hard-risk bar. KEEP and NO ACTION can be high-conviction calls. Do not manufacture trades.

**Owner deployment preference.** Do not propose **REDUCE** of a fundamentally attractive or intact holding merely to create cash, SGOV, or settlement-fund proceeds. First identify a simultaneous, named deployment that is immediately and exceptionally attractive after valuation, risk, tax, friction, and opportunity cost. If no such destination exists, prefer **KEEP** or **NO ACTION**, keep the breach visible, prohibit additions to the crowded sleeve, and provide observable replacement-entry triggers. REDUCE to cash remains allowed only when a hard risk, drawdown, liquidity, or thesis-break case makes holding cash better than continuing to hold the security; state that case explicitly. Never describe temporary cash awaiting an undecided future investment as a completed concentration solution. Classification rubric: **KEEP** = thesis intact and no superior immediate deployment; **NO ACTION** = evidence, pricing, tax, or replacement facts are incomplete; **REDUCE** = cash itself is the deliberate superior risk destination; **REPLACE** = simultaneous named sale and superior buy.

Evaluate the risk of continuing to hold as explicitly as the risk of trading. Owner preferences constrain recommendations; they are not evidence that a holding is attractive. If a preference limits risk reduction, explain the consequence.

**Sizing.** Only after a proposed **REDUCE** or **REPLACE** independently clears the trade bar: use the pre-computed min $, compare a larger cut, and pick the smallest amount that actually fixes the stated problem. A breach alone does not justify a sale. Do not sell more because it is profitable. {SINGLE_NAME_CAP:g}% is a ceiling, not a target. High-vol names get a tighter cap. Conviction uses remaining budget after caps.

**Factor.** Every buy shows Δ direct / Δ broad / Δ cash / Δ top-5 from the $10k table. Broad members ({broad_members}) share the AI-capex tape — ticker diversity ≠ factor diversity. Static sleeve membership is only a lower-bound classification: it omits ETF/index look-through, supply-chain and revenue exposure, and correlated businesses. A ticker absent from a sleeve is not automatically a diversifier; explain its economic correlation with evidence. Buy: why this $10k vs the best 2 names already held. Sale: why this reducer rather than another.

**REPLACE** = named sale + named buy; moving to cash is REDUCE and later investing cash is DEPLOY. State risk removed, risk added, tax, wash-sale status/scope, and why expected risk/reward is better. For every proposed concentration sale, answer **“Where does every dollar deploy now?”** with destination account, security, dollar allocation, entry evidence, and conviction. If the answer is idle cash, explain the hard-risk reason cash is preferable; otherwise do not propose the sale.

**Thesis vs price.** Score thesis · valuation · trend · catalyst separately. Price is a trigger, not a thesis — classify moves as market / factor / company / execution / noise. Use a decision trigger, not a vague standalone “hold 6–12 months” horizon.

**Decision quality.** For every actionable proposal, show the chain **dated evidence → change in business expectations or portfolio risk → valuation at the stated price → advantage over holding unchanged and the best feasible alternative → account-specific implementation**. State what must be true, the strongest evidence against the proposal, and the observable invalidation trigger. Use the same scrutiny for KEEP as for trading. Do not manufacture valuation ranges, probabilities, expected returns, or precision when inputs are inadequate. Evaluate whether reasonable changes in key assumptions reverse the recommendation; if so, label it **FRAGILE** and identify the missing evidence.

**TLH proposal requirements.** REDUCE or REPLACE for TLH must state: exact taxable account and lot/lot group; quantity and expected realized loss; wash-sale status and scope; named replacement or explicit cash destination; exposure preserved or intentionally changed; and why expected after-tax benefit exceeds spread, complexity, and opportunity cost. A usable loss alone does not clear the trade bar. Apply the owner deployment preference: require an immediately attractive replacement unless a hard-risk case independently makes cash the superior destination. Fundamental weakness is not required, but the portfolio case must stand on its own.

**Evidence.** Any company-specific fact that initiates, modifies, or reverses an action needs a source and event/publication date. Prefer filings, earnings releases, transcripts, and regulator/company sources. Label unsourced claims and forecasts as assumptions. If current evidence cannot be verified, use INSUFFICIENT EVIDENCE and do not reverse a prior proposal on that basis.

Keep the portfolio snapshot time, quote/session time, financial reporting period, and news publication/event date distinct. Label stale, mixed-session, or unreconciled inputs. A daily headline is not material unless it changes valuation, thesis, risk budget, deployability, or a decision trigger.

**New ideas.** Include **2–3 securities not currently held** as research candidates in every briefing. Favor ideas that improve diversification and do not raise direct AI/semi exposure. For each: give a **Conviction level of High / Medium / Low**, the portfolio role, current sourced thesis and catalyst, principal risk, valuation/entry discipline, an observable decision trigger, and the supplied $10k factor/cash/top-5 impact. For the strongest candidate, explain what expectations are embedded in the current valuation, what differs from those expectations, why acting now beats waiting, and the evidence period/metric definition. Compare it with the best two relevant names already held and with the other new candidates; if no relevant holding exists, say so. A research candidate is not automatically a trade: assign Portfolio Action **DEPLOY** only when cash/proceeds are available and the idea clears the trade bar; otherwise assign **NO ACTION**, mark Candidate Status **WATCH**, and state what would make it deployable. Distinguish research conviction from conviction to deploy at today's valuation. Do not invent live prices or valuation facts that cannot be verified.

# 3. Tax engine (REDUCE/REPLACE only)

A ticker is not one lot (taxable LT ≠ taxable ST ≠ IRA). Compare at least two sleeves. Pick account/lot on: risk removed · tax · thesis of what you sell · leftover factor · ST→LT optionality. Tax-free ≠ correct sleeve — do not gut IRA/Roth to spare a taxable ST lot you were not forced to touch. If the only cheap cut is IRA, say so. If the only taxable cut is a fat ST gain, prefer no trade unless #1 forces it. State: **why this ticker, account, and lot vs the next-best.** Then recompute weights, sleeves, cash, tax, $10k.

# 4. Output (this order, short)

**Daily Portfolio Action Briefing – [Today’s Date] – Live E*TRADE Book**

**0. Executive Recommendation** — PROPOSED TRADE / NO ACTION / NEEDS REVIEW · High/Medium/Low confidence · Reduce/Maintain/Add + Risk-On/Neutral/Risk-Off · proposed actions (ticker, account, lot/term, quantity, $, decision trigger, proceeds and immediate deployment) or None · proposals declined today · why in 3–5 sentences. If concentration is above a soft threshold but no replacement clears the deployment bar, explicitly prefer KEEP/NO ACTION over selling to idle cash. State: **Analytical proposal — not an approved or submitted trade.**

**1. Daily Delta + Health** — use the pre-computed portfolio and observed-account deltas; 8–12 lines on weights, quantities/account changes, P/L, ST/LT, sleeves, cash, breaches, and open reviews. Report at most three material developments; unchanged coverage belongs in the compact appendix. No 20-name tape dump. Never infer a trade or intent from a position change alone.

**2. Regime / stress** — only what changes a decision. Directional (no fake VaR): Nasdaq −10%; semi −15%; AI-capex down; rates spike; broad correction without AI damage.

**3. Decision reconciliation** — compact table: Ticker · Prior Model Proposal · Current Proposal · Continuity (UNCHANGED/MODIFIED/REVERSED/RESOLVED/NOT CAPTURED) · Observed Account Change · Qualifying Delta · Approval State. A prior proposal is not approval. No E*TRADE change means NO EXECUTION DETECTED, not “pending.”

**4. Must-analyze** — every name listed in §1. Do not omit a name because it is under {ANALYZE_WEIGHT_FLOOR:g}%; taxable-harvest and material-loss names stay in. Compact table: Ticker · Fundamental View · Portfolio Action · Confidence · Prior Proposal/Status · last · avg/cost · P/L · term/account · target wt · verified evidence · missing information · decision trigger. Apply the tax engine and two-source comparison only to REDUCE or REPLACE.

**5. New ideas** — required table with 2–3 tickers not currently held: Rank · Ticker · Conviction (High/Medium/Low) · Portfolio role · Fundamental View · Portfolio Action (DEPLOY/NO ACTION) · Candidate Status (ACTIONABLE/WATCH) · Why now (verified, dated evidence) · Valuation/entry discipline · Principal risk · Comparison with the best two relevant holdings · $10k direct/broad/cash/top-5 impact · decision trigger. Do not force a DEPLOY action when cash, evidence, valuation, or portfolio fit does not clear the trade bar.

**6. Cash / tax / post-trade** — SGOV plan (now {fmt_money(cash_mv)}). If any proposed trade: min $ vs recommended; estimated ST/LT tax; wash-sale status/scope; updated cash, top-5, direct %, broad %, $10k of the trade; and a dollar-for-dollar proceeds deployment table. Re-rank top-five membership after the proposed trade and identify the actual funding and purchasing accounts. A concentration sale may leave proceeds idle only when the hard-risk case for cash is explicit. Nasdaq beta: up / similar / down. No fake expected-return or drawdown to one decimal.

**7. Decision triggers** — only names touched, under observable review, near a limit, or listed as a new idea. Use the allowed Portfolio Action values; WATCH is a candidate status, not a Portfolio Action. One line each: the specific observable condition that would make today's proposal wrong or require review.

**Appendix — monitoring only** — one short line per unchanged must-analyze holding or candidate: Ticker · **NO MATERIAL UPDATE** · next observable trigger. Do not repeat the executive recommendation, health section, or full position tables here.
"""
    return prompt.strip() + "\n"


def main(argv=None) -> int:
    parser = ArgumentParser(description="Build a portfolio decision-support prompt.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Generate an unsafe marked prompt when an account fails.",
    )
    args = parser.parse_args(argv)
    print("Fetching live E*TRADE portfolio...")
    formatted, grand_total, npos, results, as_of = fetch_portfolio_block(
        verbose=True, allow_partial=args.allow_partial
    )
    save_portfolio_block(formatted, as_of=as_of)

    holdings, accounts = consolidate(results, grand_total)
    prompt = build_prompt(holdings, accounts, grand_total, as_of, formatted, results=results)

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
        print(
            "Clipboard:                 FULL PROMPT copied — paste into your preferred model for analysis"
        )
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

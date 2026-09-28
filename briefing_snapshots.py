"""Daily snapshots and deltas for the briefing.

Two independent records are kept per ET calendar day under ``briefings/``:

* ``weights_*.json`` — portfolio weights, sleeves and breaches, used for the
  daily *portfolio* delta.
* ``observations_*.json`` — per-account quantities and lot identity, used for
  the *observed account* delta.

Directories are passed in by the caller so this module has no hidden paths.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from briefing_formatting import fmt_weight
from get_portfolio import account_tail, atomic_write_text, is_cash_symbol
from portfolio_policy import active_policy


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


def save_weights_snapshot(snap: dict, as_of: datetime, briefings_dir: Path) -> Path:
    briefings_dir.mkdir(exist_ok=True)
    dated = briefings_dir / f"weights_{as_of.strftime('%Y-%m-%d')}.json"
    latest = briefings_dir / "weights_latest.json"
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


def save_observation_snapshot(snap: dict, as_of: datetime, briefings_dir: Path) -> Path:
    briefings_dir.mkdir(exist_ok=True)
    dated = briefings_dir / f"observations_{as_of.strftime('%Y-%m-%d')}.json"
    latest = briefings_dir / "observations_latest.json"
    text = json.dumps(snap, indent=2)
    atomic_write_text(dated, text)
    atomic_write_text(latest, text)
    return dated


def load_prior_observation(
    as_of: datetime, briefings_dir: Path
) -> tuple[dict | None, str]:
    today = as_of.strftime("%Y-%m-%d")
    paths = sorted(briefings_dir.glob("observations_20*.json"), reverse=True)
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
            if h["weight"] > active_policy().single_name_cap_pct and not is_cash_symbol(h["symbol"])
        ],
    }


def load_prior_snapshot(
    as_of: datetime, briefings_dir: Path, prompts_dir: Path
) -> tuple[dict | None, str]:
    today = as_of.strftime("%Y-%m-%d")
    json_files = sorted(briefings_dir.glob("weights_20*.json"), reverse=True)
    for path in json_files:
        if today in path.name:
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8")), path.name
        except (OSError, json.JSONDecodeError):
            continue

    prompt_files = sorted(prompts_dir.glob("daily_briefing_prompt_20*.md"), reverse=True)
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
    cap = active_policy().single_name_cap_pct
    if today_br - prior_br:
        lines.append(f"- **New {cap:g}% breach:** " + ", ".join(sorted(today_br - prior_br)))
    if prior_br - today_br:
        lines.append(f"- **{cap:g}% breach cleared:** " + ", ".join(sorted(prior_br - today_br)))
    if today_br and today_br == prior_br:
        lines.append(f"- {cap:g}% breach **unchanged:** " + ", ".join(sorted(today_br)))
    lines.append(
        "Classify these moves as market, sector, or company-specific. "
        "Do not repeat unchanged analysis."
    )
    return "\n".join(lines)


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

"""Validate a model's briefing reply against the exact portfolio data and save it.

The reply must end with the fenced ``json`` block described in section 6 of
templates/briefing_prompt.md. Every number (proceeds, realized gain, post-trade
weights) is computed here from the saved portfolio payload; nothing the model
states is trusted. Read-only: no orders are ever placed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from argparse import ArgumentParser
from datetime import date, datetime
from pathlib import Path

from build_briefing_prompt import consolidate, sleeve_stats, top5_noncash_weight
from get_portfolio import (
    LOT_QUANTITY_TOLERANCE,
    PortfolioDataError,
    account_tail,
    atomic_write_text,
    days_to_long_term,
    is_cash_symbol,
    long_term_date,
    now_et,
    parse_portable_portfolio_text,
    read_clipboard_text,
    term_from_lot,
    to_et,
)
from portfolio_policy import active_policy, load_policy, set_active_policy

PROJECT_ROOT = Path(__file__).resolve().parent
BRIEFINGS_DIR = PROJECT_ROOT / "briefings"
RESPONSES_DIR = PROJECT_ROOT / "responses"

ENGINES = ("codex", "claude", "chatgpt", "grok", "other")
VIEWS = frozenset({"ATTRACTIVE", "NEUTRAL", "UNATTRACTIVE", "INSUFFICIENT_EVIDENCE"})
ACTIONS = frozenset({"KEEP", "REDUCE", "REPLACE", "DEPLOY", "NO_ACTION"})
SIDES = frozenset({"SELL", "BUY"})
SCHEMA_VERSION = 1
LT_SOON_DAYS = 60
WASH_WINDOW_DAYS = 30

_JSON_FENCE = re.compile(r"```[ \t]*json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Checks:
    """Ordered list of ERROR/WARN findings."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def error(self, code: str, message: str) -> None:
        self.items.append({"level": "ERROR", "code": code, "message": message})

    def warn(self, code: str, message: str) -> None:
        self.items.append({"level": "WARN", "code": code, "message": message})

    @property
    def has_error(self) -> bool:
        return any(c["level"] == "ERROR" for c in self.items)


def extract_json_block(reply: str) -> str | None:
    """Return the body of the LAST fenced json block, or None."""
    blocks = _JSON_FENCE.findall(reply or "")
    return blocks[-1] if blocks else None


def _valid_date(value) -> date | None:
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _money(x: float | None) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _digits(text) -> str:
    return re.sub(r"\D", "", str(text or ""))


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _lots_of(holding: dict) -> list[dict]:
    if holding.get("lots"):
        return holding["lots"]
    if holding.get("date_acquired"):
        return [{
            "qty": holding.get("quantity") or 0.0,
            "total_cost": holding.get("total_cost") or 0.0,
            "acquired": holding["date_acquired"],
            "term_code": None,
        }]
    return []


def find_account(results: list[dict], raw_account) -> tuple[dict | None, str | None]:
    """Match an account by last-4 digits; returns (account, problem)."""
    wanted = _digits(raw_account)[-4:]
    if len(wanted) < 4:
        return None, f"account {raw_account!r} has no 4-digit tail"
    matches = [r for r in results if _digits(account_tail(r["label"]))[-4:] == wanted]
    if not matches:
        return None, f"account {raw_account!r} matches no account in the data"
    if len(matches) > 1:
        return None, f"account {raw_account!r} matches more than one account"
    return matches[0], None


def _find_holding(account: dict, ticker: str) -> dict | None:
    for holding in account["holdings"]:
        if holding["symbol"] == ticker:
            return holding
    return None


def _check_names(checks: Checks, parsed: dict, meta: dict, as_of_date: date) -> None:
    names = parsed.get("names")
    if names is None:
        names = []
    if not isinstance(names, list):
        checks.error("names_not_list", "\"names\" must be a list")
        names = []
    seen: dict[str, dict] = {}
    for entry in names:
        if not isinstance(entry, dict) or not str(entry.get("ticker") or "").strip():
            checks.error("name_bad_entry", f"names entry without a ticker: {entry!r}")
            continue
        seen[str(entry["ticker"]).upper().strip()] = entry
    for symbol in meta.get("focus_symbols") or []:
        entry = seen.get(str(symbol).upper())
        if entry is None:
            checks.error("missing_focus_name", f"focus name {symbol} has no entry in names")
            continue
        view, action = entry.get("view"), entry.get("action")
        if view not in VIEWS:
            checks.error("invalid_view", f"{symbol}: view {view!r} is not one of {sorted(VIEWS)}")
        if action not in ACTIONS:
            checks.error(
                "invalid_action", f"{symbol}: action {action!r} is not one of {sorted(ACTIONS)}"
            )
        sources = entry.get("sources")
        sources = [s for s in sources if isinstance(s, dict)] if isinstance(sources, list) else []
        dated = [_valid_date(s.get("date")) for s in sources]
        if action == "KEEP" and not any(d is not None for d in dated):
            checks.warn(
                "keep_without_dated_source",
                f"{symbol}: KEEP has no source with a valid YYYY-MM-DD date",
            )
        for d in dated:
            if d is not None and d > as_of_date:
                checks.warn(
                    "source_after_as_of",
                    f"{symbol}: source dated {d.isoformat()} is after the data date "
                    f"{as_of_date.isoformat()}",
                )


def _lot_label(lot: dict) -> str:
    return to_et(lot["acquired"]).date().isoformat() if lot.get("acquired") else "unknown date"


def _check_sell(checks, trade, account, holding, as_of, sells_by_holding, lot_used):
    tid, ticker, qty = trade["id"], trade["ticker"], trade["quantity"]
    rec = trade["record"]
    key = (account["label"], ticker)
    sold_so_far = sells_by_holding.get(key, 0.0) + qty
    if holding is None:
        checks.error("sell_not_held", f"{tid}: {ticker} is not held in {account['label']}")
        return
    if sold_so_far > holding["quantity"] + LOT_QUANTITY_TOLERANCE:
        checks.error(
            "sell_exceeds_holding",
            f"{tid}: selling {qty:g} {ticker} (total {sold_so_far:g}) but "
            f"{account['label']} holds {holding['quantity']:g}",
        )
        return
    sells_by_holding[key] = sold_so_far
    price = holding["price"]
    rec["price"] = price
    rec["amount"] = qty * price
    rec["valid"] = True
    taxable = account.get("tax_bucket") == "taxable"
    if is_cash_symbol(ticker):
        rec["tax_note"] = "cash-equivalent, no gain computed"
        rec["cash_symbol"] = True
        return
    if not taxable:
        rec["tax_note"] = "no CG tax"
        return

    acquired_text = trade.get("lot_acquired")
    wanted = _valid_date(acquired_text)
    if wanted is None:
        checks.error(
            "lot_acquired_required",
            f"{tid}: taxable SELL of {ticker} needs lot_acquired as YYYY-MM-DD "
            f"(got {acquired_text!r})",
        )
        return
    lots = _lots_of(holding)
    candidates = [
        (i, lot) for i, lot in enumerate(lots)
        if lot.get("acquired") is not None and to_et(lot["acquired"]).date() == wanted
    ]
    if not candidates:
        checks.error(
            "lot_not_found",
            f"{tid}: no {ticker} lot acquired {wanted.isoformat()} in {account['label']}",
        )
        return
    chosen = None
    for i, lot in candidates:
        used = lot_used.get((account["label"], ticker, i), 0.0)
        if used + qty <= lot["qty"] + LOT_QUANTITY_TOLERANCE:
            chosen = (i, lot, used)
            break
    if chosen is None:
        capacity = sum(lot["qty"] for _i, lot in candidates)
        checks.error(
            "sell_exceeds_lot",
            f"{tid}: selling {qty:g} {ticker} from the {wanted.isoformat()} lot exceeds its "
            f"remaining quantity (lot {capacity:g}, already sold in this proposal "
            f"{sum(lot_used.get((account['label'], ticker, i), 0.0) for i, _l in candidates):g})",
        )
        return
    i, lot, used = chosen
    lot_used[(account["label"], ticker, i)] = used + qty
    basis_per_share = lot["total_cost"] / lot["qty"]
    gain = qty * (price - basis_per_share)
    term = term_from_lot(lot, as_of)
    dtl = days_to_long_term(lot, as_of)
    rec.update({
        "lot_acquired": wanted.isoformat(),
        "basis_per_share": basis_per_share,
        "realized_gain": gain,
        "term": term,
        "days_to_long_term": dtl,
        "tax_note": "taxable",
        "lot_index": i,
    })
    if gain > 0 and term == "ST" and dtl is not None and dtl <= LT_SOON_DAYS:
        lt_date = long_term_date(lot["acquired"]).date().isoformat()
        rec["lt_date"] = lt_date
        checks.warn(
            "st_gain_turns_lt_soon",
            f"{tid}: {ticker} lot {wanted.isoformat()} sells at a short-term gain of "
            f"{_money(gain)}; it turns long-term in {dtl} days (on {lt_date})",
        )


def _check_buy(checks, trade, holdings_by_symbol):
    tid, ticker, qty = trade["id"], trade["ticker"], trade["quantity"]
    rec = trade["record"]
    held = holdings_by_symbol.get(ticker)
    price = held["price"] if held else None
    if price is None and _is_number(trade.get("est_price")) and trade["est_price"] > 0:
        price = float(trade["est_price"])
    rec["valid"] = True
    if price is None:
        checks.warn(
            "cost_not_checked",
            f"{tid}: BUY {ticker} is not held and has no est_price; cost not checked",
        )
        return
    rec["price"] = price
    rec["amount"] = qty * price


def _funding_checks(checks, buys, by_id):
    for trade in buys:
        tid = trade["id"]
        funded = trade.get("funded_by")
        if not isinstance(funded, str) or not funded.strip():
            checks.warn("funded_by_missing", f"{tid}: BUY has no funded_by")
            continue
        funded = funded.strip()
        if funded.upper() in ("CASH", "SGOV"):
            continue
        source = by_id.get(funded)
        if source is None:
            checks.error("funded_by_unknown", f"{tid}: funded_by {funded!r} is not a trade id")
        elif source["side"] != "SELL":
            checks.error("funded_by_not_sell", f"{tid}: funded_by {funded!r} is not a SELL")
        elif source.get("account_label") != trade.get("account_label"):
            checks.error(
                "funded_by_other_account",
                f"{tid}: funded by {funded} in {source.get('account_label')}, but cash cannot "
                f"move between accounts (this BUY is in {trade.get('account_label')})",
            )


def _check_trades(checks, parsed, results, as_of, holdings_by_symbol):
    raw_trades = parsed.get("trades")
    if raw_trades is None:
        raw_trades = []
    if not isinstance(raw_trades, list):
        checks.error("trades_not_list", "\"trades\" must be a list")
        raw_trades = []

    trades: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_trades, start=1):
        if not isinstance(raw, dict):
            checks.error("trade_bad_entry", f"trade #{index} is not an object")
            continue
        tid = str(raw.get("id") or f"#{index}")
        if tid in seen_ids:
            checks.error("duplicate_trade_id", f"trade id {tid} is used more than once")
        seen_ids.add(tid)
        trade = dict(raw)
        trade["id"] = tid
        trade["side"] = raw.get("side")
        trade["ticker"] = str(raw.get("ticker") or "").upper().strip()
        trade["record"] = {"valid": False}
        trades.append(trade)

    sells_by_holding: dict[tuple[str, str], float] = {}
    lot_used: dict[tuple[str, str, int], float] = {}
    by_id = {t["id"]: t for t in trades}
    structurally_ok: list[dict] = []
    for trade in trades:
        tid = trade["id"]
        if trade["side"] not in SIDES:
            checks.error("invalid_side", f"{tid}: side {trade['side']!r} is not SELL or BUY")
            continue
        if not trade["ticker"]:
            checks.error("trade_missing_ticker", f"{tid}: no ticker")
            continue
        account, problem = find_account(results, trade.get("account"))
        if account is None:
            checks.error("account_not_found", f"{tid}: {problem}")
            continue
        trade["account_label"] = account["label"]
        qty = trade.get("quantity")
        if not _is_number(qty) or not qty > 0:
            checks.error("invalid_quantity", f"{tid}: quantity {qty!r} must be a number > 0")
            continue
        trade["quantity"] = float(qty)
        trade["_account"] = account
        structurally_ok.append(trade)

    for trade in structurally_ok:
        if trade["side"] == "SELL":
            account = trade["_account"]
            holding = _find_holding(account, trade["ticker"])
            _check_sell(checks, trade, account, holding, as_of, sells_by_holding, lot_used)
        else:
            _check_buy(checks, trade, holdings_by_symbol)

    sells = [t for t in structurally_ok if t["side"] == "SELL" and t["record"]["valid"]]
    buys = [t for t in structurally_ok if t["side"] == "BUY"]

    # Cash is per account: existing cash-symbol holdings plus same-account sale proceeds.
    available: dict[str, float] = {}
    for account in results:
        available[account["label"]] = sum(
            h["market_value"] for h in account["holdings"] if is_cash_symbol(h["symbol"])
        )
    for sell in sells:
        if not sell["record"].get("cash_symbol"):
            available[sell["account_label"]] += sell["record"]["amount"]
    spent: dict[str, float] = {}
    for buy in buys:
        if buy["record"].get("amount") is not None:
            label = buy["account_label"]
            spent[label] = spent.get(label, 0.0) + buy["record"]["amount"]
    for label, cost in spent.items():
        if cost > available.get(label, 0.0) + 0.005:
            checks.error(
                "insufficient_cash",
                f"BUYs in {label} cost {_money(cost)} but only {_money(available.get(label, 0.0))} "
                "is available there (cash cannot move between accounts)",
            )
    _funding_checks(checks, buys, by_id)

    _check_wash_sales(checks, sells, buys, results, as_of)
    return trades, sells, buys, available


def _check_wash_sales(checks, sells, buys, results, as_of) -> None:
    as_of_date = to_et(as_of).date()
    for sell in sells:
        rec = sell["record"]
        gain = rec.get("realized_gain")
        if gain is None or gain >= 0 or sell["_account"].get("tax_bucket") != "taxable":
            continue
        tid, ticker = sell["id"], sell["ticker"]
        for buy in buys:
            if buy["ticker"] == ticker:
                checks.error(
                    "wash_sale_buy_in_proposal",
                    f"{tid}: sells {ticker} at a loss of {_money(gain)} while {buy['id']} buys "
                    f"{ticker} in {buy['account_label']}: wash sale",
                )
        recent = []
        for account in results:
            holding = _find_holding(account, ticker)
            if holding is None:
                continue
            for i, lot in enumerate(_lots_of(holding)):
                if lot.get("acquired") is None:
                    continue
                if account["label"] == sell["account_label"] and i == rec.get("lot_index"):
                    continue  # the lot being sold is not its own replacement
                age = (as_of_date - to_et(lot["acquired"]).date()).days
                if 0 <= age <= WASH_WINDOW_DAYS:
                    recent.append(f"{account['label']} lot {_lot_label(lot)}")
        if recent:
            checks.warn(
                "wash_sale_possible",
                f"{tid}: POSSIBLE wash sale: {ticker} was bought within 30 days before "
                f"the data date ({'; '.join(recent)})",
            )
        checks.warn(
            "wash_sale_verify",
            f"{tid}: loss sale of {ticker} ({_money(gain)}). Owner must verify: {ticker} "
            "purchases in the 30 days after the sale, spouse accounts, dividend reinvestment, "
            "and other brokers",
        )


def _weights(holdings, total, symbols, policy):
    direct = policy.sleeves["direct_ai_semi"]
    broad = policy.sleeves["broad_ai_cycle"]
    pseudo = [{"symbol": s, "market_value": v} for s, v in holdings.items()]
    return {
        "direct_ai_semi": sleeve_stats(pseudo, direct, total)[1],
        "broad_ai_cycle": sleeve_stats(pseudo, broad, total)[1],
        "top5_noncash": top5_noncash_weight(holdings, total),
        "tickers": {
            s: (holdings.get(s, 0.0) / total * 100 if total else 0.0) for s in symbols
        },
    }


def _check_weights(checks, sells, buys, grand_total, policy, results):
    # Non-cash market value per symbol across accounts, before and after.
    before: dict[str, float] = {}
    cash_before = 0.0
    for account in results:
        for holding in account["holdings"]:
            if is_cash_symbol(holding["symbol"]):
                cash_before += holding["market_value"]
            else:
                symbol = holding["symbol"]
                before[symbol] = before.get(symbol, 0.0) + holding["market_value"]
    after = dict(before)
    cash_after = cash_before
    for trade in sells + buys:
        rec = trade["record"]
        amount = rec.get("amount")
        if amount is None or rec.get("cash_symbol"):
            continue
        if trade["side"] == "SELL":
            after[trade["ticker"]] = after.get(trade["ticker"], 0.0) - amount
            cash_after += amount
        else:
            after[trade["ticker"]] = after.get(trade["ticker"], 0.0) + amount
            cash_after -= amount

    traded = sorted({t["ticker"] for t in sells + buys if not is_cash_symbol(t["ticker"])})
    w_before = _weights(before, grand_total, traded, policy)
    w_after = _weights(after, grand_total, traded, policy)
    limit = policy.cluster_do_not_increase_pct
    direct = set(policy.sleeves["direct_ai_semi"])
    for buy in buys:
        if buy["ticker"] in direct and (
            w_before["direct_ai_semi"] >= limit or w_after["direct_ai_semi"] >= limit
        ):
            checks.error(
                "direct_sleeve_no_add",
                f"{buy['id']}: BUY {buy['ticker']} adds to the direct AI/semi sleeve, which is "
                f"{w_before['direct_ai_semi']:.2f}% before and {w_after['direct_ai_semi']:.2f}% "
                f"after (no-add limit {limit:g}%)",
            )
    cap = policy.single_name_cap_pct
    for ticker in sorted({b["ticker"] for b in buys}):
        weight = w_after["tickers"].get(ticker, 0.0)
        if weight > cap:
            checks.warn(
                "single_name_cap_exceeded",
                f"{ticker} would be {weight:.2f}% after the trades (single-name cap {cap:g}%)",
            )

    def pct(value: float) -> float:
        return value / grand_total * 100 if grand_total else 0.0

    return {
        "total_value": grand_total,
        "tickers": {
            t: {"before": w_before["tickers"][t], "after": w_after["tickers"][t]} for t in traded
        },
        "sleeves": {
            name: {"before": w_before[name], "after": w_after[name]}
            for name in ("direct_ai_semi", "broad_ai_cycle")
        },
        "top5_noncash": {"before": w_before["top5_noncash"], "after": w_after["top5_noncash"]},
        "cash": {"before": pct(cash_before), "after": pct(cash_after)},
    }


def _tax_summary(sells) -> dict:
    st_gain = lt_gain = losses = 0.0
    for sell in sells:
        gain = sell["record"].get("realized_gain")
        if gain is None:
            continue
        if gain < 0:
            losses += gain
        elif sell["record"].get("term") == "LT":
            lt_gain += gain
        else:
            st_gain += gain  # an unknown term is treated as short-term
    return {
        "taxable_st_gain": st_gain,
        "taxable_lt_gain": lt_gain,
        "taxable_losses": losses,
        "taxable_net": st_gain + lt_gain + losses,
    }


def validate_reply(
    reply: str, meta: dict, results: list[dict], grand_total: float, as_of: datetime
):
    """Return (checks, parsed, computed). Never raises on bad model output."""
    checks = Checks()
    computed: dict = {"trades": [], "weights": None, "tax_summary": None}
    block = extract_json_block(reply)
    if block is None:
        checks.error("no_json_block", "no fenced ```json block found in the reply")
        return checks, None, computed
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        checks.error("invalid_json", f"the final json block is not valid JSON: {exc}")
        return checks, None, computed
    if not isinstance(parsed, dict):
        checks.error("json_not_object", "the final json block must be a JSON object")
        return checks, None, computed

    policy = active_policy()
    as_of_date = to_et(as_of).date()
    if parsed.get("briefing_id") != meta.get("briefing_id"):
        checks.error(
            "briefing_id_mismatch",
            f"briefing_id {parsed.get('briefing_id')!r} does not match "
            f"{meta.get('briefing_id')!r}: "
            "the reply is for a different prompt or data",
        )
    if parsed.get("web_research") is not True:
        checks.warn("web_research_not_confirmed", "web_research is not true; claims are unsourced")
        if parsed.get("trades"):
            checks.error(
                "web_research_required_for_trades",
                "trades were proposed without web_research: true",
            )

    _check_names(checks, parsed, meta, as_of_date)
    holdings, _accounts = consolidate(results, grand_total)
    holdings_by_symbol = {h["symbol"]: h for h in holdings}
    trades, sells, buys, available = _check_trades(
        checks, parsed, results, as_of, holdings_by_symbol
    )
    computed["weights"] = _check_weights(
        checks, sells, buys, grand_total, policy, results
    )
    computed["tax_summary"] = _tax_summary(sells)
    computed["cash_available_by_account"] = available
    for trade in trades:
        rec = {k: v for k, v in trade["record"].items() if k != "lot_index"}
        rec.update({
            "id": trade["id"],
            "side": trade["side"],
            "ticker": trade["ticker"],
            "account": trade.get("account_label") or trade.get("account"),
            "quantity": trade.get("quantity"),
        })
        computed["trades"].append(rec)
    return checks, parsed, computed


def status_of(checks: Checks) -> str:
    return "REJECTED" if checks.has_error else "ACCEPTED"


def _unique_stem(directory: Path, stem: str) -> str:
    candidate, n = stem, 1
    while (directory / f"{candidate}.md").exists() or (directory / f"{candidate}.json").exists():
        n += 1
        candidate = f"{stem}_{n}"
    return candidate


def save_response(
    reply, meta, engine, status, parsed, checks, computed, date_str
) -> tuple[Path, Path]:
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    stem = _unique_stem(RESPONSES_DIR, f"{date_str}_{engine}")
    md_path = RESPONSES_DIR / f"{stem}.md"
    json_path = RESPONSES_DIR / f"{stem}.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "briefing_id": meta.get("briefing_id"),
        "engine": engine,
        "imported_at": now_et().isoformat(),
        "status": status,
        "reply_sha256": hashlib.sha256(reply.encode("utf-8")).hexdigest(),
        "parsed": parsed,
        "checks": checks.items,
        "computed": computed,
    }
    atomic_write_text(md_path, reply)
    atomic_write_text(json_path, json.dumps(payload, indent=2, ensure_ascii=False))
    return md_path, json_path


def format_report(status, checks, computed, engine, date_str) -> str:
    lines = [f"Import of {engine} reply for {date_str}: {status}", ""]
    for level in ("ERROR", "WARN"):
        items = [c for c in checks.items if c["level"] == level]
        lines.append(f"{level}S ({len(items)})")
        lines.extend(f"  [{c['code']}] {c['message']}" for c in items)
        if not items:
            lines.append("  none")
        lines.append("")
    trades = computed.get("trades") or []
    if trades:
        lines.append("COMPUTED TRADES")
        for t in trades:
            amount = _money(t.get("amount"))
            row = (
                f"  {t['id']}: {t['side']} {t.get('quantity'):g} {t['ticker']} in {t['account']}"
                f" @ {_money(t.get('price'))} = {amount}"
                if t.get("quantity") is not None
                else f"  {t['id']}: {t['side']} {t['ticker']} (not computed)"
            )
            if t.get("realized_gain") is not None:
                row += (
                    f" | lot {t.get('lot_acquired')}, realized {_money(t['realized_gain'])} "
                    f"{t.get('term')}"
                )
                if t.get("days_to_long_term") is not None:
                    row += f", LT in {t['days_to_long_term']}d"
            elif t.get("tax_note"):
                row += f" | {t['tax_note']}"
            lines.append(row)
        lines.append("")
    summary = computed.get("tax_summary")
    if summary and trades:
        lines.append("TAXABLE REALIZED SUMMARY")
        lines.append(f"  ST gain {_money(summary['taxable_st_gain'])}, "
                     f"LT gain {_money(summary['taxable_lt_gain'])}, "
                     f"losses {_money(summary['taxable_losses'])}, "
                     f"net {_money(summary['taxable_net'])}")
        lines.append("")
    weights = computed.get("weights")
    if weights and trades:
        lines.append("WEIGHTS BEFORE -> AFTER")
        for ticker, w in weights["tickers"].items():
            lines.append(f"  {ticker}: {w['before']:.2f}% -> {w['after']:.2f}%")
        for name, w in weights["sleeves"].items():
            lines.append(f"  sleeve {name}: {w['before']:.2f}% -> {w['after']:.2f}%")
        top5 = weights["top5_noncash"]
        lines.append(f"  top-5 non-cash: {top5['before']:.2f}% -> {top5['after']:.2f}%")
        cash = weights["cash"]
        lines.append(f"  cash: {cash['before']:.2f}% -> {cash['after']:.2f}%")
        lines.append("")
    return "\n".join(lines)


def _read_reply(args) -> str:
    if args.from_clipboard:
        return read_clipboard_text()
    if args.input_file == "-":
        return sys.stdin.read()
    raw = Path(args.input_file).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def _load_briefing(date_arg: str | None) -> tuple[dict, str]:
    name = f"briefing_{date_arg}.json" if date_arg else "briefing_latest.json"
    path = BRIEFINGS_DIR / name
    if not path.exists():
        raise PortfolioDataError(f"{path} not found; run build_briefing_prompt.py first")
    meta = json.loads(path.read_text(encoding="utf-8"))
    date_str = date_arg or str(meta.get("as_of") or "")[:10]
    if not _valid_date(date_str):
        raise PortfolioDataError(f"cannot determine the briefing date from {path}")
    return meta, date_str


def main(argv=None) -> int:
    parser = ArgumentParser(
        description="Validate a model's briefing reply against the portfolio data and save it."
    )
    parser.add_argument("--engine", required=True, choices=ENGINES)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-clipboard", action="store_true", help="Read the reply from the clipboard."
    )
    source.add_argument("--input-file", help="Read the reply from a file, or '-' for stdin.")
    parser.add_argument("--date", help="Briefing date YYYY-MM-DD (default: briefing_latest.json).")
    parser.add_argument(
        "--policy", type=Path, help="Use this policy JSON instead of portfolio_policy.json."
    )
    args = parser.parse_args(argv)
    if args.date and not _valid_date(args.date):
        parser.error("--date must be YYYY-MM-DD")
    if args.policy:
        set_active_policy(load_policy(args.policy))
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        meta, date_str = _load_briefing(args.date)
        portfolio_path = BRIEFINGS_DIR / str(meta.get("portfolio_file") or "")
        text = portfolio_path.read_text(encoding="utf-8")
        _formatted, grand_total, _n, results, as_of = parse_portable_portfolio_text(text)
        reply = _read_reply(args)
    except (PortfolioDataError, OSError, ValueError) as exc:
        print(f"Cannot import: {exc}", file=sys.stderr)
        return 2

    checks, parsed, computed = validate_reply(reply, meta, results, grand_total, as_of)
    status = status_of(checks)
    md_path, json_path = save_response(
        reply, meta, args.engine, status, parsed, checks, computed, date_str
    )
    print(format_report(status, checks, computed, args.engine, date_str))
    print(f"Saved reply:  {md_path}")
    print(f"Saved checks: {json_path}")
    return 1 if checks.has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())

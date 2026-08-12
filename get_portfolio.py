#!/usr/bin/env python3
"""
Daily portfolio fetcher for the Grok briefing.
Pulls live positions from E*TRADE and prints a clean block ready to paste.
By default fetches ALL active accounts. Set ETRADE_ACCOUNT_ID_KEY to limit to one.

Also importable: fetch_portfolio_block() used by build_briefing_prompt.py

READ-ONLY SAFEGUARD
-------------------
This module only uses pyetrade.ETradeAccounts for:
  - list_accounts
  - get_account_portfolio
  - get_account_balance
  - get portfolio tax lots
It never imports or constructs ETradeOrder / market order helpers.
No preview, place, cancel, or change-order calls exist in this repo.
"""

from __future__ import annotations

import os
import json
import logging
import subprocess
import time
from argparse import ArgumentParser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import pyetrade
from portfolio_policy import POLICY

# Explicit allow-list: only the Accounts API surface is used (list + portfolio).
# Do not import or construct pyetrade.ETradeOrder — trading is intentionally unsupported.
_ALLOWED_PYETRADE_TYPES = (pyetrade.ETradeAccounts,)

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent

try:
    ET = ZoneInfo("America/New_York")
except Exception:
    ET = datetime.now().astimezone().tzinfo

CASH_SYMBOLS = POLICY.cash_symbols
# Residual NAV-vs-mark gaps below this are noise, not sweep cash.
CASH_RESIDUAL_FLOOR = 1.0
LOGGER = logging.getLogger(__name__)

class PortfolioDataError(RuntimeError):
    """The API response was incomplete or could not be validated."""


class IncompletePortfolioError(PortfolioDataError):
    """One or more selected accounts could not be loaded completely."""


def _first(d, *keys, default=None):
    """Return the first present value, trying exact then case-insensitive keys."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    lower_map = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = lower_map.get(str(k).lower())
        if v is not None:
            return v
    return default


def _as_float(val, default=None):
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def is_cash_symbol(symbol: str) -> bool:
    normalized = (symbol or "").upper()
    return normalized in CASH_SYMBOLS or "GOVERNMENT" in normalized


def now_et() -> datetime:
    """Return an aware timestamp in E*TRADE calendar timezone."""
    return datetime.now(ET)


def _status_code(exc: Exception) -> int | None:
    return getattr(getattr(exc, "response", None), "status_code", None)


def _retry_read(call, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:
            status = _status_code(exc)
            transient = status is None or status == 429 or (status is not None and status >= 500)
            if not transient or attempt == attempts:
                raise
            LOGGER.warning("Transient E*TRADE read failed (%s/%s): %s", attempt, attempts, exc)
            time.sleep(0.25 * (2 ** (attempt - 1)))


def to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        return dt.replace(tzinfo=local_tz).astimezone(ET)
    return dt.astimezone(ET)


def parse_etrade_date(raw) -> datetime | None:
    """Parse E*TRADE epoch seconds/ms (or a date string) to an ET datetime."""
    if raw in (None, "", 0, "0"):
        return None
    if isinstance(raw, datetime):
        return to_et(raw)
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n > 1e11:  # milliseconds
        n /= 1000.0
    if n < 1e9:  # before ~2001 — treat as missing/junk
        return None
    return datetime.fromtimestamp(n, tz=ET)


def fmt_acq_date(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return to_et(dt).strftime("%Y-%m-%d")


def anniversary_plus_one_year(dt: datetime) -> datetime:
    try:
        return dt.replace(year=dt.year + 1)
    except ValueError:
        return dt.replace(year=dt.year + 1, day=28)


def holding_period(acquired: datetime | None, as_of: datetime) -> str:
    """IRS long-term = held more than one year. Exactly one year is still ST."""
    if acquired is None:
        return "unknown"
    as_of_et = to_et(as_of)
    acq_et = to_et(acquired)
    if as_of_et.date() > anniversary_plus_one_year(acq_et).date():
        return "LT"
    return "ST"


def tax_bucket_from_account(acct: dict) -> str:
    blob = " ".join(
        str(acct.get(k) or "")
        for k in ("accountType", "accountDesc", "accountName", "accountMode")
    ).upper()
    if "ROTH" in blob:
        return "roth"
    retirement = ("IRA", "CONTRIBUTORY", "TRADITIONAL", "401K", "401(K)", "403B", "403(B)", "SEP", "SIMPLE", "PENSION", "RETIREMENT", "COVERDELL", "MONEY_PURCHASE", "PROFIT_SHARING", "INDIVIDUAL_K")
    if any(marker in blob for marker in retirement):
        return "traditional"
    return "taxable"


def tax_bucket_label(bucket: str) -> str:
    return {
        "taxable": "taxable",
        "traditional": "Traditional IRA (no CG tax)",
        "roth": "Roth (no CG tax)",
    }.get(bucket, bucket)


def extract_lots(pos: dict) -> list[dict]:
    raw = _first(pos, "PositionLot", "positionLot", default=[]) or []
    if isinstance(raw, dict):
        raw = [raw]
    lots = []
    for lot in raw:
        if not isinstance(lot, dict):
            continue
        qty = _as_float(_first(lot, "remainingQty", "availableQty", "originalQty"), 0.0) or 0.0
        if qty <= 0:
            continue
        term_code = _as_float(_first(lot, "termCode"), default=None)
        lots.append({
            "qty": qty,
            "price": _as_float(_first(lot, "price", "adjPrice")),
            "total_cost": _as_float(_first(lot, "totalCost"), 0.0) or 0.0,
            "total_gain": _as_float(_first(lot, "totalGain"), 0.0) or 0.0,
            "market_value": _as_float(_first(lot, "marketValue"), 0.0) or 0.0,
            "acquired": parse_etrade_date(_first(lot, "acquiredDate", "dateAcquired")),
            "term_code": int(term_code) if term_code is not None else None,
        })
    return lots


def term_from_lot(lot: dict, as_of: datetime) -> str:
    """Prefer E*TRADE termCode (1=LT, 2=ST) — that is what a sale will be reported as."""
    code = lot.get("term_code")
    if code == 1:
        return "LT"
    if code == 2:
        return "ST"
    return holding_period(lot.get("acquired"), as_of)


def parse_lots_response(raw) -> list[dict]:
    lots = None
    if isinstance(raw, dict):
        inner = raw.get("PositionLotsResponse") or raw
        lots = _first(inner, "PositionLot", "positionLot", default=[])
    if isinstance(lots, dict):
        lots = [lots]
    return extract_lots({"PositionLot": lots or []})


def fetch_position_lots(accounts_api, lots_url: str) -> list[dict]:
    """READ: tax lots for one position."""
    def request():
        response = accounts_api.session.get(lots_url, timeout=15)
        response.raise_for_status()
        return parse_lots_response(response.json())

    return _retry_read(request)


def attach_lots(accounts_api, holdings, verbose: bool = False, strict: bool = True) -> None:
    """Fill holding['lots'] from lotsDetails when the portfolio view omitted them."""
    pending = [
        h for h in holdings
        if not h.get("lots")
        and h.get("lots_details")
        and not is_cash_symbol(h.get("symbol") or "")
    ]
    if verbose and pending:
        print(f"  Fetching tax lots for {len(pending)} position(s)...")
    failures = []
    for h in pending:
        try:
            h["lots"] = fetch_position_lots(accounts_api, h["lots_details"])
        except Exception as exc:
            failures.append(f"{h.get('symbol')}: {exc}")
            if verbose:
                print(f"  Could not load lots for {h.get('symbol')}: {exc}")
    if failures and strict:
        raise PortfolioDataError("Could not load required tax lots: " + "; ".join(failures))


def lot_term_split(holding: dict, as_of: datetime) -> dict:
    """Classify market value / gain as ST, LT, or unknown."""
    lots = holding.get("lots") or []
    st_mv = lt_mv = unk_mv = 0.0
    st_gain = lt_gain = 0.0
    dates: list[datetime] = []

    if lots:
        lot_mv_sum = sum((lot.get("market_value") or 0.0) for lot in lots)
        lot_qty_sum = sum((lot.get("qty") or 0.0) for lot in lots)
        pos_mv = holding.get("market_value") or 0.0
        pos_gain = holding.get("total_gain") or 0.0
        for lot in lots:
            mv = lot.get("market_value") or 0.0
            gain = lot.get("total_gain") or 0.0
            if lot_mv_sum <= 0 and lot_qty_sum:
                share = (lot.get("qty") or 0.0) / lot_qty_sum
                mv = pos_mv * share
                gain = pos_gain * share
            acq = lot.get("acquired")
            term = term_from_lot(lot, as_of)
            if acq is not None:
                dates.append(acq)
            if term == "LT":
                lt_mv += mv
                lt_gain += gain
            elif term == "ST":
                st_mv += mv
                st_gain += gain
            else:
                unk_mv += mv
        if not dates and holding.get("date_acquired"):
            return lot_term_split({**holding, "lots": []}, as_of)
    else:
        acq = holding.get("date_acquired")
        term = holding_period(acq, as_of)
        mv = holding.get("market_value") or 0.0
        gain = holding.get("total_gain") or 0.0
        if acq is not None:
            dates.append(acq)
        if term == "LT":
            lt_mv, lt_gain = mv, gain
        elif term == "ST":
            st_mv, st_gain = mv, gain
        else:
            unk_mv = mv

    if lt_mv > 0 and st_mv > 0:
        label = "mixed"
    elif lt_mv > 0:
        label = "LT"
    elif st_mv > 0:
        label = "ST"
    else:
        label = "unknown"

    return {
        "term": label,
        "st_mv": st_mv,
        "lt_mv": lt_mv,
        "unk_mv": unk_mv,
        "st_gain": st_gain,
        "lt_gain": lt_gain,
        "earliest": min(dates) if dates else None,
        "latest": max(dates) if dates else None,
    }


def fmt_signed_money(x: float) -> str:
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    ax = abs(x)
    if ax >= 1_000_000:
        body = f"${ax / 1_000_000:.2f}M".replace(".00M", "M")
        return f"{sign}{body}"
    return f"{sign}${ax:,.0f}"


def fmt_signed_pct(x: float) -> str:
    sign = "+" if x > 0 else ("-" if x < 0 else "")
    ax = abs(x)
    if ax >= 10:
        return f"{sign}{ax:.0f}%"
    return f"{sign}{ax:.1f}%"


def fmt_acq_range(earliest: datetime | None, latest: datetime | None) -> str | None:
    a = fmt_acq_date(earliest)
    b = fmt_acq_date(latest)
    if not a and not b:
        return None
    if a and b and a != b:
        return f"{a}→{b}"
    return a or b


def avg_cost_per_share(holding: dict) -> float | None:
    cps = holding.get("cost_per_share") or holding.get("price_paid")
    if cps:
        return cps
    qty = holding.get("quantity") or 0
    tc = holding.get("total_cost")
    if qty and tc:
        return tc / qty
    return None


def gain_pct(holding: dict) -> float | None:
    if holding.get("total_gain_pct") is not None:
        return holding["total_gain_pct"]
    tc = holding.get("total_cost")
    tg = holding.get("total_gain")
    if tc and tg is not None:
        return 100.0 * tg / tc
    return None


def format_cost_suffix(
    holding: dict,
    as_of: datetime,
    tax_bucket: str = "taxable",
) -> str:
    """Compact cost / P/L / holding-period suffix for a holding line."""
    if is_cash_symbol(holding.get("symbol") or ""):
        return ""

    parts: list[str] = []
    cps = avg_cost_per_share(holding)
    tc = holding.get("total_cost")
    tg = holding.get("total_gain")
    tgp = gain_pct(holding)
    paid = holding.get("price_paid")

    if cps:
        parts.append(f"avg ${cps:,.2f}")
    elif paid:
        parts.append(f"paid ${paid:,.2f}")
    if tc:
        parts.append(f"cost ${tc:,.0f}")
    if tg is not None and (tc or tg != 0):
        if tgp is not None:
            parts.append(f"P/L {fmt_signed_money(tg)} / {fmt_signed_pct(tgp)}")
        else:
            parts.append(f"P/L {fmt_signed_money(tg)}")

    split = lot_term_split(holding, as_of)
    acq = fmt_acq_range(split["earliest"], split["latest"])

    buckets = holding.get("tax_buckets")
    if buckets and len(buckets) > 1:
        loc = []
        if holding.get("taxable_mv"):
            loc.append(f"taxable ${holding['taxable_mv']:,.0f}")
        if holding.get("ira_mv"):
            loc.append(f"IRA ${holding['ira_mv']:,.0f}")
        if holding.get("roth_mv"):
            loc.append(f"Roth ${holding['roth_mv']:,.0f}")
        if loc:
            parts.append(" + ".join(loc))
        if acq:
            parts.append(f"acq {acq} (see per-account lots for ST/LT)")
    elif tax_bucket in ("traditional", "roth"):
        loc = "IRA — no CG tax" if tax_bucket == "traditional" else "Roth — no CG tax"
        if acq:
            parts.append(f"acq {acq} ({loc})")
        else:
            parts.append(f"({loc})")
    else:
        if split["term"] == "mixed" and acq:
            parts.append(
                f"acq {acq}  LT ${split['lt_mv']:,.0f} / ST ${split['st_mv']:,.0f}"
            )
        elif split["term"] in ("ST", "LT") and acq:
            parts.append(f"acq {acq} {split['term']}")
        elif acq:
            parts.append(f"acq {acq}")
        else:
            parts.append("holding period unknown")

    if not parts:
        return " | cost basis unavailable"
    return " | " + "  ".join(parts)


def format_taxable_cost_table(results, as_of: datetime) -> str:
    """Markdown table of taxable positions for the briefing prompt."""
    header = (
        "| Ticker | Account | Last | Avg cost | Price paid | Total cost | "
        "Unrealized P/L | Term | Acquired |\n"
        "|---|---|---:|---:|---:|---:|---:|---|---|"
    )
    rows = []
    for r in results:
        if r.get("error") or r.get("tax_bucket") != "taxable":
            continue
        label = r.get("label") or ""
        acct = label[label.find("…"):] if "…" in label else label
        acct = acct.rstrip(")")
        for h in r.get("holdings") or []:
            if is_cash_symbol(h["symbol"]):
                continue
            split = lot_term_split(h, as_of)
            cps = avg_cost_per_share(h)
            paid = h.get("price_paid")
            tc = h.get("total_cost")
            tg = h.get("total_gain")
            tgp = gain_pct(h)
            last = h.get("price") or 0
            pl = "—"
            if tg is not None:
                pl = fmt_signed_money(tg)
                if tgp is not None:
                    pl = f"{pl} ({fmt_signed_pct(tgp)})"
            term = split["term"]
            if term == "mixed":
                term = f"mixed LT ${split['lt_mv']:,.0f} / ST ${split['st_mv']:,.0f}"
            acq = fmt_acq_range(split["earliest"], split["latest"]) or "—"
            rows.append(
                f"| {h['symbol']} | {acct} | ${last:,.2f} | "
                f"{f'${cps:,.2f}' if cps else '—'} | "
                f"{f'${paid:,.2f}' if paid else '—'} | "
                f"{f'${tc:,.0f}' if tc else '—'} | "
                f"{pl} | {term} | {acq} |"
            )
    if not rows:
        return "_No taxable equity positions with reportable cost basis._"
    return header + "\n" + "\n".join(rows)


def collect_tax_flags(results, as_of: datetime) -> list[str]:
    """Pre-computed P/L and ST/LT flags for the briefing Health Check."""
    st_gains = []
    losses = []
    lt_gains = []
    missing = []

    for r in results:
        if r.get("error"):
            continue
        bucket = r.get("tax_bucket") or "taxable"
        for h in r.get("holdings") or []:
            if is_cash_symbol(h["symbol"]):
                continue
            split = lot_term_split(h, as_of)
            tg = h.get("total_gain")
            name = h["symbol"]
            if bucket != "taxable":
                continue
            if h.get("total_cost") in (None, 0) and not h.get("price_paid"):
                missing.append(name)
                continue
            if split["st_gain"] > 500 or (
                split["term"] == "ST" and tg is not None and tg > 500
            ):
                st_gains.append(
                    f"{name} {fmt_signed_money(split['st_gain'] or (tg or 0))} ST"
                )
            if tg is not None and tg < POLICY.harvest_loss_dollars:
                losses.append(f"{name} {fmt_signed_money(tg)}")
            if split["lt_gain"] > 2000 or (
                split["term"] == "LT" and tg is not None and tg > 2000
            ):
                lt_gains.append(
                    f"{name} {fmt_signed_money(split['lt_gain'] or (tg or 0))} LT"
                )

    flags = []
    if st_gains:
        flags.append(
            "- **Taxable short-term unrealized gains** (selling now = short-term "
            "capital gains tax): " + "; ".join(st_gains)
        )
    if lt_gains:
        flags.append(
            "- **Taxable long-term unrealized gains** (prefer these if de-risking "
            "in the taxable account): " + "; ".join(lt_gains)
        )
    if losses:
        flags.append(
            "- **Taxable unrealized losses** (tax-loss harvest candidates if the "
            "thesis is weak): " + "; ".join(losses)
        )
    if missing:
        flags.append(
            "- **Cost basis missing** on taxable: "
            + ", ".join(sorted(set(missing)))
            + " — do not invent a purchase price."
        )
    flags.append(
        "- Traditional IRA / Roth P/L is **economic only** — do **not** apply "
        "ST/LT capital-gains tax to Trim/Sell in those accounts."
    )
    return flags


def _credentials():
    consumer_key = os.getenv("ETRADE_CONSUMER_KEY")
    consumer_secret = os.getenv("ETRADE_CONSUMER_SECRET")
    access_token = os.getenv("ETRADE_ACCESS_TOKEN")
    access_token_secret = os.getenv("ETRADE_ACCESS_TOKEN_SECRET")
    dev = os.getenv("ETRADE_DEV", "false").lower() == "true"
    account_id_key = os.getenv("ETRADE_ACCOUNT_ID_KEY") or None

    if not all([consumer_key, consumer_secret, access_token, access_token_secret]):
        raise ValueError(
            "Missing credentials in .env.\n"
            "Set ETRADE_CONSUMER_KEY and ETRADE_CONSUMER_SECRET, then run "
            "python etrade_auth.py (browser + verification code). "
            "That script updates the daily access tokens in .env. "
            "Tokens expire at midnight US Eastern — re-run etrade_auth.py to refresh."
        )

    return {
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
        "access_token": access_token,
        "access_token_secret": access_token_secret,
        "dev": dev,
        "account_id_key": account_id_key,
    }


def get_accounts_api():
    """Return a READ-ONLY accounts client (list + portfolio only)."""
    c = _credentials()
    client = pyetrade.ETradeAccounts(
        c["consumer_key"],
        c["consumer_secret"],
        c["access_token"],
        c["access_token_secret"],
        dev=c["dev"],
    )
    # Runtime check: only Accounts client type is allowed in this project path.
    if not isinstance(client, _ALLOWED_PYETRADE_TYPES):
        raise RuntimeError("Refusing non-accounts E*TRADE client (read-only policy).")
    return client, c


def list_accounts(accounts_api=None):
    """READ: list brokerage accounts."""
    if accounts_api is None:
        accounts_api, _ = get_accounts_api()
    return _retry_read(lambda: accounts_api.list_accounts(resp_format="json"))


def get_account_balance(account_id_key: str, account_type: str, accounts_api=None):
    """READ: get authoritative account value, including actual cash."""
    if accounts_api is None:
        accounts_api, _ = get_accounts_api()
    return _retry_read(lambda: accounts_api.get_account_balance(
        account_id_key, account_type=account_type or None,
        real_time=True, resp_format="json",
    ))


def _find_numeric_key(value, keys: tuple[str, ...]) -> float | None:
    if isinstance(value, dict):
        lower = {str(k).lower(): v for k, v in value.items()}
        for key in keys:
            found = _as_float(lower.get(key.lower()))
            if found is not None:
                return found
        for child in value.values():
            found = _find_numeric_key(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_numeric_key(child, keys)
            if found is not None:
                return found
    return None


def _direct_numeric(value, *keys) -> float | None:
    if not isinstance(value, dict):
        return None
    return _as_float(_first(value, *keys))


def parse_account_cash(resp: dict) -> float | None:
    """Settled cash + money-market sweep from the balance payload, or None if absent."""
    if not isinstance(resp, dict) or "BalanceResponse" not in resp:
        return None
    balance = resp["BalanceResponse"]
    computed = _first(balance, "Computed", "computed") or {}
    cash_obj = _first(balance, "Cash", "cash") or {}

    cash_balance = _direct_numeric(computed, "cashBalance")
    if cash_balance is None:
        cash_balance = _direct_numeric(cash_obj, "cashBalance")
    mmkt = _direct_numeric(computed, "moneyMktBalance")
    if mmkt is None:
        mmkt = _direct_numeric(cash_obj, "moneyMktBalance")
    if cash_balance is not None or mmkt is not None:
        return (cash_balance or 0.0) + (mmkt or 0.0)

    net_cash = _direct_numeric(computed, "netCash")
    if net_cash is None:
        net_cash = _direct_numeric(cash_obj, "netCash")
    return net_cash


def cash_holding(amount: float) -> dict:
    return {
        "symbol": "CASH",
        "quantity": amount,
        "price": 1.0,
        "market_value": amount,
        "price_paid": None,
        "cost_per_share": None,
        "total_cost": None,
        "total_gain": None,
        "total_gain_pct": None,
        "date_acquired": None,
        "position_id": None,
        "lots_details": None,
        "lots": [],
    }


def apply_cash_lot(
    holdings: list[dict],
    *,
    account_total: float,
    position_total: float,
    cash_from_balance: float | None,
) -> None:
    """Attach sweep cash. Do not treat NAV-versus-mark residuals as cash."""
    if cash_from_balance is not None:
        if abs(cash_from_balance) >= 0.01:
            holdings.append(cash_holding(cash_from_balance))
            holdings.sort(key=lambda h: h["market_value"], reverse=True)
        return
    residual = account_total - position_total
    if residual >= CASH_RESIDUAL_FLOOR:
        holdings.append(cash_holding(residual))
        holdings.sort(key=lambda h: h["market_value"], reverse=True)


def parse_account_balance(resp: dict) -> float:
    if not isinstance(resp, dict) or "BalanceResponse" not in resp:
        raise PortfolioDataError("Unrecognized account balance response")
    balance = resp["BalanceResponse"]
    total = _find_numeric_key(balance, ("totalAccountValue",))
    if total is not None:
        return total
    net_mv = _find_numeric_key(balance, ("netMv",))
    net_cash = _find_numeric_key(balance, ("netCash",))
    if net_mv is not None and net_cash is not None:
        return net_mv + net_cash
    raise PortfolioDataError(
        "Account balance response has neither totalAccountValue nor netMv + netCash"
    )


def parse_account_list(resp):
    """Return a normalized list of account dictionaries."""
    try:
        accounts = resp["AccountListResponse"]["Accounts"]["Account"]
    except (KeyError, TypeError) as exc:
        raise PortfolioDataError("Unrecognized account list response") from exc
    if isinstance(accounts, dict):
        accounts = [accounts]
    if not isinstance(accounts, list) or any(not isinstance(a, dict) for a in accounts):
        raise PortfolioDataError("Account list did not contain account objects")
    return accounts


def get_portfolio(account_id_key: str, accounts_api=None, lots_required: bool = True):
    """READ: portfolio positions for one account. Never places orders."""
    if accounts_api is None:
        accounts_api, _ = get_accounts_api()
    kwargs = dict(
        count=100,
        totals_required=True,
        view="COMPLETE",
        lots_required=lots_required,
        resp_format="json",
    )
    try:
        return _retry_read(lambda: accounts_api.get_account_portfolio(account_id_key, **kwargs))
    except Exception:
        if not lots_required:
            raise
        # COMPLETE+lots view may be retried without lots_required.
        # Per-position lot fetches in attach_lots(..., strict=True) remain required.
        kwargs["lots_required"] = False
        return _retry_read(lambda: accounts_api.get_account_portfolio(account_id_key, **kwargs))


def extract_holdings(portfolio_resp):
    """Parse portfolio response into holdings list and total value."""
    try:
        portfolios = portfolio_resp["PortfolioResponse"]["AccountPortfolio"]
        if isinstance(portfolios, dict):
            portfolios = [portfolios]
        account_portfolio = portfolios[0]
        positions = account_portfolio.get("Position", [])
        if isinstance(positions, dict):
            positions = [positions]
        if not isinstance(positions, list):
            raise TypeError("Position must be a list or object")
    except (KeyError, IndexError, TypeError) as exc:
        raise PortfolioDataError("Unrecognized portfolio response") from exc

    holdings = []
    total_value = 0.0

    for pos in positions:
        try:
            product = _first(pos, "Product", "product") or {}
            symbol = _first(product, "symbol")
            if not symbol:
                raise PortfolioDataError("Portfolio position has no symbol")
            quantity = _as_float(_first(pos, "quantity"), 0.0) or 0.0
            market_value = _as_float(_first(pos, "marketValue"), 0.0) or 0.0
            quick = _first(pos, "Quick", "quick") or {}
            price = _as_float(_first(quick, "lastTrade"), 0.0) or (
                _as_float(_first(pos, "price"), 0.0) or 0.0
            ) or (market_value / quantity if quantity else 0.0)

            price_paid = _as_float(_first(pos, "pricePaid"))
            cost_per_share = _as_float(_first(pos, "costPerShare"))
            total_cost = _as_float(_first(pos, "totalCost"))
            total_gain = _as_float(_first(pos, "totalGain"))
            total_gain_pct = _as_float(_first(pos, "totalGainPct"))
            # E*TRADE uses 0 as "empty" on cost fields more often than a true $0 basis.
            if price_paid == 0:
                price_paid = None
            if cost_per_share == 0:
                cost_per_share = None
            if total_cost == 0:
                total_cost = None

            holdings.append({
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "market_value": market_value,
                "price_paid": price_paid,
                "cost_per_share": cost_per_share,
                "total_cost": total_cost,
                "total_gain": total_gain,
                "total_gain_pct": total_gain_pct,
                "date_acquired": parse_etrade_date(_first(pos, "dateAcquired")),
                "position_id": _first(pos, "positionId"),
                "lots_details": _first(pos, "lotsDetails"),
                "lots": extract_lots(pos),
            })
            total_value += market_value
        except (TypeError, ValueError) as exc:
            raise PortfolioDataError("Could not parse a portfolio position") from exc

    holdings.sort(key=lambda x: x["market_value"], reverse=True)
    return holdings, total_value


def account_label(acct: dict) -> str:
    """Human-readable label for an account."""
    name = (acct.get("accountName") or "").strip()
    desc = (acct.get("accountDesc") or "").strip()
    mode = (acct.get("accountMode") or "").strip()
    acct_type = (acct.get("accountType") or "").strip()
    acct_id = acct.get("accountId", "?")

    parts = []
    if name:
        parts.append(name)
    if desc and desc not in parts:
        parts.append(desc)
    if mode and mode not in parts:
        parts.append(mode)
    if acct_type and acct_type not in parts:
        parts.append(acct_type)

    label = " / ".join(parts) if parts else "Account"
    return f"{label} (…{str(acct_id)[-4:]})"


def format_holdings_lines(holdings, total_value, as_of=None, tax_bucket: str = "taxable"):
    as_of = as_of or now_et()
    lines = []
    for h in holdings:
        weight = (h["market_value"] / total_value * 100) if total_value > 0 else 0
        if is_cash_symbol(h["symbol"]):
            lines.append(
                f"- Cash ({h['symbol']}) ≈ ${h['market_value']:,.0f} (~{weight:.1f}%)"
            )
        else:
            suffix = format_cost_suffix(h, as_of, tax_bucket=tax_bucket)
            lines.append(
                f"- {h['symbol']} {weight:.1f}%  "
                f"(${h['market_value']:,.0f} @ ${h['price']:.2f}{suffix})"
            )
    return lines


def format_all_for_briefing(account_results, as_of: datetime | None = None):
    """
    account_results: list of dicts with keys:
      label, holdings, total_value, account_id_key, error (optional)
    """
    as_of = as_of or now_et()
    now = as_of.strftime("%Y-%m-%d %H:%M")
    grand_total = sum(r["total_value"] for r in account_results if not r.get("error"))
    total_positions = sum(sum(1 for h in r["holdings"] if h.get("symbol") != "CASH") for r in account_results if not r.get("error"))

    lines = []
    zone = as_of.tzname() or "ET"
    lines.append(f"**Portfolio (live from E*TRADE – {now} {zone})**")
    lines.append(f"Total across all accounts ≈ ${grand_total:,.0f}")
    lines.append(
        f"Accounts included: {sum(1 for r in account_results if not r.get('error'))}"
    )
    lines.append(
        "Cost basis: avg cost/share, price paid, total cost, unrealized P/L, "
        "date acquired. Taxable **ST** = held ≤ 1 year; **LT** = held > 1 year "
        "(IRS). IRA/Roth: economic P/L only — no capital-gains tax.\n"
    )

    for r in account_results:
        if r.get("error"):
            lines.append(f"### {r['label']}")
            lines.append(f"_Could not load: {r['error']}_\n")
            continue

        lines.append(f"### {r['label']} — ≈ ${r['total_value']:,.0f}")
        if not r["holdings"]:
            lines.append("_No positions_\n")
            continue

        lines.extend(
            format_holdings_lines(
                r["holdings"],
                r["total_value"],
                as_of=as_of,
                tax_bucket=r.get("tax_bucket") or "taxable",
            )
        )
        lines.append("")

    return "\n".join(lines), grand_total, total_positions


def select_accounts(all_accts, account_id_key=None):
    """
    If ETRADE_ACCOUNT_ID_KEY is set, use only that account.
    Otherwise use all ACTIVE accounts.
    """
    if account_id_key is None:
        account_id_key = _credentials()["account_id_key"]

    if account_id_key:
        matched = [a for a in all_accts if a.get("accountIdKey") == account_id_key]
        if not matched:
            raise ValueError(
                f"ETRADE_ACCOUNT_ID_KEY={account_id_key} not found in account list."
            )
        return matched

    return [a for a in all_accts if a.get("accountStatus", "").upper() == "ACTIVE"]


def fetch_portfolio_block(verbose: bool = True, allow_partial: bool = False):
    """
    Fetch all (or one) account portfolios and return:
      formatted_block, grand_total, total_positions, results, as_of
    """
    as_of = now_et()
    accounts_api, creds = get_accounts_api()

    if verbose:
        print("Fetching account list...\n")
    raw_accounts = list_accounts(accounts_api)
    all_accts = parse_account_list(raw_accounts)
    selected = select_accounts(all_accts, creds["account_id_key"])

    if not selected:
        raise RuntimeError("No accounts to fetch.")

    if verbose:
        print(f"Will fetch {len(selected)} account(s):\n")
        for a in selected:
            print(
                f"  - {account_label(a)}  [{a.get('accountIdKey')}]  "
                f"status={a.get('accountStatus')}"
            )
        print()

    results = []
    for a in selected:
        key = a["accountIdKey"]
        label = account_label(a)
        if verbose:
            print(f"Fetching portfolio: {label}...")
        try:
            raw = get_portfolio(key, accounts_api)
            holdings, position_total = extract_holdings(raw)
            attach_lots(accounts_api, holdings, verbose=verbose, strict=True)
            balance_raw = get_account_balance(key, a.get("accountType") or "", accounts_api)
            total = parse_account_balance(balance_raw)
            apply_cash_lot(
                holdings,
                account_total=total,
                position_total=position_total,
                cash_from_balance=parse_account_cash(balance_raw),
            )
            results.append({
                "label": label,
                "holdings": holdings,
                "total_value": total,
                "account_id_key": key,
                "tax_bucket": tax_bucket_from_account(a),
                "account_type": a.get("accountType"),
            })
        except Exception as e:
            results.append({
                "label": label,
                "holdings": [],
                "total_value": 0.0,
                "account_id_key": key,
                "tax_bucket": tax_bucket_from_account(a),
                "account_type": a.get("accountType"),
                "error": str(e),
            })

    failures = [r for r in results if r.get("error")]
    if failures and not allow_partial:
        labels = ", ".join(r["label"] for r in failures)
        raise IncompletePortfolioError(f"Portfolio generation stopped: incomplete account data for {labels}. Use --allow-partial only if you accept incorrect weights.")
    formatted, grand_total, total_positions = format_all_for_briefing(results, as_of=as_of)
    if failures:
        formatted = "**INCOMPLETE DATA — do not use for trade sizing.**\n\n" + formatted
    return formatted, grand_total, total_positions, results, as_of


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 text artifact."""
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def save_portfolio_block(formatted: str, as_of: datetime | None = None) -> Path:
    """Write dated + latest portfolio block files under briefings/."""
    as_of = as_of or now_et()
    date_str = as_of.strftime("%Y-%m-%d")
    out_dir = PROJECT_ROOT / "briefings"
    out_dir.mkdir(exist_ok=True)

    dated = out_dir / f"portfolio_{date_str}.txt"
    latest = out_dir / "portfolio_latest.txt"
    atomic_write_text(dated, formatted + "\n")
    atomic_write_text(latest, formatted + "\n")
    return dated


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the Windows clipboard. Returns True on success."""
    try:
        # clip.exe expects UTF-16LE on Windows
        subprocess.run(
            ["clip"],
            input=text.encode("utf-16-le"),
            check=True,
            shell=False,
        )
        return True
    except Exception:
        return False


def main(argv=None) -> int:
    parser = ArgumentParser(description="Fetch a read-only E*TRADE portfolio block.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Generate an unsafe, prominently marked result if an account fails.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    formatted, grand_total, total_positions, _results, as_of = fetch_portfolio_block(
        allow_partial=args.allow_partial
    )
    path = save_portfolio_block(formatted, as_of=as_of)
    clipped = copy_to_clipboard(formatted)

    print()
    print(formatted)
    print("\n" + "-" * 50)
    print(f"Positions found: {total_positions} | Grand total: USD {grand_total:,.0f}")
    print(f"Saved: {path}")
    if clipped:
        print("Clipboard: portfolio block copied — paste into grok.com (Ctrl+V)")
    else:
        print("Clipboard: could not copy automatically — select the block above manually")
    print("-" * 50)
    print("\nNext: open grok.com → paste into your Daily Portfolio Action Briefing prompt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

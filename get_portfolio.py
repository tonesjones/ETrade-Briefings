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
It never imports or constructs ETradeOrder / market order helpers.
No preview, place, cancel, or change-order calls exist in this repo.
"""

from __future__ import annotations

import os
import json
import subprocess
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
import pyetrade

# Explicit allow-list: only the Accounts API surface is used (list + portfolio).
# Do not import or construct pyetrade.ETradeOrder — trading is intentionally unsupported.
_ALLOWED_PYETRADE_TYPES = (pyetrade.ETradeAccounts,)

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent


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
            "Make sure you have run etrade_auth.py and added the access tokens."
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
    return accounts_api.list_accounts(resp_format="json")


def parse_account_list(resp):
    """Return list of account dicts from list_accounts response."""
    try:
        return resp["AccountListResponse"]["Accounts"]["Account"]
    except (KeyError, TypeError):
        print("Unexpected account list structure:")
        print(json.dumps(resp, indent=2))
        raise


def get_portfolio(account_id_key: str, accounts_api=None):
    """READ: portfolio positions for one account. Never places orders."""
    if accounts_api is None:
        accounts_api, _ = get_accounts_api()
    return accounts_api.get_account_portfolio(
        account_id_key,
        count=100,
        totals_required=True,
        view="COMPLETE",
        resp_format="json",
    )


def extract_holdings(portfolio_resp):
    """Parse portfolio response into holdings list and total value."""
    try:
        account_portfolio = portfolio_resp["PortfolioResponse"]["AccountPortfolio"][0]
        positions = account_portfolio.get("Position", [])
        if isinstance(positions, dict):
            positions = [positions]
    except (KeyError, IndexError, TypeError):
        return [], 0.0

    holdings = []
    total_value = 0.0

    for pos in positions:
        try:
            symbol = pos["Product"]["symbol"]
            quantity = float(pos.get("quantity", 0))
            market_value = float(pos.get("marketValue", 0))
            price = float(pos.get("Quick", {}).get("lastTrade", 0)) or (
                market_value / quantity if quantity else 0
            )

            if market_value < 50:
                continue

            holdings.append({
                "symbol": symbol,
                "quantity": quantity,
                "price": price,
                "market_value": market_value,
            })
            total_value += market_value
        except Exception:
            continue

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


def format_holdings_lines(holdings, total_value):
    lines = []
    for h in holdings:
        weight = (h["market_value"] / total_value * 100) if total_value > 0 else 0
        if h["symbol"] in ("SGOV", "BIL", "SGOVX") or "GOVERNMENT" in h["symbol"].upper():
            lines.append(
                f"- Cash ({h['symbol']}) ≈ ${h['market_value']:,.0f} (~{weight:.1f}%)"
            )
        else:
            lines.append(
                f"- {h['symbol']} {weight:.1f}%  "
                f"(${h['market_value']:,.0f} @ ${h['price']:.2f})"
            )
    return lines


def format_all_for_briefing(account_results, as_of: datetime | None = None):
    """
    account_results: list of dicts with keys:
      label, holdings, total_value, account_id_key, error (optional)
    """
    as_of = as_of or datetime.now()
    now = as_of.strftime("%Y-%m-%d %H:%M")
    grand_total = sum(r["total_value"] for r in account_results if not r.get("error"))
    total_positions = sum(len(r["holdings"]) for r in account_results if not r.get("error"))

    lines = []
    lines.append(f"**Portfolio (live from E*TRADE – {now} PDT)**")
    lines.append(f"Total across all accounts ≈ ${grand_total:,.0f}")
    lines.append(
        f"Accounts included: {sum(1 for r in account_results if not r.get('error'))}\n"
    )

    for r in account_results:
        if r.get("error"):
            lines.append(f"### {r['label']}")
            lines.append(f"_Could not load: {r['error']}_\n")
            continue

        lines.append(f"### {r['label']} — ≈ ${r['total_value']:,.0f}")
        if not r["holdings"]:
            lines.append("_No positions above $50_\n")
            continue

        lines.extend(format_holdings_lines(r["holdings"], r["total_value"]))
        lines.append("")

    lines.append(
        "Employer: ≈ $60k SNPS (include in analysis; action allowed if price/thesis warrants)"
    )
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


def fetch_portfolio_block(verbose: bool = True):
    """
    Fetch all (or one) account portfolios and return:
      formatted_block, grand_total, total_positions, results, as_of
    """
    as_of = datetime.now()
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
            holdings, total = extract_holdings(raw)
            results.append({
                "label": label,
                "holdings": holdings,
                "total_value": total,
                "account_id_key": key,
            })
        except Exception as e:
            results.append({
                "label": label,
                "holdings": [],
                "total_value": 0.0,
                "account_id_key": key,
                "error": str(e),
            })

    formatted, grand_total, total_positions = format_all_for_briefing(results, as_of=as_of)
    return formatted, grand_total, total_positions, results, as_of


def save_portfolio_block(formatted: str, as_of: datetime | None = None) -> Path:
    """Write dated + latest portfolio block files under briefings/."""
    as_of = as_of or datetime.now()
    date_str = as_of.strftime("%Y-%m-%d")
    out_dir = PROJECT_ROOT / "briefings"
    out_dir.mkdir(exist_ok=True)

    dated = out_dir / f"portfolio_{date_str}.txt"
    latest = out_dir / "portfolio_latest.txt"
    dated.write_text(formatted + "\n", encoding="utf-8")
    latest.write_text(formatted + "\n", encoding="utf-8")
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


if __name__ == "__main__":
    formatted, grand_total, total_positions, _results, as_of = fetch_portfolio_block()
    path = save_portfolio_block(formatted, as_of=as_of)
    clipped = copy_to_clipboard(formatted)

    print()
    print(formatted)
    print("\n" + "-" * 50)
    print(f"Positions found: {total_positions} | Grand total: ${grand_total:,.0f}")
    print(f"Saved: {path}")
    if clipped:
        print("Clipboard: portfolio block copied — paste into grok.com (Ctrl+V)")
    else:
        print("Clipboard: could not copy automatically — select the block above manually")
    print("-" * 50)
    print("\nNext: open grok.com → paste into your Daily Portfolio Action Briefing prompt.")

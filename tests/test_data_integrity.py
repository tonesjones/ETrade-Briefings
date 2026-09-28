"""Tests for fetch integrity: paging, reconciliation, lots, retries, auth, policy."""

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

import build_briefing_prompt as briefing
import get_portfolio as portfolio
import portfolio_policy
from etrade_auth import update_env_tokens

ET = ZoneInfo("America/New_York")


def _position(symbol: str, qty: float = 1.0, value: float = 10.0) -> dict:
    return {"Product": {"symbol": symbol}, "quantity": qty, "marketValue": value}


def _page(positions: list[dict], total_pages: int | None = None) -> dict:
    account = {"Position": positions}
    if total_pages is not None:
        account["totalPages"] = total_pages
    return {"PortfolioResponse": {"AccountPortfolio": [account]}}


def _http_error(status: int) -> requests.HTTPError:
    return requests.HTTPError(response=SimpleNamespace(status_code=status))


class FakeAccounts:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_account_portfolio(self, key, **kwargs):
        self.calls.append(kwargs)
        page = self.pages[kwargs["page_number"] - 1]
        if isinstance(page, Exception):
            raise page
        return page


class PagingTests(unittest.TestCase):
    def test_follows_total_pages(self):
        api = FakeAccounts([
            _page([_position("A")], total_pages=3),
            _page([_position("B")], total_pages=3),
            _page([_position("C")], total_pages=3),
        ])
        response = portfolio.get_portfolio("key", api)
        holdings, total = portfolio.extract_holdings(response)
        self.assertEqual({h["symbol"] for h in holdings}, {"A", "B", "C"})
        self.assertEqual(total, 30.0)
        self.assertEqual([c["page_number"] for c in api.calls], [1, 2, 3])

    def test_full_page_without_count_fetches_next_page(self):
        size = portfolio.PORTFOLIO_PAGE_SIZE
        first = [_position(f"S{i}") for i in range(size)]
        api = FakeAccounts([_page(first), _page([_position("LAST")])])
        holdings, _ = portfolio.extract_holdings(portfolio.get_portfolio("key", api))
        self.assertEqual(len(holdings), size + 1)

    def test_short_page_stops(self):
        api = FakeAccounts([_page([_position("A")])])
        portfolio.get_portfolio("key", api)
        self.assertEqual(len(api.calls), 1)

    def test_lots_fallback_only_for_request_errors(self):
        api = FakeAccounts([_page([_position("A")])])
        original = api.get_account_portfolio
        calls = {"n": 0}

        def flaky(key, **kwargs):
            calls["n"] += 1
            if kwargs["lots_required"]:
                raise _http_error(400)
            return original(key, **kwargs)

        api.get_account_portfolio = flaky
        portfolio.get_portfolio("key", api)
        self.assertEqual(calls["n"], 2)

        api.get_account_portfolio = lambda key, **kw: (_ for _ in ()).throw(KeyError("bug"))
        with self.assertRaises(KeyError):
            portfolio.get_portfolio("key", api)


class ReconciliationTests(unittest.TestCase):
    def test_missing_positions_fail(self):
        holdings = [{"symbol": "A", "market_value": 60_000.0}]
        with self.assertRaisesRegex(portfolio.PortfolioDataError, "do not reconcile"):
            portfolio.reconcile_account("Acct", holdings, 100_000.0, source="Live portfolio")

    def test_small_timing_difference_passes(self):
        holdings = [{"symbol": "A", "market_value": 99_960.0}]
        portfolio.reconcile_account("Acct", holdings, 100_000.0, source="Live portfolio")

    def test_lot_quantity_mismatch_fails(self):
        holding = {"symbol": "A", "quantity": 10.0, "lots": [{"qty": 4.0}, {"qty": 5.0}]}
        with self.assertRaisesRegex(portfolio.PortfolioDataError, "lots 9 vs position 10"):
            portfolio.check_lot_quantities("Acct", [holding])

    def test_lot_quantities_matching_within_rounding_pass(self):
        holding = {"symbol": "A", "quantity": 10.0, "lots": [{"qty": 4.99999}, {"qty": 5.0}]}
        portfolio.check_lot_quantities("Acct", [holding])


class RetryTests(unittest.TestCase):
    def test_programming_errors_are_not_retried(self):
        calls = []

        def boom():
            calls.append(1)
            raise KeyError("parse bug")

        with self.assertRaises(KeyError):
            portfolio._retry_read(boom)
        self.assertEqual(len(calls), 1)

    @patch.object(portfolio.time, "sleep")
    def test_server_errors_are_retried(self, _sleep):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise _http_error(503)
            return "ok"

        self.assertEqual(portfolio._retry_read(flaky), "ok")
        self.assertEqual(len(calls), 3)

    def test_401_becomes_reauth_message(self):
        def expired():
            raise _http_error(401)

        with self.assertRaisesRegex(portfolio.AuthExpiredError, "etrade_auth.py"):
            portfolio._retry_read(expired)

    @patch("get_portfolio.get_portfolio", side_effect=portfolio.AuthExpiredError())
    @patch("get_portfolio.list_accounts")
    @patch("get_portfolio.get_accounts_api")
    def test_expired_token_is_not_swallowed_as_partial(self, get_api, list_accounts, _gp):
        get_api.return_value = (object(), {"account_id_key": None})
        list_accounts.return_value = {
            "AccountListResponse": {"Accounts": {"Account": {
                "accountIdKey": "key", "accountStatus": "ACTIVE", "accountType": "INDIVIDUAL",
            }}}
        }
        with self.assertRaises(portfolio.AuthExpiredError):
            portfolio.fetch_portfolio_block(verbose=False, allow_partial=True)


class AuthDateTests(unittest.TestCase):
    today = datetime(2026, 9, 28, 9, 0, tzinfo=ET)

    def test_stale_date_fails_fast(self):
        with patch.dict(os.environ, {"ETRADE_AUTH_DATE": "2026-09-27"}):
            with self.assertRaisesRegex(portfolio.AuthExpiredError, "2026-09-27"):
                portfolio.check_auth_date(self.today)

    def test_same_day_and_missing_date_pass(self):
        with patch.dict(os.environ, {"ETRADE_AUTH_DATE": "2026-09-28"}):
            portfolio.check_auth_date(self.today)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ETRADE_AUTH_DATE", None)
            portfolio.check_auth_date(self.today)

    def test_env_update_handles_export_and_records_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# ETRADE_ACCESS_TOKEN=comment stays\n"
                "export ETRADE_ACCESS_TOKEN=old\n"
                "ETRADE_ACCESS_TOKEN_SECRET = 'old'\n",
                encoding="utf-8",
            )
            update_env_tokens(path, "new", "secret", auth_date="2026-09-28")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, [
                "# ETRADE_ACCESS_TOKEN=comment stays",
                "ETRADE_ACCESS_TOKEN=new",
                "ETRADE_ACCESS_TOKEN_SECRET=secret",
                "ETRADE_AUTH_DATE=2026-09-28",
            ])


class PolicySwapTests(unittest.TestCase):
    def test_active_policy_can_be_replaced(self):
        base = portfolio_policy.active_policy()
        tighter = portfolio_policy.PortfolioPolicy(
            **{**base.__dict__, "single_name_cap_pct": 1.0}
        )
        previous = portfolio_policy.set_active_policy(tighter)
        try:
            text = briefing.format_constraint_math(
                [{"symbol": "A", "market_value": 50.0, "weight": 5.0}], 1000.0, 0.0
            )
            self.assertIn("1%", text)
            self.assertIs(portfolio_policy.POLICY, tighter)
        finally:
            portfolio_policy.set_active_policy(previous)


if __name__ == "__main__":
    unittest.main()

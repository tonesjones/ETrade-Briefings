import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import get_portfolio as portfolio
from etrade_auth import update_env_tokens
from portfolio_policy import POLICY


ET = ZoneInfo("America/New_York")


class PortfolioParsingTests(unittest.TestCase):
    def test_exact_one_year_is_short_term(self):
        acquired = datetime(2025, 8, 12, tzinfo=ET)
        self.assertEqual(
            portfolio.holding_period(acquired, datetime(2026, 8, 12, tzinfo=ET)),
            "ST",
        )
        self.assertEqual(
            portfolio.holding_period(acquired, datetime(2026, 8, 13, tzinfo=ET)),
            "LT",
        )

    def test_leap_day_anniversary(self):
        acquired = datetime(2024, 2, 29, tzinfo=ET)
        self.assertEqual(
            portfolio.holding_period(acquired, datetime(2025, 3, 1, tzinfo=ET)),
            "LT",
        )

    def test_account_list_normalizes_single_object(self):
        response = {
            "AccountListResponse": {"Accounts": {"Account": {"accountIdKey": "x"}}}
        }
        self.assertEqual(portfolio.parse_account_list(response), [{"accountIdKey": "x"}])

    def test_malformed_portfolio_fails_closed(self):
        with self.assertRaises(portfolio.PortfolioDataError):
            portfolio.extract_holdings({"unexpected": {}})

    def test_balance_uses_total_account_value(self):
        response = {
            "BalanceResponse": {
                "Computed": {"RealTimeValues": {"totalAccountValue": 12345.67}}
            }
        }
        self.assertEqual(portfolio.parse_account_balance(response), 12345.67)

    def test_balance_falls_back_to_net_market_value_plus_cash(self):
        response = {
            "BalanceResponse": {
                "Computed": {
                    "accountBalance": 0,
                    "netCash": 900.0,
                    "RealTimeValues": {"netMv": 1100.0},
                }
            }
        }
        self.assertEqual(portfolio.parse_account_balance(response), 2000.0)

    def test_account_balance_alone_is_not_a_safe_total(self):
        with self.assertRaises(portfolio.PortfolioDataError):
            portfolio.parse_account_balance(
                {"BalanceResponse": {"Computed": {"accountBalance": 0}}}
            )
    @patch("get_portfolio.get_portfolio", side_effect=RuntimeError("expired"))
    @patch("get_portfolio.list_accounts")
    @patch("get_portfolio.get_accounts_api")
    def test_account_failure_requires_explicit_partial_mode(
        self, get_api, list_accounts, _get_portfolio
    ):
        get_api.return_value = (object(), {"account_id_key": None})
        list_accounts.return_value = {
            "AccountListResponse": {
                "Accounts": {
                    "Account": {
                        "accountIdKey": "key",
                        "accountId": "1234",
                        "accountStatus": "ACTIVE",
                        "accountType": "INDIVIDUAL",
                    }
                }
            }
        }
        with self.assertRaises(portfolio.IncompletePortfolioError):
            portfolio.fetch_portfolio_block(verbose=False)
        text, *_ = portfolio.fetch_portfolio_block(
            verbose=False, allow_partial=True
        )
        self.assertIn("INCOMPLETE DATA", text)


class OAuthFileTests(unittest.TestCase):
    def test_token_update_is_targeted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("KEEP=yes\nETRADE_ACCESS_TOKEN=old\n", encoding="utf-8")
            update_env_tokens(path, "new", "secret")
            text = path.read_text(encoding="utf-8")
            self.assertIn("KEEP=yes", text)
            self.assertIn("ETRADE_ACCESS_TOKEN=new", text)
            self.assertIn("ETRADE_ACCESS_TOKEN_SECRET=secret", text)
            self.assertNotIn("ETRADE_ACCESS_TOKEN=old", text)


class CashLotTests(unittest.TestCase):
    def test_parse_account_cash_sums_balance_and_money_market(self):
        response = {
            "BalanceResponse": {
                "Computed": {"cashBalance": 40.0},
                "Cash": {"moneyMktBalance": 60.0},
            }
        }
        self.assertEqual(portfolio.parse_account_cash(response), 100.0)

    def test_parse_account_cash_falls_back_to_net_cash(self):
        response = {"BalanceResponse": {"Computed": {"netCash": 250.0}}}
        self.assertEqual(portfolio.parse_account_cash(response), 250.0)

    def test_official_zero_cash_does_not_synthesize_residual(self):
        holdings = []
        portfolio.apply_cash_lot(
            holdings,
            account_total=1000.0,
            position_total=1174.0,
            cash_from_balance=0.0,
        )
        self.assertEqual(holdings, [])

    def test_negative_residual_is_not_labeled_cash(self):
        holdings = []
        portfolio.apply_cash_lot(
            holdings,
            account_total=1000.0,
            position_total=1174.0,
            cash_from_balance=None,
        )
        self.assertEqual(holdings, [])

    def test_sub_dollar_residual_is_ignored(self):
        holdings = []
        portfolio.apply_cash_lot(
            holdings,
            account_total=1000.40,
            position_total=1000.0,
            cash_from_balance=None,
        )
        self.assertEqual(holdings, [])

    def test_positive_residual_synthesized_when_cash_fields_missing(self):
        holdings = []
        portfolio.apply_cash_lot(
            holdings,
            account_total=1078.0,
            position_total=1000.0,
            cash_from_balance=None,
        )
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["symbol"], "CASH")
        self.assertEqual(holdings[0]["market_value"], 78.0)

    def test_official_cash_is_used_instead_of_residual(self):
        holdings = []
        portfolio.apply_cash_lot(
            holdings,
            account_total=1200.0,
            position_total=1000.0,
            cash_from_balance=25.0,
        )
        self.assertEqual(holdings[0]["market_value"], 25.0)


class BriefingPolicyTests(unittest.TestCase):
    def test_harvest_flag_uses_policy_cutoff(self):
        as_of = datetime(2026, 8, 12, tzinfo=ET)

        def flags_for(gain: float) -> str:
            results = [{
                "tax_bucket": "taxable",
                "holdings": [{
                    "symbol": "ZZZ",
                    "total_cost": 1000.0,
                    "price_paid": 10.0,
                    "total_gain": gain,
                    "lots": [],
                    "market_value": 750.0,
                }],
            }]
            return "\n".join(portfolio.collect_tax_flags(results, as_of))

        self.assertNotIn("ZZZ", flags_for(POLICY.harvest_loss_dollars + 1))
        self.assertIn("ZZZ", flags_for(POLICY.harvest_loss_dollars - 1))

    def test_snapshot_parses_cash_equivalents_header(self):
        text = (
            "**Consolidated holdings**\n"
            "- AMD 15.2%  ($179,865 @ $482.25)\n"
            "**Top-5 (E*TRADE):** 64.2% · **Cash + cash equivalents:** $69,979 (5.9%)\n"
        )
        snap = briefing.parse_snapshot_from_prompt(text)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap["cash_w"], 5.9)

    def test_snapshot_still_parses_legacy_sgov_cash(self):
        text = (
            "**Consolidated holdings**\n"
            "- AMD 15.2%  ($179,865 @ $482.25)\n"
            "Cash (SGOV): $70,159 (6.1%)\n"
        )
        snap = briefing.parse_snapshot_from_prompt(text)
        self.assertAlmostEqual(snap["cash_w"], 6.1)

    def test_prompt_wires_policy_sleeve_labels_and_thresholds(self):
        as_of = datetime(2026, 8, 12, 16, 0, tzinfo=ET)
        holdings = [{
            "symbol": "AMD",
            "market_value": 100_000.0,
            "weight": 100.0,
            "price": 100.0,
            "alias": None,
            "quantity": 1000.0,
            "cost_per_share": 50.0,
            "price_paid": 50.0,
            "total_cost": 50_000.0,
            "total_gain": 50_000.0,
            "total_gain_pct": 100.0,
            "lots": [],
            "tax_buckets": {"taxable"},
            "taxable_mv": 100_000.0,
            "ira_mv": 0.0,
            "roth_mv": 0.0,
        }]
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(briefing, "BRIEFINGS_DIR", Path(directory)),
                patch.object(briefing, "OUT_DIR", Path(directory)),
            ):
                prompt = briefing.build_prompt(
                    holdings,
                    [{"label": "Brokerage", "total": 100_000.0}],
                    100_000.0,
                    as_of,
                    "block",
                    results=[],
                )
        extras = [s for s in POLICY.sleeves["broad_ai_cycle"] if s not in POLICY.sleeves["direct_ai_semi"]]
        self.assertIn("Broad AI-cycle liquid (direct + " + " ".join(extras) + ")", prompt)
        self.assertIn("Crypto (" + " ".join(POLICY.sleeves["crypto"]) + ")", prompt)
        self.assertIn("Payments (" + " ".join(POLICY.sleeves["payments"]) + ")", prompt)
        self.assertIn("Broad index (" + " ".join(POLICY.sleeves["broad_index"]) + ")", prompt)
        self.assertIn(f"work *toward* {POLICY.risk_off_glide_pct:g}%", prompt)
        self.assertIn(
            f"Do not omit a name because it is under {POLICY.analyze_weight_floor_pct:g}%",
            prompt,
        )


if __name__ == "__main__":
    unittest.main()

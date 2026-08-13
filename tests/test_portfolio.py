import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import get_portfolio as portfolio
from etrade_auth import update_env_tokens
from portfolio_policy import POLICY, load_policy


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

    def test_st_lt_flags_use_policy_cutoffs(self):
        as_of = datetime(2026, 8, 12, tzinfo=ET)

        def flags_for(gain: float, acquired: datetime) -> str:
            results = [{
                "tax_bucket": "taxable",
                "holdings": [{
                    "symbol": "ZZZ",
                    "total_cost": 10_000.0,
                    "price_paid": 10.0,
                    "total_gain": gain,
                    "date_acquired": acquired,
                    "lots": [],
                    "market_value": 10_000.0 + gain,
                }],
            }]
            return "\n".join(portfolio.collect_tax_flags(results, as_of))

        st_acq = datetime(2026, 6, 1, tzinfo=ET)
        lt_acq = datetime(2024, 6, 1, tzinfo=ET)
        self.assertNotIn("ZZZ", flags_for(POLICY.st_gain_flag_dollars, st_acq))
        self.assertIn("ZZZ", flags_for(POLICY.st_gain_flag_dollars + 1, st_acq))
        self.assertNotIn("ZZZ", flags_for(POLICY.lt_gain_flag_dollars, lt_acq))
        self.assertIn("ZZZ", flags_for(POLICY.lt_gain_flag_dollars + 1, lt_acq))

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
                    [
                        {"label": "Brokerage (…7810)", "total": 100_000.0, "tax_bucket": "taxable"},
                        {"label": "Empty (…9627)", "total": 0.0, "tax_bucket": "taxable"},
                    ],
                    100_000.0,
                    as_of,
                    "**Portfolio (live from E*TRADE – should not be pasted)**",
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
        for label, _symbol, _note in POLICY.marginal_examples:
            self.assertIn(label, prompt)
        self.assertNotIn("Per-account source block", prompt)
        self.assertNotIn("Portfolio (live from E*TRADE", prompt)
        self.assertNotIn("**Live risk flags:**", prompt)
        self.assertNotIn("9627", prompt)
        self.assertIn("7810", prompt)

    def test_holding_line_still_parses_for_daily_delta(self):
        line = briefing.holding_line({
            "symbol": "AMD",
            "alias": None,
            "weight": 15.2,
            "market_value": 180_133,
            "price": 482.93,
            "total_gain_pct": 165.0,
            "taxable_mv": 132_806,
            "ira_mv": 47_327,
            "roth_mv": 0.0,
        }, datetime(2026, 8, 12, tzinfo=ET))
        snap = briefing.parse_snapshot_from_prompt(
            "**Consolidated holdings**\n" + line + "\n"
        )
        self.assertEqual(snap["holdings"][0]["symbol"], "AMD")
        self.assertAlmostEqual(snap["holdings"][0]["weight"], 15.2)
        self.assertAlmostEqual(snap["holdings"][0]["market_value"], 180_133)

    def test_lot_table_covers_ira_and_skips_cash(self):
        as_of = datetime(2026, 8, 12, tzinfo=ET)
        table = portfolio.format_lot_table(
            [
                {
                    "label": "Brokerage (…7810)",
                    "tax_bucket": "taxable",
                    "holdings": [
                        {
                            "symbol": "AMD",
                            "price": 100.0,
                            "market_value": 1000.0,
                            "cost_per_share": 50.0,
                            "total_cost": 500.0,
                            "total_gain": 500.0,
                            "total_gain_pct": 100.0,
                            "lots": [],
                            "date_acquired": datetime(2024, 1, 1, tzinfo=ET),
                        },
                        {"symbol": "SGOV", "market_value": 100.0, "price": 100.0},
                    ],
                },
                {
                    "label": "Traditional IRA (…6183)",
                    "tax_bucket": "traditional",
                    "holdings": [{
                        "symbol": "AMZN",
                        "price": 200.0,
                        "market_value": 2000.0,
                        "cost_per_share": 180.0,
                        "total_cost": 1800.0,
                        "total_gain": 200.0,
                        "lots": [],
                        "date_acquired": datetime(2021, 1, 12, tzinfo=ET),
                    }],
                },
            ],
            as_of,
        )
        self.assertIn("| AMD |", table)
        self.assertIn("| AMZN |", table)
        self.assertIn("| IRA |", table)
        self.assertIn("no CG", table)
        self.assertNotIn("SGOV", table)

    def test_load_policy_defaults_optional_flag_fields(self):
        raw = {
            "schema_version": 1,
            "single_name_cap_pct": 15.0,
            "cluster_do_not_increase_pct": 38.0,
            "cluster_soft_cap_pct": 40.0,
            "analyze_weight_floor_pct": 2.5,
            "analyze_market_value_floor": 5000.0,
            "material_loss_dollars": -5000.0,
            "material_loss_pct": -20.0,
            "harvest_loss_dollars": -250.0,
            "risk_off_glide_pct": 35.0,
            "cash_symbols": ["SGOV"],
            "sleeves": {
                "direct_ai_semi": ["NVDA"],
                "broad_ai_cycle": ["NVDA"],
                "crypto": ["ETHA"],
                "payments": ["MA"],
                "broad_index": ["VOO"],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            loaded = load_policy(path)
        self.assertEqual(loaded.st_gain_flag_dollars, 500.0)
        self.assertEqual(loaded.lt_gain_flag_dollars, 2000.0)
        self.assertGreaterEqual(len(loaded.marginal_examples), 1)


class TaxBucketTests(unittest.TestCase):
    def test_official_types(self):
        self.assertEqual(
            portfolio.tax_bucket_from_account({"accountType": "INDIVIDUAL"}),
            "taxable",
        )
        self.assertEqual(
            portfolio.tax_bucket_from_account({"accountType": "CONTRIBUTORY"}),
            "traditional",
        )
        self.assertEqual(
            portfolio.tax_bucket_from_account({"accountType": "ROTHIRA"}),
            "roth",
        )

    def test_individual_named_retirement_stays_taxable(self):
        self.assertEqual(
            portfolio.tax_bucket_from_account({
                "accountType": "INDIVIDUAL",
                "accountName": "Retirement",
                "accountDesc": "Retirement savings",
                "accountMode": "CASH",
            }),
            "taxable",
        )

    def test_unknown_type_falls_back_to_name(self):
        self.assertEqual(
            portfolio.tax_bucket_from_account({
                "accountType": "UNKNOWN",
                "accountName": "SEP IRA",
            }),
            "traditional",
        )
        self.assertEqual(
            portfolio.tax_bucket_from_account({
                "accountType": "",
                "accountName": "Roth leftover",
            }),
            "roth",
        )


if __name__ == "__main__":
    unittest.main()

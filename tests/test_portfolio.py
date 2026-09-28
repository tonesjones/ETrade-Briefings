import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import get_portfolio as portfolio
from etrade_auth import update_env_tokens
from portfolio_policy import POLICY, load_policy

ET = ZoneInfo("America/New_York")


def consolidated(symbol: str, market_value: float, weight: float, **overrides) -> dict:
    """A consolidated holding as build_prompt expects it, with neutral defaults."""
    row = {
        "symbol": symbol, "market_value": market_value, "weight": weight,
        "price": 100.0, "alias": None, "quantity": market_value / 100.0,
        "cost_per_share": None, "price_paid": None, "total_cost": None,
        "total_gain": None, "total_gain_pct": None, "lots": [],
        "tax_buckets": {"taxable"}, "taxable_mv": market_value, "ira_mv": 0.0, "roth_mv": 0.0,
    }
    row.update(overrides)
    return row


SGOV_AND_CASH = lambda: [  # noqa: E731
    consolidated("SGOV", 900.0, 90.0),
    consolidated("CASH", 100.0, 10.0, price=1.0, quantity=100.0),
]
AMD_ONLY = dict(
    price=100.0, quantity=1000.0, cost_per_share=50.0, price_paid=50.0,
    total_cost=50_000.0, total_gain=50_000.0, total_gain_pct=100.0,
)


class PortfolioParsingTests(unittest.TestCase):
    def test_portable_payload_round_trip_preserves_accounts_lots_and_timestamp(self):
        fixture = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"
        text = fixture.read_text(encoding="utf-8")
        formatted, total, positions, results, as_of = portfolio.parse_portable_portfolio_text(text)
        self.assertIn("Sanitized offline fixture", formatted)
        self.assertEqual(total, 1500.0)
        self.assertEqual(positions, 2)
        self.assertEqual(as_of.isoformat(), "2026-09-18T16:00:00-04:00")
        self.assertEqual(results[0]["tax_bucket"], "taxable")
        self.assertEqual(results[0]["account_ref"], "fixture-taxable")
        self.assertEqual(results[0]["holdings"][0]["lots"][0]["qty"], 5.0)
        self.assertEqual(results[1]["tax_bucket"], "roth")

    def test_portable_payload_allows_small_balance_to_mark_timing_difference(self):
        fixture = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"
        _before, raw_json = fixture.read_text(encoding="utf-8").split(
            portfolio.PORTABLE_BEGIN, 1
        )
        raw_json, _after = raw_json.split(portfolio.PORTABLE_END, 1)
        payload = json.loads(raw_json)
        account = payload["accounts"][0]
        account["total_value"] = 100_000.0
        account["holdings"][1]["market_value"] = 99_540.0
        text = (
            f"{portfolio.PORTABLE_BEGIN}\n{json.dumps(payload)}\n"
            f"{portfolio.PORTABLE_END}\n"
        )

        _formatted, total, _positions, _results, _as_of = (
            portfolio.parse_portable_portfolio_text(text)
        )

        self.assertEqual(total, 100_500.0)

    def test_portable_payload_rejects_material_balance_to_mark_difference(self):
        fixture = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"
        _before, raw_json = fixture.read_text(encoding="utf-8").split(
            portfolio.PORTABLE_BEGIN, 1
        )
        raw_json, _after = raw_json.split(portfolio.PORTABLE_END, 1)
        payload = json.loads(raw_json)
        account = payload["accounts"][0]
        account["total_value"] = 100_000.0
        account["holdings"][1]["market_value"] = 99_600.0
        text = (
            f"{portfolio.PORTABLE_BEGIN}\n{json.dumps(payload)}\n"
            f"{portfolio.PORTABLE_END}\n"
        )

        with self.assertRaisesRegex(portfolio.PortfolioDataError, "do not reconcile"):
            portfolio.parse_portable_portfolio_text(text)

    def test_legacy_human_dump_fails_closed(self):
        with self.assertRaisesRegex(portfolio.PortfolioDataError, "structured payload"):
            portfolio.parse_portable_portfolio_text("**Portfolio (live from E*TRADE)**")

    @patch.object(portfolio.subprocess, "run")
    def test_clipboard_reader_decodes_windows_code_page_text(self, run):
        payload = (
            f"{portfolio.PORTABLE_BEGIN}\n"
            '{"observed_at":"2026-09-19T00:43:00-04:00","label":"…7810"}\n'
            f"{portfolio.PORTABLE_END}\n"
        )
        run.return_value = SimpleNamespace(stdout=payload.encode("cp1252"))
        self.assertEqual(portfolio.read_clipboard_text(), payload)

    @patch.object(portfolio.subprocess, "run")
    def test_clipboard_reader_rejects_missing_output(self, run):
        run.return_value = SimpleNamespace(stdout=None)
        with self.assertRaisesRegex(portfolio.PortfolioDataError, "clipboard"):
            portfolio.read_clipboard_text()

    def test_portable_payload_hashes_raw_account_key(self):
        raw_key = "raw-secret-account-key"
        payload = portfolio.portable_portfolio_payload(
            [
                {
                    "label": "Brokerage (…1111)",
                    "account_id_key": raw_key,
                    "tax_bucket": "taxable",
                    "account_type": "INDIVIDUAL",
                    "total_value": 10.0,
                    "holdings": [],
                }
            ],
            datetime(2026, 9, 18, tzinfo=ET),
        )
        encoded = json.dumps(payload)
        self.assertNotIn(raw_key, encoded)
        self.assertEqual(len(payload["accounts"][0]["account_ref"]), 12)

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
        response = {"AccountListResponse": {"Accounts": {"Account": {"accountIdKey": "x"}}}}
        self.assertEqual(portfolio.parse_account_list(response), [{"accountIdKey": "x"}])

    def test_malformed_portfolio_fails_closed(self):
        with self.assertRaises(portfolio.PortfolioDataError):
            portfolio.extract_holdings({"unexpected": {}})

    def test_balance_uses_total_account_value(self):
        response = {
            "BalanceResponse": {"Computed": {"RealTimeValues": {"totalAccountValue": 12345.67}}}
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
        text, *_ = portfolio.fetch_portfolio_block(verbose=False, allow_partial=True)
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
    def test_offline_input_never_fetches_and_persists_after_build(self):
        fixture = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            briefings = root / "briefings"
            prompts = root / "prompts"
            briefings.mkdir()
            prompts.mkdir()
            (briefings / "weights_2026-09-15.json").write_text(
                json.dumps({"date": "2026-09-15", "holdings": []}),
                encoding="utf-8",
            )
            (briefings / "observations_2026-09-15.json").write_text(
                json.dumps({"date": "2026-09-15", "positions": [], "reviews": []}),
                encoding="utf-8",
            )
            with (
                patch.object(briefing, "BRIEFINGS_DIR", briefings),
                patch.object(briefing, "OUT_DIR", prompts),
                patch.object(briefing, "CONTEXT_PATH", root / "missing-context.json"),
                patch.object(
                    briefing,
                    "fetch_portfolio_block",
                    side_effect=AssertionError("network path used"),
                ),
                patch.object(briefing, "copy_to_clipboard", return_value=True),
                patch.object(portfolio, "PROJECT_ROOT", root),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = briefing.main(["--input-file", str(fixture)])
            self.assertEqual(exit_code, 0)
            prompt = (prompts / "daily_briefing_prompt_2026-09-18.md").read_text(encoding="utf-8")
            self.assertIn("3 calendar days earlier", prompt)
            self.assertIn("changes are cumulative", prompt)
            self.assertTrue((briefings / "weights_2026-09-18.json").exists())
            self.assertTrue((briefings / "observations_2026-09-18.json").exists())

    def test_invalid_offline_input_writes_no_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.txt"
            source.write_text("legacy human-readable dump", encoding="utf-8")
            briefings = root / "briefings"
            prompts = root / "prompts"
            with (
                patch.object(briefing, "BRIEFINGS_DIR", briefings),
                patch.object(briefing, "OUT_DIR", prompts),
                patch.object(
                    briefing,
                    "fetch_portfolio_block",
                    side_effect=AssertionError("network path used"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(portfolio.PortfolioDataError):
                    briefing.main(["--input-file", str(source)])
            self.assertFalse(briefings.exists())
            self.assertFalse(prompts.exists())

    def test_context_loader_and_formatters_preserve_dated_research(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio_context.json"
            path.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-09-08",
                        "owner_profile": {"horizon": "10+ years"},
                        "research": {
                            "ABC": {
                                "reviewed_at": "2026-09-07",
                                "fundamental_view": "ATTRACTIVE",
                                "decision_basis": "THESIS SUPPORTED",
                                "valuation_or_entry": "FCF yield above 4%",
                                "decision_trigger": "Revenue growth below 5%",
                            }
                        },
                        "decision_history": [
                            {
                                "as_of": "2026-09-07",
                                "scope": "ABC",
                                "portfolio_action": "KEEP",
                                "decision_basis": "THESIS SUPPORTED",
                                "decision_trigger": "Revenue growth below 5%",
                                "approval_state": "NOT APPROVED",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            context, status = briefing.load_portfolio_context(path)
        self.assertIn("2026-09-08", status)
        self.assertIn("10+ years", briefing.format_owner_profile(context))
        self.assertIn("FCF yield above 4%", briefing.format_research_records(context))
        self.assertIn("NOT APPROVED", briefing.format_decision_history(context))

    def test_prompt_separates_cash_components(self):
        as_of = datetime(2026, 8, 12, 16, 0, tzinfo=ET)
        holdings = SGOV_AND_CASH()
        with patch.object(briefing, "load_portfolio_context", return_value=({}, "_No context._")):
            prompt = briefing.build_prompt(
                holdings,
                [
                    {
                        "label": "Brokerage (…7810)",
                        "total": 1000.0,
                        "tax_bucket": "taxable",
                    }
                ],
                1000.0,
                as_of,
                "unused",
                results=[],
            )
        self.assertIn("**Cash composition:** SGOV $900, CASH $100", prompt)

    def test_routine_prompt_defers_candidate_refresh(self):
        as_of = datetime(2026, 9, 18, 16, 0, tzinfo=ET)
        prior_as_of = datetime(2026, 9, 17, 16, 0, tzinfo=ET)
        holdings = SGOV_AND_CASH()
        prior_weights = briefing.snapshot_dict(
            holdings,
            {
                "grand_total": 1000.0,
                "cluster_w": 0.0,
                "broad_w": 0.0,
                "cash_w": 100.0,
                "top5_w": 100.0,
                "breaches": [],
            },
            prior_as_of,
        )
        prior_observation = briefing.observation_snapshot_dict(
            [], [], [], prior_as_of
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            briefings = root / "briefings"
            prompts = root / "prompts"
            briefings.mkdir()
            prompts.mkdir()
            (briefings / "weights_2026-09-17.json").write_text(
                json.dumps(prior_weights), encoding="utf-8"
            )
            (briefings / "observations_2026-09-17.json").write_text(
                json.dumps(prior_observation), encoding="utf-8"
            )
            with (
                patch.object(briefing, "BRIEFINGS_DIR", briefings),
                patch.object(briefing, "OUT_DIR", prompts),
                patch.object(
                    briefing, "load_portfolio_context", return_value=({}, "_No context._")
                ),
            ):
                prompt = briefing.build_prompt(
                    holdings,
                    [{"label": "Brokerage (…7810)", "total": 1000.0, "tax_bucket": "taxable"}],
                    1000.0,
                    as_of,
                    "unused",
                    results=[],
                )
        self.assertIn("**Output mode:** ROUTINE DAY", prompt)
        self.assertIn("**Candidate refresh:** no", prompt)
        self.assertNotIn("Refresh the candidate list.", prompt)

    def test_decision_prompt_refreshes_candidates_after_weight_change(self):
        as_of = datetime(2026, 9, 18, 16, 0, tzinfo=ET)
        prior_as_of = datetime(2026, 9, 17, 16, 0, tzinfo=ET)
        holding = consolidated("AMD", 100_000.0, 100.0, **AMD_ONLY)
        prior_weights = briefing.snapshot_dict(
            [{**holding, "weight": 90.0, "market_value": 90_000.0}],
            {
                "grand_total": 100_000.0,
                "cluster_w": 90.0,
                "broad_w": 90.0,
                "cash_w": 10.0,
                "top5_w": 90.0,
                "breaches": ["AMD"],
            },
            prior_as_of,
        )
        policy = briefing.briefing_output_policy(
            [holding],
            [],
            [holding],
            briefing.snapshot_dict(
                [holding],
                {
                    "grand_total": 100_000.0,
                    "cluster_w": 100.0,
                    "broad_w": 100.0,
                    "cash_w": 0.0,
                    "top5_w": 100.0,
                    "breaches": ["AMD"],
                },
                as_of,
            ),
            prior_weights,
            {"positions": [], "reviews": []},
            {"positions": [], "reviews": []},
            as_of,
        )
        self.assertEqual(policy.mode, "DECISION DAY")
        self.assertTrue(policy.refresh_candidates)
        self.assertIn("AMD", policy.material_symbols)

    def test_harvest_flag_uses_policy_cutoff(self):
        as_of = datetime(2026, 8, 12, tzinfo=ET)

        def flags_for(gain: float) -> str:
            results = [
                {
                    "tax_bucket": "taxable",
                    "holdings": [
                        {
                            "symbol": "ZZZ",
                            "total_cost": 1000.0,
                            "price_paid": 10.0,
                            "total_gain": gain,
                            "lots": [],
                            "market_value": 750.0,
                        }
                    ],
                }
            ]
            return "\n".join(portfolio.collect_tax_flags(results, as_of))

        self.assertNotIn("ZZZ", flags_for(POLICY.harvest_loss_dollars + 1))
        self.assertIn("ZZZ", flags_for(POLICY.harvest_loss_dollars - 1))

    def test_st_lt_flags_use_policy_cutoffs(self):
        as_of = datetime(2026, 8, 12, tzinfo=ET)

        def flags_for(gain: float, acquired: datetime) -> str:
            results = [
                {
                    "tax_bucket": "taxable",
                    "holdings": [
                        {
                            "symbol": "ZZZ",
                            "total_cost": 10_000.0,
                            "price_paid": 10.0,
                            "total_gain": gain,
                            "date_acquired": acquired,
                            "lots": [],
                            "market_value": 10_000.0 + gain,
                        }
                    ],
                }
            ]
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

    def test_prompt_wires_policy_values_and_hides_raw_data(self):
        as_of = datetime(2026, 8, 12, 16, 0, tzinfo=ET)
        holdings = [consolidated("AMD", 100_000.0, 100.0, **AMD_ONLY)]
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
        extras = [
            s for s in POLICY.sleeves["broad_ai_cycle"] if s not in POLICY.sleeves["direct_ai_semi"]
        ]
        self.assertIn("Broad AI-cycle liquid (direct + " + " ".join(extras) + ")", prompt)
        self.assertIn("Crypto (" + " ".join(POLICY.sleeves["crypto"]) + ")", prompt)
        self.assertIn("Payments (" + " ".join(POLICY.sleeves["payments"]) + ")", prompt)
        self.assertIn("Broad index (" + " ".join(POLICY.sleeves["broad_index"]) + ")", prompt)
        self.assertIn(f"work toward {POLICY.risk_off_glide_pct:g}%", prompt)
        self.assertIn(
            f"weight ≥ {POLICY.analyze_weight_floor_pct:g}%",
            prompt,
        )
        self.assertIn("$10k purchase examples are unavailable", prompt)
        # The raw per-account dump and empty accounts must not leak into the prompt.
        self.assertNotIn("Portfolio (live from E*TRADE", prompt)
        self.assertNotIn("9627", prompt)
        self.assertIn("7810", prompt)

    def test_consolidate_hides_gain_pct_when_any_gain_is_unknown(self):
        results = [
            {
                "label": "Brokerage (…7810)",
                "total_value": 200.0,
                "tax_bucket": "taxable",
                "holdings": [
                    {
                        "symbol": "ZZZ",
                        "market_value": 100.0,
                        "price": 10.0,
                        "quantity": 10.0,
                        "total_cost": 80.0,
                        "total_gain": 20.0,
                    }
                ],
            },
            {
                "label": "IRA (…6183)",
                "total_value": 200.0,
                "tax_bucket": "traditional",
                "holdings": [
                    {
                        "symbol": "ZZZ",
                        "market_value": 100.0,
                        "price": 10.0,
                        "quantity": 10.0,
                        "total_cost": 80.0,
                        "total_gain": None,
                    }
                ],
            },
        ]
        holdings, _accounts = briefing.consolidate(results, 400.0)
        self.assertIsNone(holdings[0]["total_gain"])
        self.assertIsNone(holdings[0]["total_gain_pct"])

    def test_marginal_10k_reranks_top_five_after_sgov_to_top_five_transfer(self):
        holdings = [
            {"symbol": "SGOV", "market_value": 50_000.0},
            {"symbol": "VOO", "market_value": 40_000.0},
            {"symbol": "AAA", "market_value": 30_000.0},
            {"symbol": "BBB", "market_value": 20_000.0},
            {"symbol": "CCC", "market_value": 15_000.0},
            {"symbol": "DDD", "market_value": 10_000.0},
        ]
        total = sum(h["market_value"] for h in holdings)
        table = briefing.format_marginal_10k(
            holdings,
            total,
            cluster_mv=0.0,
            broad_mv=0.0,
            cash_mv=50_000.0,
            results=[
                {
                    "label": "Brokerage (…7810)",
                    "holdings": [{"symbol": "SGOV", "market_value": 50_000.0}],
                }
            ],
        )
        voo_row = next(line for line in table.splitlines() if line.startswith("| VOO |"))
        self.assertEqual(voo_row.split("|")[5].strip(), "0")

    def test_taxable_review_is_not_hidden_by_ira_gain(self):
        results = [
            {
                "label": "Brokerage (…7810)",
                "tax_bucket": "taxable",
                "holdings": [
                    {
                        "symbol": "ZZZ",
                        "quantity": 100.0,
                        "market_value": 6000.0,
                        "total_gain": -1000.0,
                    }
                ],
            },
            {
                "label": "IRA (…6183)",
                "tax_bucket": "traditional",
                "holdings": [
                    {
                        "symbol": "ZZZ",
                        "quantity": 100.0,
                        "market_value": 6000.0,
                        "total_gain": 3000.0,
                    }
                ],
            },
        ]
        reviews = briefing.taxable_harvest_reviews(results)
        self.assertEqual([row["symbol"] for row in reviews], ["ZZZ"])

        consolidated = [
            {
                "symbol": "ZZZ",
                "market_value": 12_000.0,
                "weight": 1.0,
                "total_gain": 2000.0,
                "total_gain_pct": 20.0,
                "taxable_mv": 6000.0,
            }
        ]
        rows = briefing.must_analyze_holdings(consolidated, {row["symbol"] for row in reviews})
        self.assertIn("harvest", rows[0]["analyze_reasons"])

    def test_observed_delta_ignores_price_only_move(self):
        position = {
            "account_ref": "abc",
            "account": "…7810",
            "symbol": "TE",
            "quantity": 100.0,
            "price": 4.33,
            "lots": [
                {
                    "acquired": "2026-06-05",
                    "quantity": 100.0,
                    "total_cost": 879.0,
                    "term_code": 2,
                }
            ],
        }
        prior = {
            "date": "2026-08-20",
            "positions": [position],
            "reviews": [],
        }
        today = {
            "date": "2026-08-21",
            "positions": [
                {
                    **position,
                    "price": 4.34,
                    "lots": [{**position["lots"][0], "term_code": 1}],
                }
            ],
            "reviews": [],
        }
        delta = briefing.format_observed_delta(today, prior, "prior.json")
        self.assertIn("NO EXECUTION DETECTED", delta)
        self.assertNotIn("QUANTITY_INCREASE", delta)
        self.assertNotIn("QUANTITY_DECREASE", delta)

    def test_observed_delta_detects_partial_sale(self):
        prior = {
            "date": "2026-08-20",
            "positions": [
                {
                    "account_ref": "abc",
                    "account": "…7810",
                    "symbol": "TE",
                    "quantity": 100.0,
                    "lots": [],
                }
            ],
            "reviews": [],
        }
        today = {
            "date": "2026-08-21",
            "positions": [
                {
                    "account_ref": "abc",
                    "account": "…7810",
                    "symbol": "TE",
                    "quantity": 40.0,
                    "lots": [],
                }
            ],
            "reviews": [],
        }
        delta = briefing.format_observed_delta(today, prior, "prior.json")
        self.assertIn("QUANTITY_DECREASE", delta)
        self.assertIn("100 → 40", delta)
        self.assertIn("execution is not confirmed", delta)

    def test_observation_snapshot_hashes_account_key(self):
        secret_key = "secret-account-key"
        snapshot = briefing.observation_snapshot_dict(
            [
                {
                    "label": "Brokerage (…7810)",
                    "account_id_key": secret_key,
                    "tax_bucket": "taxable",
                    "holdings": [
                        {
                            "symbol": "TE",
                            "quantity": 10.0,
                            "price": 4.0,
                            "market_value": 40.0,
                            "lots": [],
                        }
                    ],
                }
            ],
            [],
            [],
            datetime(2026, 8, 21, tzinfo=ET),
        )
        self.assertNotIn(secret_key, json.dumps(snapshot))
        self.assertEqual(snapshot["positions"][0]["account"], "…7810")

    def _amd_two_lot_results(self):
        lot = dict(price=50.0, total_cost=5_000.0, market_value=10_000.0)
        return [
            {
                "label": "Brokerage (…7810)",
                "tax_bucket": "taxable",
                "total_value": 20_000.0,
                "holdings": [
                    {
                        "symbol": "AMD", "quantity": 200.0, "market_value": 20_000.0,
                        "price": 100.0, "total_cost": 10_000.0, "total_gain": 10_000.0,
                        "lots": [
                            {**lot, "qty": 100.0, "total_gain": 5_000.0, "term_code": 1,
                             "acquired": datetime(2023, 1, 5, tzinfo=ET)},
                            {**lot, "qty": 100.0, "total_gain": 5_000.0, "term_code": 2,
                             "acquired": datetime(2026, 3, 2, tzinfo=ET)},
                        ],
                    }
                ],
            },
            {
                "label": "Traditional IRA (…6183)",
                "tax_bucket": "traditional",
                "total_value": 5_000.0,
                "holdings": [
                    {"symbol": "AMD", "quantity": 50.0, "market_value": 5_000.0, "price": 100.0,
                     "total_cost": 4_000.0, "total_gain": 1_000.0,
                     "lots": [{**lot, "qty": 50.0, "total_gain": 1_000.0,
                               "acquired": datetime(2025, 1, 5, tzinfo=ET)}]},
                ],
            },
        ]

    def test_holdings_table_splits_taxable_gain_by_term_and_ignores_ira(self):
        as_of = datetime(2026, 9, 18, tzinfo=ET)
        results = self._amd_two_lot_results()
        holdings, _ = briefing.consolidate(results, 25_000.0)
        table = briefing.format_holdings_table(holdings, results, as_of)
        row = next(line for line in table.splitlines() if line.startswith("| AMD"))
        self.assertIn("taxable $20,000 · IRA $5,000", row)
        # The IRA gain has no tax effect, so only the two taxable lots appear by term.
        self.assertIn("LT +$5,000 · ST +$5,000", row)

    def test_focus_lots_name_each_taxable_lot_and_skip_ira(self):
        as_of = datetime(2026, 9, 18, tzinfo=ET)
        block = briefing.format_focus_lots(self._amd_two_lot_results(), ("AMD",), as_of)
        lot_rows = [line for line in block.splitlines() if line.startswith("| AMD")]
        self.assertEqual(len(lot_rows), 2)
        self.assertIn("2023-01-05", lot_rows[0])
        self.assertIn("| LT |", lot_rows[0])
        self.assertIn("| ST |", lot_rows[1])
        self.assertNotIn("…6183", block)
        self.assertEqual(briefing.format_focus_lots(self._amd_two_lot_results(), (), as_of), "")

    def test_blank_owner_profile_counts_as_not_supplied(self):
        blank = {"owner_profile": {"horizon": "", "marginal_tax_rate": "UNKNOWN"}}
        self.assertIn("**Owner profile:** not supplied", briefing.format_context_block(blank))
        filled = {"owner_profile": {"horizon": "15+ years"}}
        block = briefing.format_context_block(filled)
        self.assertIn("15+ years", block)
        self.assertNotIn("Decision history", block)

    def test_routine_day_has_one_focus_list_with_reasons(self):
        as_of = datetime(2026, 9, 18, 16, 0, tzinfo=ET)
        amd = consolidated("AMD", 20_000.0, 20.0, **AMD_ONLY)
        voo = consolidated("VOO", 10_000.0, 10.0)
        snap = briefing.snapshot_dict(
            [amd, voo],
            {"grand_total": 100_000.0, "cluster_w": 20.0, "broad_w": 20.0,
             "cash_w": 0.0, "top5_w": 30.0, "breaches": ["AMD"]},
            as_of,
        )
        observation = {"positions": [], "reviews": []}
        policy = briefing.briefing_output_policy(
            [amd, voo], [], [amd], snap, snap, observation, observation, as_of
        )
        self.assertEqual(policy.mode, "ROUTINE DAY")
        self.assertEqual(policy.material_symbols, ("AMD",))
        self.assertEqual(policy.monitor_symbols, ("VOO",))
        block = briefing.format_focus_block(policy)
        self.assertIn("**AMD** — above", block)
        self.assertIn("**Monitor only**", block)
        self.assertIn("VOO", block.split("**Monitor only**")[1])

    def test_routine_day_keeps_material_losses_and_near_limit_names_in_focus(self):
        as_of = datetime(2026, 9, 18, 16, 0, tzinfo=ET)
        cap = POLICY.single_name_cap_pct
        near = consolidated("MU", 14_500.0, cap - 0.5)
        loser = consolidated(
            "ETHA", 10_000.0, 10.0, total_gain=-6_000.0, total_gain_pct=-37.5,
            taxable_mv=0.0, ira_mv=10_000.0,
        )
        steady = consolidated("VOO", 10_000.0, 10.0)
        holdings = [near, loser, steady]
        snap = briefing.snapshot_dict(
            holdings,
            {"grand_total": 100_000.0, "cluster_w": 0.0, "broad_w": 0.0,
             "cash_w": 0.0, "top5_w": 34.5, "breaches": []},
            as_of,
        )
        observation = {"positions": [], "reviews": []}
        policy = briefing.briefing_output_policy(
            holdings, [], [], snap, snap, observation, observation, as_of
        )
        self.assertEqual(policy.mode, "ROUTINE DAY")
        reasons = dict(policy.focus_reasons)
        self.assertIn(f"within 1 pp of {cap:g}% single-name limit", reasons["MU"])
        self.assertIn("material loss (-38%)", reasons["ETHA"])
        self.assertEqual(policy.monitor_symbols, ("VOO",))

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
                    "holdings": [
                        {
                            "symbol": "AMZN",
                            "price": 200.0,
                            "market_value": 2000.0,
                            "cost_per_share": 180.0,
                            "total_cost": 1800.0,
                            "total_gain": 200.0,
                            "lots": [],
                            "date_acquired": datetime(2021, 1, 12, tzinfo=ET),
                        }
                    ],
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
            portfolio.tax_bucket_from_account(
                {
                    "accountType": "INDIVIDUAL",
                    "accountName": "Retirement",
                    "accountDesc": "Retirement savings",
                    "accountMode": "CASH",
                }
            ),
            "taxable",
        )

    def test_unknown_type_falls_back_to_name(self):
        self.assertEqual(
            portfolio.tax_bucket_from_account(
                {
                    "accountType": "UNKNOWN",
                    "accountName": "SEP IRA",
                }
            ),
            "traditional",
        )
        self.assertEqual(
            portfolio.tax_bucket_from_account(
                {
                    "accountType": "",
                    "accountName": "Roth leftover",
                }
            ),
            "roth",
        )


if __name__ == "__main__":
    unittest.main()

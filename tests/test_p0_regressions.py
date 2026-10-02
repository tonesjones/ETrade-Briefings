"""Regression tests for the P0 data fixes (commit 2af10f2)."""

import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import briefing_snapshots as snapshots
import build_briefing_prompt as briefing
import get_portfolio as portfolio

ET = ZoneInfo("America/New_York")
FIXTURE = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"


def position(symbol: str, market_value: float, **overrides) -> dict:
    row = {
        "symbol": symbol,
        "quantity": market_value / 100.0,
        "price": 100.0,
        "market_value": market_value,
        "price_paid": None,
        "cost_per_share": None,
        "total_cost": None,
        "total_gain": None,
        "total_gain_pct": None,
        "date_acquired": None,
        "lots": [],
    }
    row.update(overrides)
    return row


def taxable_result(holdings: list[dict]) -> dict:
    return {
        "label": "Brokerage (…1111)",
        "account_ref": "acct-1",
        "tax_bucket": "taxable",
        "total_value": sum(h["market_value"] for h in holdings),
        "holdings": holdings,
    }


def run_offline_build(root: Path, now: datetime, extra_args=()) -> int:
    """Run main() on the fixture with every path patched into root."""
    briefings = root / "briefings"
    prompts = root / "prompts"
    with ExitStack() as stack:
        stack.enter_context(patch.object(briefing, "BRIEFINGS_DIR", briefings))
        stack.enter_context(patch.object(briefing, "OUT_DIR", prompts))
        stack.enter_context(patch.object(briefing, "CONTEXT_PATH", root / "missing.json"))
        stack.enter_context(patch.object(briefing, "copy_to_clipboard", return_value=True))
        stack.enter_context(patch.object(portfolio, "PROJECT_ROOT", root))
        stack.enter_context(patch.object(briefing, "now_et", return_value=now))
        stack.enter_context(redirect_stdout(io.StringIO()))
        stack.enter_context(redirect_stderr(io.StringIO()))
        return briefing.main(["--input-file", str(FIXTURE), *extra_args])


class Top5ExcludesCashTests(unittest.TestCase):
    VALUES = {
        "CASH": 30_000.0, "SGOV": 25_000.0, "NVDA": 10_000.0, "AMD": 9_000.0,
        "MU": 8_000.0, "VOO": 7_000.0, "MA": 6_000.0, "XYZ": 5_000.0,
    }

    def test_helper_skips_cash_and_cash_equivalents(self):
        # CASH and SGOV are cash symbols; top five are NVDA AMD MU VOO MA = 40k.
        self.assertEqual(briefing.top5_noncash_weight(self.VALUES, 100_000), 40.0)

    def test_cash_never_counts_even_when_largest(self):
        values = {"CASH": 90_000.0, "AAA": 10_000.0}
        self.assertEqual(briefing.top5_noncash_weight(values, 100_000), 10.0)

    def test_zero_total_is_zero(self):
        self.assertEqual(briefing.top5_noncash_weight({"AAA": 1.0}, 0), 0.0)

    def test_prompt_reports_noncash_top5(self):
        holdings = [position(s, v) for s, v in self.VALUES.items()]
        results = [taxable_result(holdings)]
        consolidated, accounts = briefing.consolidate(results, 100_000.0)
        as_of = datetime(2026, 9, 18, 16, tzinfo=ET)
        with patch.object(
            briefing, "load_portfolio_context", return_value=({}, "_No context._")
        ):
            built = briefing.build_briefing(
                consolidated, accounts, 100_000.0, as_of, "block", results
            )
        self.assertIn("**Top-5 non-cash:** 40.0%", built.prompt)


class DeepLoserStaysInReviewTests(unittest.TestCase):
    def setUp(self):
        self.results = [
            taxable_result(
                [
                    position("VOO", 396_000.0),
                    position(
                        "DEEP",
                        4_000.0,
                        total_cost=20_000.0,
                        total_gain=-16_000.0,
                        total_gain_pct=-80.0,
                    ),
                ]
            )
        ]

    def test_harvest_review_keeps_deep_loser(self):
        symbols = [row["symbol"] for row in briefing.taxable_harvest_reviews(self.results)]
        self.assertIn("DEEP", symbols)

    def test_must_analyze_flags_loss(self):
        holdings, _accounts = briefing.consolidate(self.results, 400_000.0)
        rows = {r["symbol"]: r for r in briefing.must_analyze_holdings(holdings)}
        self.assertIn("DEEP", rows)
        self.assertIn("loss", rows["DEEP"]["analyze_reasons"])

    def test_review_size_uses_larger_of_value_and_cost(self):
        self.assertEqual(
            briefing.review_size({"market_value": 4_000.0, "total_cost": 20_000.0}),
            20_000.0,
        )


class ResidualCashFailsClosedTests(unittest.TestCase):
    def test_unexplained_residual_raises(self):
        with self.assertRaises(portfolio.PortfolioDataError):
            portfolio.apply_cash_lot(
                [], account_total=90_000, position_total=50_000, cash_from_balance=None
            )

    def test_reported_cash_is_added(self):
        holdings: list[dict] = []
        portfolio.apply_cash_lot(
            holdings, account_total=90_000, position_total=50_000, cash_from_balance=40_000.0
        )
        self.assertEqual([h["symbol"] for h in holdings], ["CASH"])
        self.assertEqual(holdings[0]["market_value"], 40_000.0)


class BackdatedBuildIgnoresNewerSnapshotsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for prefix in ("weights", "observations"):
            for day in ("2026-10-01", "2026-09-15", "2026-09-20"):
                (self.dir / f"{prefix}_{day}.json").write_text(
                    json.dumps({"date": day}), encoding="utf-8"
                )
        self.as_of = datetime(2026, 9, 20, 16, tzinfo=ET)

    def test_prior_snapshot_is_strictly_older(self):
        snap, label = snapshots.load_prior_snapshot(self.as_of, self.dir, self.dir)
        self.assertEqual(label, "weights_2026-09-15.json")
        self.assertEqual(snap["date"], "2026-09-15")

    def test_prior_observation_is_strictly_older(self):
        obs, label = snapshots.load_prior_observation(self.as_of, self.dir)
        self.assertEqual(label, "observations_2026-09-15.json")
        self.assertEqual(obs["date"], "2026-09-15")

    def test_only_newer_files_means_no_prior(self):
        early = datetime(2026, 9, 1, 16, tzinfo=ET)
        snap, _label = snapshots.load_prior_snapshot(early, self.dir, self.dir)
        obs, _label = snapshots.load_prior_observation(early, self.dir)
        self.assertIsNone(snap)
        self.assertIsNone(obs)


class StaleOfflineInputRefusedTests(unittest.TestCase):
    NOW = datetime(2026, 10, 2, 9, 0, tzinfo=ET)

    def test_stale_input_returns_3_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_offline_build(root, self.NOW), 3)
            self.assertEqual(list(root.rglob("weights_*.json")), [])
            self.assertEqual(list(root.rglob("observations_*.json")), [])
            self.assertEqual(list(root.rglob("daily_briefing_prompt_*.md")), [])

    def test_allow_stale_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_offline_build(root, self.NOW, ["--allow-stale"]), 0)
            self.assertTrue((root / "briefings" / "weights_2026-09-18.json").exists())


class AccountTypeFallbackTests(unittest.TestCase):
    def test_unknown_type_with_sept_in_name_is_taxable_and_warns(self):
        with self.assertLogs("get_portfolio", level="WARNING") as logs:
            bucket = portfolio.tax_bucket_from_account(
                {"accountType": "PRIME", "accountDesc": "Sept Savings Brokerage"}
            )
        self.assertEqual(bucket, "taxable")
        self.assertTrue(any("Unrecognized" in line for line in logs.output))

    def test_beneficiary_ira_is_traditional(self):
        self.assertEqual(
            portfolio.tax_bucket_from_account({"accountType": "BENEFICIARYIRA"}),
            "traditional",
        )

    def test_unknown_type_with_roth_name_is_roth(self):
        with self.assertLogs("get_portfolio", level="WARNING"):
            bucket = portfolio.tax_bucket_from_account(
                {"accountType": "X", "accountName": "My Roth"}
            )
        self.assertEqual(bucket, "roth")


class TermFromDatesTests(unittest.TestCase):
    AS_OF = datetime(2026, 9, 18, 16, tzinfo=ET)

    def conflicting_lot(self) -> dict:
        return {
            "qty": 10.0,
            "price": 50.0,
            "total_cost": 500.0,
            "total_gain": 25.0,
            "acquired": datetime(2026, 8, 19, 12, tzinfo=ET),
            "term_code": 1,
        }

    def test_acquired_date_beats_term_code(self):
        lot = self.conflicting_lot()
        self.assertEqual(portfolio.term_from_lot(lot, self.AS_OF), "ST")
        self.assertTrue(portfolio.lot_term_conflict(lot, self.AS_OF))

    def test_agreeing_code_is_not_a_conflict(self):
        lot = {**self.conflicting_lot(), "term_code": 2}
        self.assertFalse(portfolio.lot_term_conflict(lot, self.AS_OF))

    def test_days_to_long_term(self):
        lot = {"acquired": datetime(2025, 10, 10, tzinfo=ET)}
        as_of = datetime(2026, 10, 2, tzinfo=ET)
        self.assertEqual(portfolio.days_to_long_term(lot, as_of), 9)
        self.assertEqual(portfolio.long_term_date(lot["acquired"]).date().isoformat(), "2026-10-11")

    def test_long_term_lot_has_no_countdown(self):
        lot = {"acquired": datetime(2024, 1, 2, tzinfo=ET)}
        self.assertIsNone(portfolio.days_to_long_term(lot, datetime(2026, 10, 2, tzinfo=ET)))

    def test_focus_lot_table_shows_countdown_and_conflict(self):
        results = [
            taxable_result(
                [position("AAA", 500.0, quantity=10.0, lots=[self.conflicting_lot()])]
            )
        ]
        text = briefing.format_focus_lots(results, ["AAA"], self.AS_OF)
        self.assertIn("Turns LT in", text)
        self.assertIn("⚠", text)
        self.assertIn("2027-08-20", text)


class PromptContractTests(unittest.TestCase):
    def test_prompt_has_briefing_id_and_web_guard(self):
        now = datetime(2026, 9, 18, 18, 0, tzinfo=ET)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_offline_build(root, now), 0)
            meta = json.loads(
                (root / "briefings" / "briefing_2026-09-18.json").read_text(encoding="utf-8")
            )
            prompt = (root / "prompts" / "daily_briefing_prompt_2026-09-18.md").read_text(
                encoding="utf-8"
            )
        self.assertRegex(meta["briefing_id"], r"^2026-09-18-[0-9a-f]{8}$")
        self.assertIn(f'"briefing_id": "{meta["briefing_id"]}"', prompt)
        self.assertIn("Actions JSON", prompt)
        self.assertIn("If you cannot search the web", prompt)


if __name__ == "__main__":
    unittest.main()

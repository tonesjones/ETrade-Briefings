"""Regressions for issues found in the final review of the reply contract."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import get_portfolio as portfolio
import import_response as imp
from briefing_formatting import fmt_qty

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 9, 18, 16, 0, tzinfo=ET)


def _account(label, bucket, holdings, total):
    return {"label": label, "tax_bucket": bucket, "holdings": holdings, "total_value": total}


def _reply(trades):
    return {"briefing_id": "b", "web_research": True, "names": [], "trades": trades}


class OptionValueTests(unittest.TestCase):
    def test_option_sale_uses_contract_value_not_quote(self):
        # 2 contracts quoted at $15 are worth $3,000 (x100 multiplier).
        option = {
            "symbol": "NVDA 2026-12-18 200C", "security_type": "OPTN", "underlying": "NVDA",
            "quantity": 2.0, "price": 15.0, "market_value": 3_000.0,
            "total_cost": 2_000.0, "total_gain": 1_000.0,
            "lots": [{"qty": 2.0, "total_cost": 2_000.0,
                      "acquired": datetime(2026, 6, 1, tzinfo=ET), "term_code": None}],
        }
        results = [_account("Brokerage (…1111)", "taxable", [option], 3_000.0)]
        trade = {"id": "t1", "side": "SELL", "account": "…1111", "ticker": option["symbol"],
                 "quantity": 2, "lot_acquired": "2026-06-01"}
        import json
        text = "```json\n" + json.dumps(_reply([trade])) + "\n```"
        checks, _parsed, computed = imp.validate_reply(
            text, {"briefing_id": "b", "focus_symbols": []}, results, 3_000.0, AS_OF
        )
        rec = computed["trades"][0]
        self.assertAlmostEqual(rec["amount"], 3_000.0)
        self.assertAlmostEqual(rec["realized_gain"], 1_000.0)


class WashSaleUnderlyingTests(unittest.TestCase):
    def test_loss_sale_with_call_buy_on_same_underlying_is_rejected(self):
        stock = {
            "symbol": "NVDA", "underlying": "NVDA", "quantity": 10.0, "price": 100.0,
            "market_value": 1_000.0, "total_cost": 5_000.0, "total_gain": -4_000.0,
            "lots": [{"qty": 10.0, "total_cost": 5_000.0,
                      "acquired": datetime(2025, 1, 2, tzinfo=ET), "term_code": None}],
        }
        cash = portfolio.cash_holding(10_000.0)
        results = [_account("Brokerage (…1111)", "taxable", [stock, cash], 11_000.0)]
        trades = [
            {"id": "t1", "side": "SELL", "account": "…1111", "ticker": "NVDA",
             "quantity": 10, "lot_acquired": "2025-01-02"},
            {"id": "t2", "side": "BUY", "account": "…1111", "ticker": "NVDA 2026-12-18 200C",
             "quantity": 1, "est_price": 500, "funded_by": "CASH"},
        ]
        import json
        text = "```json\n" + json.dumps(_reply(trades)) + "\n```"
        checks, _p, _c = imp.validate_reply(
            text, {"briefing_id": "b", "focus_symbols": []}, results, 11_000.0, AS_OF
        )
        self.assertIn("wash_sale_buy_in_proposal", [c["code"] for c in checks.items])


class UnknownBasisTests(unittest.TestCase):
    def test_zero_basis_lot_warns_and_reports_no_gain(self):
        holding = {
            "symbol": "AAA", "quantity": 100.0, "price": 50.0, "market_value": 5_000.0,
            "total_cost": None, "total_gain": None,
            "lots": [{"qty": 100.0, "total_cost": 0.0,
                      "acquired": datetime(2025, 1, 2, tzinfo=ET), "term_code": None}],
        }
        results = [_account("Brokerage (…1111)", "taxable", [holding], 5_000.0)]
        trade = {"id": "t1", "side": "SELL", "account": "…1111", "ticker": "AAA",
                 "quantity": 100, "lot_acquired": "2025-01-02"}
        import json
        text = "```json\n" + json.dumps(_reply([trade])) + "\n```"
        checks, _p, computed = imp.validate_reply(
            text, {"briefing_id": "b", "focus_symbols": []}, results, 5_000.0, AS_OF
        )
        self.assertIn("basis_unknown", [c["code"] for c in checks.items])
        self.assertIsNone(computed["trades"][0].get("realized_gain"))
        self.assertEqual(computed["tax_summary"]["taxable_net"], 0.0)


class ExactQuantityTests(unittest.TestCase):
    def test_lot_quantities_are_exact(self):
        self.assertEqual(fmt_qty(2.3456), "2.3456")
        self.assertEqual(fmt_qty(12_345.0), "12,345")
        self.assertEqual(fmt_qty(0.123456789), "0.123457")

    def test_focus_lot_table_shows_exact_quantity(self):
        lot = {"qty": 2.3456, "total_cost": 200.0, "total_gain": 34.56,
               "acquired": datetime(2025, 1, 2, tzinfo=ET), "term_code": None}
        results = [_account("Brokerage (…1111)", "taxable", [
            {"symbol": "AAA", "quantity": 2.3456, "lots": [lot]}], 300.0)]
        table = briefing.format_focus_lots(results, ["AAA"], AS_OF)
        self.assertIn("| 2.3456 |", table)


class PartialRunSidecarTests(unittest.TestCase):
    def test_partial_run_keeps_the_last_good_sidecar(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        result = briefing.BriefingBuildResult(
            prompt="p", weights_snapshot={}, observation_snapshot={},
            meta={"briefing_id": "new", "portfolio_file": "briefing_2026-09-18_portfolio.txt"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "briefings").mkdir()
            (root / "briefings" / "briefing_latest.json").write_text('{"briefing_id": "good"}')
            with (
                patch.object(briefing, "BRIEFINGS_DIR", root / "briefings"),
                patch.object(briefing, "OUT_DIR", root / "prompts"),
                patch.object(portfolio, "PROJECT_ROOT", root),
            ):
                briefing.save_briefing_result(result, "**INCOMPLETE DATA** text only", AS_OF)
            latest = (root / "briefings" / "briefing_latest.json").read_text()
            self.assertIn("good", latest)
            self.assertFalse((root / "briefings" / "briefing_2026-09-18_portfolio.txt").exists())


class AccountNameFallbackTests(unittest.TestCase):
    def test_underscored_and_restored_markers(self):
        bucket = portfolio.tax_bucket_from_account
        with self.assertLogs("get_portfolio", level="WARNING"):
            self.assertEqual(bucket({"accountType": "X", "accountName": "ROTH_IRA"}), "roth")
            self.assertEqual(
                bucket({"accountType": "BENEFICIARY_SEP", "accountDesc": "SEP Plan"}),
                "traditional",
            )
            self.assertEqual(bucket({"accountType": "X", "accountDesc": "Coverdell ESA"}),
                             "traditional")
            self.assertEqual(
                bucket({"accountType": "PRIME", "accountDesc": "Sept Savings Brokerage"}),
                "taxable",
            )


if __name__ == "__main__":
    unittest.main()

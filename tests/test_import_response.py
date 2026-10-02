import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import get_portfolio as portfolio
import import_response as imp

FIXTURE = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"
BRIEFING_ID = "brief-test-1"
TAXABLE = "…1111"
ROTH = "…2222"


def fixture_text(mutate=None) -> str:
    """The sanitized fixture, optionally with its JSON payload edited by `mutate`."""
    text = FIXTURE.read_text(encoding="utf-8")
    if mutate is None:
        return text
    head, rest = text.split(portfolio.PORTABLE_BEGIN, 1)
    raw, tail = rest.split(portfolio.PORTABLE_END, 1)
    payload = json.loads(raw)
    mutate(payload)
    return (
        head + portfolio.PORTABLE_BEGIN + "\n" + json.dumps(payload, indent=2) + "\n"
        + portfolio.PORTABLE_END + tail
    )


def make_loss(payload):
    aaa = payload["accounts"][0]["holdings"][0]
    aaa["total_cost"], aaa["total_gain"] = 600.0, -100.0
    aaa["price_paid"] = aaa["cost_per_share"] = 120.0
    aaa["lots"][0]["total_cost"], aaa["lots"][0]["total_gain"] = 600.0, -100.0
    aaa["lots"][0]["price"] = 120.0


def make_short_term(payload):
    aaa = payload["accounts"][0]["holdings"][0]
    aaa["date_acquired"] = aaa["lots"][0]["acquired"] = "2025-10-10T12:00:00-04:00"
    aaa["lots"][0]["term_code"] = 2


def sell(tid="t1", account=TAXABLE, ticker="AAA", quantity=2, lot="2025-01-02", **extra):
    trade = {
        "id": tid, "side": "SELL", "account": account, "ticker": ticker,
        "quantity": quantity, "lot_acquired": lot, "est_price": None, "funded_by": "CASH",
    }
    trade.update(extra)
    return trade


def buy(tid="t2", account=TAXABLE, ticker="BBB", quantity=2, funded_by="CASH", **extra):
    trade = {
        "id": tid, "side": "BUY", "account": account, "ticker": ticker,
        "quantity": quantity, "lot_acquired": None, "est_price": None, "funded_by": funded_by,
    }
    trade.update(extra)
    return trade


def names(*, skip=()):
    all_names = {
        "AAA": {
            "ticker": "AAA", "view": "ATTRACTIVE", "action": "KEEP",
            "sources": [{"url": "https://example.com/a", "date": "2026-09-10"}],
            "trigger": "guidance cut",
        },
        "BBB": {
            "ticker": "BBB", "view": "NEUTRAL", "action": "NO_ACTION",
            "sources": [], "trigger": "next earnings",
        },
    }
    return [v for k, v in all_names.items() if k not in skip]


def reply_text(trades=None, *, briefing_id=BRIEFING_ID, web_research=True, names_list=None):
    payload = {
        "briefing_id": briefing_id,
        "web_research": web_research,
        "names": names() if names_list is None else names_list,
        "trades": trades or [],
    }
    return "Analysis...\n\n```json\n" + json.dumps(payload, indent=2) + "\n```\n"


class ImportResponseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.briefings = root / "briefings"
        self.responses = root / "responses"
        self.briefings.mkdir()
        for target, value in (
            ("BRIEFINGS_DIR", self.briefings),
            ("RESPONSES_DIR", self.responses),
        ):
            patcher = patch.object(imp, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write_briefing(fixture_text())

    def write_briefing(self, portfolio_text: str) -> None:
        (self.briefings / "portfolio_2026-09-18.txt").write_text(portfolio_text, encoding="utf-8")
        meta = {
            "schema_version": 1,
            "briefing_id": BRIEFING_ID,
            "as_of": "2026-09-18T16:00:00-04:00",
            "portfolio_file": "portfolio_2026-09-18.txt",
            "focus_symbols": ["AAA", "BBB"],
            "monitor_symbols": [],
        }
        for name in ("briefing_2026-09-18.json", "briefing_latest.json"):
            (self.briefings / name).write_text(json.dumps(meta), encoding="utf-8")

    def run_import(self, reply: str, engine="claude", extra=()):
        reply_file = Path(self._tmp.name) / "reply.txt"
        reply_file.write_text(reply, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = imp.main(["--engine", engine, "--input-file", str(reply_file), *extra])
        return code, out.getvalue()

    def validate(self, reply: str):
        _f, total, _n, results, as_of = portfolio.parse_portable_portfolio_text(
            (self.briefings / "portfolio_2026-09-18.txt").read_text(encoding="utf-8")
        )
        meta = json.loads((self.briefings / "briefing_latest.json").read_text(encoding="utf-8"))
        return imp.validate_reply(reply, meta, results, total, as_of)

    @staticmethod
    def codes(checks, level=None):
        return [c["code"] for c in checks.items if level is None or c["level"] == level]

    def test_valid_no_trade_reply_is_accepted_and_saved(self):
        code, out = self.run_import(reply_text())
        self.assertEqual(code, 0)
        self.assertIn("ACCEPTED", out)
        saved = json.loads((self.responses / "2026-09-18_claude.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "ACCEPTED")
        self.assertEqual(saved["briefing_id"], BRIEFING_ID)
        self.assertEqual(saved["engine"], "claude")
        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(len(saved["reply_sha256"]), 64)
        self.assertEqual(saved["parsed"]["briefing_id"], BRIEFING_ID)
        self.assertEqual(saved["checks"], [])
        self.assertEqual(
            (self.responses / "2026-09-18_claude.md").read_text(encoding="utf-8"), reply_text()
        )

    def test_wrong_briefing_id_is_rejected(self):
        code, out = self.run_import(reply_text(briefing_id="someone-else"))
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", out)
        self.assertIn("briefing_id_mismatch", out)
        self.assertTrue((self.responses / "2026-09-18_claude.json").exists())

    def test_missing_focus_name_is_an_error(self):
        checks, _p, _c = self.validate(reply_text(names_list=names(skip=("BBB",))))
        self.assertEqual(self.codes(checks, "ERROR"), ["missing_focus_name"])

    def test_invalid_enums_and_keep_source_warnings(self):
        bad = names()
        bad[0].update(view="GREAT", action="HODL")
        bad[1].update(action="KEEP", sources=[{"url": "u", "date": "sometime"}])
        checks, _p, _c = self.validate(reply_text(names_list=bad))
        self.assertEqual(sorted(self.codes(checks, "ERROR")), ["invalid_action", "invalid_view"])
        self.assertIn("keep_without_dated_source", self.codes(checks, "WARN"))

        late = names()
        late[0]["sources"] = [{"url": "u", "date": "2026-10-01"}]
        checks, _p, _c = self.validate(reply_text(names_list=late))
        self.assertEqual(self.codes(checks), ["source_after_as_of"])

    def test_sell_over_lot_quantity_is_an_error(self):
        checks, _p, _c = self.validate(reply_text([sell(quantity=6)]))
        self.assertIn("sell_exceeds_holding", self.codes(checks, "ERROR"))
        checks, _p, _c = self.validate(reply_text([sell("t1", quantity=3), sell("t2", quantity=3)]))
        self.assertIn("sell_exceeds_holding", self.codes(checks, "ERROR"))

    def test_sell_over_single_lot_with_other_lot_available(self):
        def two_lots(payload):
            aaa = payload["accounts"][0]["holdings"][0]
            aaa["quantity"], aaa["market_value"] = 8.0, 800.0
            aaa["lots"] = [
                dict(aaa["lots"][0], qty=5.0),
                dict(aaa["lots"][0], qty=3.0, total_cost=240.0, total_gain=60.0,
                     market_value=300.0, acquired="2025-03-03T12:00:00-05:00"),
            ]
            aaa["total_cost"], aaa["total_gain"] = 640.0, 160.0
            payload["accounts"][0]["holdings"][1]["quantity"] = 200.0
            payload["accounts"][0]["holdings"][1]["market_value"] = 200.0
            payload["accounts"][0]["total_value"] = 1000.0

        self.write_briefing(fixture_text(two_lots))
        checks, _p, _c = self.validate(reply_text([sell(quantity=4, lot="2025-03-03")]))
        self.assertIn("sell_exceeds_lot", self.codes(checks, "ERROR"))

    def test_taxable_sell_requires_lot_acquired(self):
        checks, _p, _c = self.validate(reply_text([sell(lot=None)]))
        self.assertEqual(self.codes(checks, "ERROR"), ["lot_acquired_required"])
        checks, _p, _c = self.validate(reply_text([sell(lot="2024-01-01")]))
        self.assertEqual(self.codes(checks, "ERROR"), ["lot_not_found"])

    def test_roth_sell_needs_no_lot_and_reports_no_cg_tax(self):
        checks, _p, computed = self.validate(
            reply_text([sell(account=ROTH, ticker="BBB", quantity=4, lot=None)])
        )
        self.assertFalse(checks.has_error)
        trade = computed["trades"][0]
        self.assertEqual(trade["tax_note"], "no CG tax")
        self.assertNotIn("realized_gain", trade)
        self.assertAlmostEqual(trade["amount"], 200.0)

    def test_buy_exceeding_account_cash_is_an_error(self):
        # Taxable holds 500 cash; 20 BBB at 50 costs 1,000.
        checks, _p, _c = self.validate(reply_text([buy(quantity=20)]))
        self.assertEqual(self.codes(checks, "ERROR"), ["insufficient_cash"])
        # The Roth account has no cash, so its own BUY fails even though taxable has plenty.
        checks, _p, _c = self.validate(reply_text([buy(account=ROTH, quantity=1)]))
        self.assertEqual(self.codes(checks, "ERROR"), ["insufficient_cash"])

    def test_same_account_sale_proceeds_fund_a_buy(self):
        checks, _p, computed = self.validate(
            reply_text([sell(quantity=5), buy(quantity=12, funded_by="t1")])
        )
        self.assertFalse(checks.has_error, checks.items)
        self.assertAlmostEqual(computed["cash_available_by_account"]["Brokerage (…1111)"], 1000.0)

    def test_cross_account_funded_by_is_an_error(self):
        trades = [sell("t1", ROTH, "BBB", 5, None), buy("t2", TAXABLE, "AAA", 1, funded_by="t1")]
        checks, _p, _c = self.validate(reply_text(trades))
        self.assertEqual(self.codes(checks, "ERROR"), ["funded_by_other_account"])

    def test_loss_sale_plus_buy_of_same_ticker_is_a_wash_sale_error(self):
        self.write_briefing(fixture_text(make_loss))
        checks, _p, computed = self.validate(
            reply_text([sell(quantity=2), buy("t2", TAXABLE, "AAA", 1)])
        )
        self.assertIn("wash_sale_buy_in_proposal", self.codes(checks, "ERROR"))
        self.assertIn("wash_sale_verify", self.codes(checks, "WARN"))
        self.assertAlmostEqual(computed["trades"][0]["realized_gain"], -40.0)
        self.assertAlmostEqual(computed["tax_summary"]["taxable_losses"], -40.0)

    def test_loss_sale_alone_warns_to_verify_but_is_accepted(self):
        self.write_briefing(fixture_text(make_loss))
        checks, _p, _c = self.validate(reply_text([sell(quantity=2)]))
        self.assertFalse(checks.has_error)
        self.assertEqual(self.codes(checks, "WARN"), ["wash_sale_verify"])

    def test_possible_wash_sale_from_recent_lot_in_other_account(self):
        def recent_aaa_in_roth(payload):
            payload["accounts"][1]["holdings"][0]["symbol"] = "AAA"
            payload["accounts"][1]["holdings"][0]["lots"][0]["acquired"] = (
                "2026-09-01T12:00:00-04:00"
            )

        def both(payload):
            make_loss(payload)
            recent_aaa_in_roth(payload)

        self.write_briefing(fixture_text(both))
        checks, _p, _c = self.validate(reply_text([sell(quantity=2)]))
        self.assertIn("wash_sale_possible", self.codes(checks, "WARN"))

    def test_short_term_gain_turning_long_term_soon_warns(self):
        self.write_briefing(fixture_text(make_short_term))
        checks, _p, computed = self.validate(reply_text([sell(quantity=2, lot="2025-10-10")]))
        self.assertFalse(checks.has_error)
        self.assertEqual(self.codes(checks, "WARN"), ["st_gain_turns_lt_soon"])
        trade = computed["trades"][0]
        self.assertEqual(trade["term"], "ST")
        self.assertEqual(trade["days_to_long_term"], 23)
        self.assertEqual(trade["lt_date"], "2026-10-11")
        self.assertAlmostEqual(trade["realized_gain"], 40.0)
        self.assertAlmostEqual(computed["tax_summary"]["taxable_st_gain"], 40.0)
        message = next(c["message"] for c in checks.items if c["code"] == "st_gain_turns_lt_soon")
        self.assertIn("$40.00", message)
        self.assertIn("2026-10-11", message)

    def test_long_term_gain_does_not_warn_and_is_summarized(self):
        checks, _p, computed = self.validate(reply_text([sell(quantity=2)]))
        self.assertEqual(checks.items, [])
        self.assertAlmostEqual(computed["tax_summary"]["taxable_lt_gain"], 40.0)
        self.assertEqual(computed["trades"][0]["term"], "LT")

    def test_post_trade_weights_are_computed_to_the_cent(self):
        # Sell 2 AAA (200) then buy 4 BBB (200) with the proceeds. Total stays 1,500.
        trades = [sell(quantity=2), buy(quantity=4, funded_by="t1")]
        checks, _p, computed = self.validate(reply_text(trades))
        self.assertFalse(checks.has_error, checks.items)
        weights = computed["weights"]
        self.assertAlmostEqual(weights["tickers"]["AAA"]["before"], 33.33, places=2)
        self.assertAlmostEqual(weights["tickers"]["AAA"]["after"], 20.00, places=2)
        self.assertAlmostEqual(weights["tickers"]["BBB"]["before"], 33.33, places=2)
        self.assertAlmostEqual(weights["tickers"]["BBB"]["after"], 46.67, places=2)
        self.assertAlmostEqual(weights["cash"]["before"], 33.33, places=2)
        self.assertAlmostEqual(weights["cash"]["after"], 33.33, places=2)
        self.assertAlmostEqual(weights["top5_noncash"]["before"], 66.67, places=2)
        self.assertAlmostEqual(weights["top5_noncash"]["after"], 66.67, places=2)
        self.assertAlmostEqual(weights["sleeves"]["direct_ai_semi"]["after"], 0.0, places=2)
        # BBB is above the 15% single-name cap after the buy.
        self.assertIn("single_name_cap_exceeded", self.codes(checks, "WARN"))

    def test_sell_to_cash_moves_value_to_cash(self):
        _c, _p, computed = self.validate(reply_text([sell(quantity=5)]))
        weights = computed["weights"]
        self.assertAlmostEqual(weights["tickers"]["AAA"]["after"], 0.0, places=2)
        self.assertAlmostEqual(weights["cash"]["after"], 66.67, places=2)
        self.assertAlmostEqual(weights["top5_noncash"]["after"], 33.33, places=2)

    def test_direct_ai_buy_over_no_add_limit_is_an_error(self):
        def make_nvda(payload):
            payload["accounts"][0]["holdings"][0]["symbol"] = "NVDA"

        self.write_briefing(fixture_text(make_nvda))  # NVDA is 33% (< 38%), so before is fine
        checks, _p, computed = self.validate(
            reply_text([buy(ticker="NVDA", quantity=2)])  # 200 more -> 46.7% after
        )
        self.assertIn("direct_sleeve_no_add", self.codes(checks, "ERROR"))
        direct = computed["weights"]["sleeves"]["direct_ai_semi"]
        self.assertAlmostEqual(direct["after"], 46.67, places=2)

    def test_buy_of_unheld_ticker_uses_est_price_or_warns(self):
        checks, _p, computed = self.validate(
            reply_text([buy(ticker="ZZZ", quantity=2, est_price=40)])
        )
        self.assertFalse(checks.has_error)
        self.assertAlmostEqual(computed["trades"][0]["amount"], 80.0)
        checks, _p, _c = self.validate(reply_text([buy(ticker="ZZZ", quantity=2)]))
        self.assertIn("cost_not_checked", self.codes(checks, "WARN"))
        self.assertFalse(checks.has_error)

    def test_invalid_side_account_and_quantity(self):
        trades = [
            sell("t1", quantity=1) | {"side": "HOLD"},
            sell("t2", account="…9999"),
            sell("t3", quantity=0),
            sell("t4", account="1111", quantity=1),  # digits-only tail still matches
        ]
        checks, _p, _c = self.validate(reply_text(trades))
        self.assertEqual(
            self.codes(checks, "ERROR"), ["invalid_side", "account_not_found", "invalid_quantity"]
        )

    def test_no_json_block_is_rejected_and_still_saved(self):
        code, out = self.run_import("I could not format JSON, sorry.")
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", out)
        self.assertIn("no_json_block", out)
        saved = json.loads((self.responses / "2026-09-18_claude.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "REJECTED")
        self.assertIsNone(saved["parsed"])
        self.assertTrue((self.responses / "2026-09-18_claude.md").exists())

    def test_invalid_json_is_rejected_and_last_block_wins(self):
        code, out = self.run_import("```json\n{not json}\n```")
        self.assertEqual(code, 1)
        self.assertIn("invalid_json", out)
        reply = '```json\n{"briefing_id": "old"}\n```\ntext\n' + reply_text()
        code, _out = self.run_import(reply, engine="grok")
        self.assertEqual(code, 0)

    def test_web_research_false_warns_and_rejects_trades(self):
        checks, _p, _c = self.validate(reply_text(web_research=False))
        self.assertEqual(self.codes(checks), ["web_research_not_confirmed"])
        code, out = self.run_import(reply_text([sell(quantity=1)], web_research=False))
        self.assertEqual(code, 1)
        self.assertIn("REJECTED", out)
        self.assertIn("web_research_required_for_trades", out)

    def test_second_save_gets_numeric_suffix(self):
        self.run_import(reply_text())
        self.run_import(reply_text())
        self.run_import(reply_text())
        names_saved = sorted(p.name for p in self.responses.iterdir())
        self.assertEqual(
            names_saved,
            [
                "2026-09-18_claude.json", "2026-09-18_claude.md",
                "2026-09-18_claude_2.json", "2026-09-18_claude_2.md",
                "2026-09-18_claude_3.json", "2026-09-18_claude_3.md",
            ],
        )

    def test_explicit_date_selects_the_dated_briefing(self):
        code, _out = self.run_import(reply_text(), engine="codex", extra=["--date", "2026-09-18"])
        self.assertEqual(code, 0)
        self.assertTrue((self.responses / "2026-09-18_codex.json").exists())
        code, _out = self.run_import(reply_text(), extra=["--date", "2026-01-01"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

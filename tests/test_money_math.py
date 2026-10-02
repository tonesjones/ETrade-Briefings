"""The numbers the model is told to act on: sell sizes, weights, sleeves, ST/LT."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import get_portfolio as portfolio
from portfolio_policy import POLICY

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 9, 18, 16, 0, tzinfo=ET)


class MinimumCutTests(unittest.TestCase):
    def test_cut_restores_exactly_the_cap(self):
        # 20% of a $100k book; 15% cap → sell $5k, not 15% of the position.
        cut = briefing.min_cut_to_cap(20_000.0, 100_000.0, 15.0)
        self.assertAlmostEqual(cut, 5_000.0)
        self.assertAlmostEqual((20_000.0 - cut) / 100_000.0 * 100, 15.0)

    def test_under_cap_needs_no_cut(self):
        self.assertEqual(briefing.min_cut_to_cap(10_000.0, 100_000.0, 15.0), 0.0)

    def test_empty_book_needs_no_cut(self):
        self.assertEqual(briefing.min_cut_to_cap(10_000.0, 0.0, 15.0), 0.0)

    def test_constraint_math_shows_the_cut_for_a_breach(self):
        holdings = [
            {"symbol": "NVDA", "market_value": 20_000.0, "weight": 20.0},
            {"symbol": "VOO", "market_value": 10_000.0, "weight": 10.0},
        ]
        text = briefing.format_constraint_math(holdings, 100_000.0, 20_000.0)
        self.assertIn("NVDA", text)
        self.assertIn("$5,000", text)
        self.assertNotIn("VOO", text)


class ConsolidateTests(unittest.TestCase):
    def _account(self, label, bucket, holdings, total):
        return {"label": label, "tax_bucket": bucket, "holdings": holdings, "total_value": total}

    def test_same_ticker_in_two_accounts_sums_dollars_weight_and_cost(self):
        results = [
            self._account("Brokerage (…1111)", "taxable", [{
                "symbol": "AMD", "market_value": 3_000.0, "price": 100.0, "quantity": 30.0,
                "total_cost": 1_500.0, "total_gain": 1_500.0, "price_paid": 50.0,
            }], 5_000.0),
            self._account("Roth (…2222)", "roth", [{
                "symbol": "AMD", "market_value": 1_000.0, "price": 100.0, "quantity": 10.0,
                "total_cost": 900.0, "total_gain": 100.0, "price_paid": 90.0,
            }], 5_000.0),
        ]
        holdings, accounts = briefing.consolidate(results, 10_000.0)
        amd = holdings[0]
        self.assertEqual(amd["market_value"], 4_000.0)
        self.assertAlmostEqual(amd["weight"], 40.0)
        self.assertEqual(amd["quantity"], 40.0)
        self.assertEqual(amd["total_cost"], 2_400.0)
        self.assertAlmostEqual(amd["cost_per_share"], 60.0)
        self.assertAlmostEqual(amd["price_paid"], 60.0)  # quantity-weighted
        self.assertAlmostEqual(amd["total_gain_pct"], 100.0 * 1_600.0 / 2_400.0)
        self.assertEqual(amd["taxable_mv"], 3_000.0)
        self.assertEqual(amd["roth_mv"], 1_000.0)
        self.assertEqual(amd["ira_mv"], 0.0)
        self.assertEqual(len(accounts), 2)

    def test_failed_account_is_excluded(self):
        results = [
            self._account("Ok (…1111)", "taxable", [{
                "symbol": "VOO", "market_value": 500.0, "price": 500.0, "quantity": 1.0,
            }], 500.0),
            {**self._account("Bad (…2222)", "taxable", [{
                "symbol": "VOO", "market_value": 999.0, "price": 500.0, "quantity": 2.0,
            }], 999.0), "error": "boom"},
        ]
        holdings, accounts = briefing.consolidate(results, 500.0)
        self.assertEqual(holdings[0]["market_value"], 500.0)
        self.assertEqual([a["label"] for a in accounts], ["Ok (…1111)"])


class SleeveTests(unittest.TestCase):
    def test_direct_sleeve_is_counted_inside_broad_sleeve(self):
        direct = POLICY.sleeves["direct_ai_semi"]
        broad = POLICY.sleeves["broad_ai_cycle"]
        self.assertTrue(set(direct) <= set(broad), "policy: direct must be a subset of broad")
        extra = next(s for s in broad if s not in direct)
        holdings = [
            {"symbol": direct[0], "market_value": 30.0},
            {"symbol": extra, "market_value": 20.0},
            {"symbol": "ZZZ-NOT-A-MEMBER", "market_value": 50.0},
        ]
        direct_mv, direct_w, _ = briefing.sleeve_stats(holdings, direct, 100.0)
        broad_mv, broad_w, _ = briefing.sleeve_stats(holdings, broad, 100.0)
        self.assertEqual((direct_mv, direct_w), (30.0, 30.0))
        self.assertEqual((broad_mv, broad_w), (50.0, 50.0))


class LotTermTests(unittest.TestCase):
    def test_extract_lots_reads_term_code_and_skips_closed_lots(self):
        lots = portfolio.extract_lots({"PositionLot": [
            {"remainingQty": 5, "price": 10, "totalCost": 50, "marketValue": 80,
             "acquiredDate": "1700000000000", "termCode": 1},
            {"remainingQty": 0, "price": 10, "totalCost": 50, "marketValue": 0, "termCode": 2},
        ]})
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["qty"], 5.0)
        self.assertEqual(lots[0]["term_code"], 1)

    def test_acquired_date_decides_term_and_broker_code_only_flags_conflict(self):
        # termCode semantics are undocumented, so the acquired date decides.
        recent = datetime(2026, 8, 19, tzinfo=ET)
        term = portfolio.term_from_lot
        self.assertEqual(term({"term_code": 1, "acquired": recent}, AS_OF), "ST")
        self.assertTrue(portfolio.lot_term_conflict({"term_code": 1, "acquired": recent}, AS_OF))
        self.assertFalse(portfolio.lot_term_conflict({"term_code": 2, "acquired": recent}, AS_OF))
        self.assertEqual(term({"term_code": 1, "acquired": None}, AS_OF), "LT")

    def test_mixed_lots_split_value_and_gain(self):
        holding = {"market_value": 300.0, "total_gain": 120.0, "lots": [
            {"qty": 1, "market_value": 100.0, "total_gain": 60.0, "term_code": 1,
             "acquired": datetime(2024, 1, 2, tzinfo=ET)},
            {"qty": 2, "market_value": 200.0, "total_gain": 60.0, "term_code": 2,
             "acquired": datetime(2026, 6, 1, tzinfo=ET)},
        ]}
        split = portfolio.lot_term_split(holding, AS_OF)
        self.assertEqual(split["term"], "mixed")
        self.assertEqual((split["lt_mv"], split["lt_gain"]), (100.0, 60.0))
        self.assertEqual((split["st_mv"], split["st_gain"]), (200.0, 60.0))
        self.assertEqual(split["earliest"].year, 2024)

    def test_lots_without_values_are_prorated_by_quantity(self):
        holding = {"market_value": 400.0, "total_gain": 100.0, "lots": [
            {"qty": 1, "market_value": 0.0, "term_code": 1,
             "acquired": datetime(2024, 1, 2, tzinfo=ET)},
            {"qty": 3, "market_value": 0.0, "term_code": 2,
             "acquired": datetime(2026, 6, 1, tzinfo=ET)},
        ]}
        split = portfolio.lot_term_split(holding, AS_OF)
        self.assertAlmostEqual(split["lt_mv"], 100.0)
        self.assertAlmostEqual(split["st_mv"], 300.0)
        self.assertAlmostEqual(split["st_gain"], 75.0)


class MustAnalyzeTests(unittest.TestCase):
    def _row(self, **kw):
        base = {"symbol": "ZZZ", "market_value": 1_000.0, "weight": 0.1,
                "total_gain": 0.0, "total_gain_pct": 0.0, "taxable_mv": 1_000.0}
        base.update(kw)
        return base

    def reasons(self, **kw):
        rows = briefing.must_analyze_holdings([self._row(**kw)])
        return rows[0]["analyze_reasons"] if rows else []

    def test_weight_floor_is_inclusive(self):
        floor = POLICY.analyze_weight_floor_pct
        self.assertIn("weight", self.reasons(weight=floor))
        self.assertEqual(self.reasons(weight=floor - 0.01), [])

    def test_small_positions_never_forced_in_for_losses(self):
        mv = POLICY.analyze_market_value_floor - 1
        self.assertEqual(self.reasons(market_value=mv, taxable_mv=mv, total_gain=-1e9), [])

    def test_sized_material_loss_is_forced_in_below_weight_floor(self):
        mv = POLICY.analyze_market_value_floor
        pct = POLICY.material_loss_pct
        self.assertIn("loss", self.reasons(market_value=mv, total_gain_pct=pct))
        dollars = POLICY.material_loss_dollars
        self.assertIn("loss", self.reasons(market_value=mv, total_gain=dollars))

    def test_harvest_requires_taxable_dollars(self):
        mv = POLICY.analyze_market_value_floor
        loss = POLICY.harvest_loss_dollars - 1
        self.assertIn("harvest", self.reasons(market_value=mv, total_gain=loss))
        self.assertNotIn("harvest", self.reasons(market_value=mv, total_gain=loss, taxable_mv=0.0))

    def test_cash_is_never_analyzed(self):
        self.assertEqual(self.reasons(symbol="SGOV", weight=90.0), [])


if __name__ == "__main__":
    unittest.main()

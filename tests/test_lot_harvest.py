"""Lot-aware taxable loss-harvest review flags."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 9, 18, 16, 0, tzinfo=ET)


def _lot(qty, mv, gain, acquired):
    return {
        "qty": qty,
        "market_value": mv,
        "total_cost": mv - gain,
        "total_gain": gain,
        "acquired": acquired,
    }


def _account(bucket, holdings, label="Brokerage (…1111)"):
    return {"label": label, "tax_bucket": bucket, "holdings": holdings, "total_value": 50_000.0}


def _amd(lots, total_gain, market_value=20_000.0):
    return {
        "symbol": "AMD",
        "quantity": 20.0,
        "market_value": market_value,
        "total_cost": market_value - total_gain,
        "total_gain": total_gain,
        "lots": lots,
    }


REPRO_LOTS = [
    _lot(10, 10_000.0, 8_000.0, datetime(2022, 1, 3, tzinfo=ET)),
    _lot(10, 10_000.0, -6_000.0, datetime(2026, 6, 1, tzinfo=ET)),
]


class LotHarvestTests(unittest.TestCase):
    def test_net_gain_position_with_losing_lot_is_flagged(self):
        rows = briefing.taxable_harvest_reviews(
            [_account("taxable", [_amd(REPRO_LOTS, 2_000.0)])], AS_OF
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["symbol"], row["account"]), ("AMD", "…1111"))
        self.assertEqual(
            row["loss_lots"],
            [{"acquired": "2026-06-01", "qty": 10, "total_gain": -6_000.0, "term": "ST"}],
        )

    def test_no_lot_below_threshold_and_net_gain_is_not_flagged(self):
        lots = [
            _lot(10, 10_000.0, 8_000.0, datetime(2022, 1, 3, tzinfo=ET)),
            _lot(10, 10_000.0, -100.0, datetime(2026, 6, 1, tzinfo=ET)),
        ]
        rows = briefing.taxable_harvest_reviews([_account("taxable", [_amd(lots, 7_900.0)])], AS_OF)
        self.assertEqual(rows, [])

    def test_position_level_net_loss_still_flagged(self):
        lots = [_lot(20, 20_000.0, -1_000.0, datetime(2024, 1, 2, tzinfo=ET))]
        acct = _account("taxable", [_amd(lots, -1_000.0)])
        rows = briefing.taxable_harvest_reviews([acct], AS_OF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_gain"], -1_000.0)
        self.assertEqual(rows[0]["loss_lots"][0]["term"], "LT")
        # Position with a net loss but no lot detail is still flagged with empty loss_lots.
        rows = briefing.taxable_harvest_reviews([_account("taxable", [_amd([], -1_000.0)])], AS_OF)
        self.assertEqual(rows[0]["loss_lots"], [])

    def test_ira_and_roth_never_flagged(self):
        for bucket in ("traditional", "roth"):
            rows = briefing.taxable_harvest_reviews(
                [_account(bucket, [_amd(REPRO_LOTS, 2_000.0)])], AS_OF
            )
            self.assertEqual(rows, [], bucket)

    def test_sub_floor_position_not_flagged(self):
        lots = [_lot(1, 1_000.0, -600.0, datetime(2026, 6, 1, tzinfo=ET))]
        holding = _amd(lots, -600.0, market_value=1_000.0)
        holding["total_cost"] = 1_600.0
        rows = briefing.taxable_harvest_reviews([_account("taxable", [holding])], AS_OF)
        self.assertEqual(rows, [])

    def test_loss_lots_sorted_worst_first(self):
        lots = REPRO_LOTS + [_lot(5, 5_000.0, -300.0, datetime(2023, 2, 1, tzinfo=ET))]
        rows = briefing.taxable_harvest_reviews([_account("taxable", [_amd(lots, 1_700.0)])], AS_OF)
        self.assertEqual([lot["total_gain"] for lot in rows[0]["loss_lots"]], [-6_000.0, -300.0])
        self.assertEqual(rows[0]["loss_lots"][1]["term"], "LT")

    def test_formatting_shows_lot_date_and_amount(self):
        rows = briefing.taxable_harvest_reviews(
            [_account("taxable", [_amd(REPRO_LOTS, 2_000.0)])], AS_OF
        )
        text = briefing.format_observable_reviews(rows, [])
        self.assertIn("AMD / …1111", text)
        self.assertIn("position P/L +$2,000", text)
        self.assertIn("losing lots: 2026-06-01 qty 10 -$6,000 ST.", text)


if __name__ == "__main__":
    unittest.main()

"""Option positions must stay distinct from their underlying stock."""

import copy
import json
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import build_briefing_prompt as briefing
import get_portfolio as portfolio

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 9, 18, 16, 0, tzinfo=ET)
FIXTURE = Path(__file__).parent / "fixtures" / "portable_portfolio.txt"

STOCK = {
    "Product": {"symbol": "NVDA", "securityType": "EQ"},
    "quantity": 100, "marketValue": 18000, "Quick": {"lastTrade": 180},
}
SHORT_CALL = {
    "Product": {
        "symbol": "NVDA", "securityType": "OPTN", "callPut": "CALL",
        "strikePrice": 200, "expiryYear": 2026, "expiryMonth": 12, "expiryDay": 18,
    },
    "quantity": -5, "marketValue": -4000, "Quick": {"lastTrade": 8},
}


def _response(*positions):
    return {"PortfolioResponse": {"AccountPortfolio": [{"Position": list(positions)}]}}


class ExtractOptionTests(unittest.TestCase):
    def test_option_is_a_distinct_holding_from_its_underlying(self):
        holdings, total = portfolio.extract_holdings(_response(STOCK, SHORT_CALL))
        by_symbol = {h["symbol"]: h for h in holdings}
        self.assertEqual(set(by_symbol), {"NVDA", "NVDA 2026-12-18 200C"})
        self.assertEqual(total, 14000)
        stock = by_symbol["NVDA"]
        self.assertEqual(stock["quantity"], 100)
        self.assertEqual(stock["security_type"], "EQ")
        self.assertEqual(stock["underlying"], "NVDA")
        option = by_symbol["NVDA 2026-12-18 200C"]
        self.assertEqual(option["quantity"], -5)
        self.assertEqual(option["market_value"], -4000)
        self.assertEqual(option["security_type"], "OPTN")
        self.assertEqual(option["underlying"], "NVDA")

    def test_put_and_fractional_strike_format(self):
        put = copy.deepcopy(SHORT_CALL)
        put["Product"].update(callPut="put", strikePrice=12.5, expiryMonth=1, expiryDay=5)
        holdings, _ = portfolio.extract_holdings(_response(put))
        self.assertEqual(holdings[0]["symbol"], "NVDA 2026-01-05 12.5P")

    def test_lowercase_keys_are_accepted(self):
        pos = {
            "product": {
                "symbol": "AMD", "securitytype": "optn", "callput": "CALL",
                "strikeprice": 150, "expiryyear": 2027, "expirymonth": 3, "expiryday": 19,
            },
            "quantity": 1, "marketvalue": 500,
        }
        holdings, _ = portfolio.extract_holdings(_response(pos))
        self.assertEqual(holdings[0]["symbol"], "AMD 2027-03-19 150C")
        self.assertEqual(holdings[0]["security_type"], "OPTN")

    def test_missing_security_type_defaults_to_equity(self):
        pos = {"Product": {"symbol": "VOO"}, "quantity": 1, "marketValue": 500}
        holdings, _ = portfolio.extract_holdings(_response(pos))
        self.assertEqual(holdings[0]["security_type"], "EQ")
        self.assertEqual(holdings[0]["underlying"], "VOO")

    def test_option_missing_contract_field_fails_closed(self):
        for missing in ("strikePrice", "callPut", "expiryYear", "expiryMonth", "expiryDay"):
            with self.subTest(missing=missing):
                option = copy.deepcopy(SHORT_CALL)
                del option["Product"][missing]
                with self.assertRaises(portfolio.PortfolioDataError) as ctx:
                    portfolio.extract_holdings(_response(STOCK, option))
                self.assertIn("NVDA", str(ctx.exception))

    def test_call_put_without_security_type_is_still_treated_as_option(self):
        option = copy.deepcopy(SHORT_CALL)
        del option["Product"]["securityType"]
        holdings, _ = portfolio.extract_holdings(_response(option))
        self.assertEqual(holdings[0]["symbol"], "NVDA 2026-12-18 200C")

    def test_cash_holding_is_tagged(self):
        cash = portfolio.cash_holding(100.0)
        self.assertEqual(cash["security_type"], "CASH")
        self.assertEqual(cash["underlying"], "CASH")


class ConsolidateOptionTests(unittest.TestCase):
    def test_consolidate_keeps_option_separate_from_stock(self):
        holdings, total = portfolio.extract_holdings(_response(STOCK, SHORT_CALL))
        results = [{
            "label": "Brokerage (…1111)", "tax_bucket": "taxable",
            "holdings": holdings, "total_value": total,
        }]
        consolidated, _accounts = briefing.consolidate(results, total)
        by_symbol = {h["symbol"]: h for h in consolidated}
        self.assertEqual(set(by_symbol), {"NVDA", "NVDA 2026-12-18 200C"})
        self.assertEqual(by_symbol["NVDA"]["market_value"], 18000)
        self.assertEqual(by_symbol["NVDA"]["quantity"], 100)
        self.assertEqual(by_symbol["NVDA 2026-12-18 200C"]["market_value"], -4000)
        self.assertEqual(by_symbol["NVDA 2026-12-18 200C"]["quantity"], -5)


class PortableOptionTests(unittest.TestCase):
    def test_round_trip_preserves_security_type_and_underlying(self):
        holdings, total = portfolio.extract_holdings(_response(STOCK, SHORT_CALL))
        results = [{
            "label": "Brokerage (…1111)", "account_id_key": "key-1", "tax_bucket": "taxable",
            "account_type": "INDIVIDUAL", "holdings": holdings, "total_value": total,
        }]
        text = portfolio.format_portable_portfolio_text("Header", results, AS_OF)
        _text, _total, _n, parsed, _as_of = portfolio.parse_portable_portfolio_text(text)
        by_symbol = {h["symbol"]: h for h in parsed[0]["holdings"]}
        option = by_symbol["NVDA 2026-12-18 200C"]
        self.assertEqual(option["security_type"], "OPTN")
        self.assertEqual(option["underlying"], "NVDA")
        self.assertEqual(option["quantity"], -5)
        self.assertEqual(by_symbol["NVDA"]["security_type"], "EQ")

    def test_old_payload_without_new_keys_still_parses(self):
        payload = json.loads(
            FIXTURE.read_text(encoding="utf-8")
            .split(portfolio.PORTABLE_BEGIN, 1)[1]
            .split(portfolio.PORTABLE_END, 1)[0]
        )
        for account in payload["accounts"]:
            for holding in account["holdings"]:
                self.assertNotIn("security_type", holding)
        _text, _total, _n, results, _as_of = portfolio.parse_portable_portfolio_text(
            FIXTURE.read_text(encoding="utf-8")
        )
        for result in results:
            for holding in result["holdings"]:
                self.assertEqual(holding["security_type"], "EQ")
                self.assertEqual(holding["underlying"], holding["symbol"])


if __name__ == "__main__":
    unittest.main()

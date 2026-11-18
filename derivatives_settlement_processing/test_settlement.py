import sqlite3
import unittest
from datetime import date

import settlement as s
from settlement import Trade


def trade(instrument="FUTURE", side="BUY", qty=1, strike=100.0, premium=0.0, mult=1.0,
          expiry=date(2026, 11, 18), tid="T1", acct="A1", und="X"):
    return Trade(tid, acct, date(2026, 11, 1), instrument, und, side, qty, strike, expiry, premium, mult)


PRICES = {"X": ([date(2026, 11, 17), date(2026, 11, 18)], [105.0, 110.0])}


class ValuationTests(unittest.TestCase):
    def test_future_long_and_short(self):
        self.assertEqual(s.trade_value(trade(side="BUY", qty=2, mult=10), 110), 200)
        self.assertEqual(s.trade_value(trade(side="SELL", qty=2, mult=10), 110), -200)

    def test_call_payoff(self):
        self.assertEqual(s.trade_value(trade("CALL", "BUY", strike=100), 110), 10)
        self.assertEqual(s.trade_value(trade("CALL", "BUY", strike=100), 90), 0)
        self.assertEqual(s.trade_value(trade("CALL", "SELL", strike=100), 110), -10)

    def test_put_payoff(self):
        self.assertEqual(s.trade_value(trade("PUT", "BUY", strike=100), 90), 10)
        self.assertEqual(s.trade_value(trade("PUT", "BUY", strike=100), 110), 0)

    def test_premium_direction(self):
        self.assertEqual(s.premium_cash(trade("CALL", "BUY", qty=2, premium=5, mult=10)), -100)
        self.assertEqual(s.premium_cash(trade("CALL", "SELL", qty=2, premium=5, mult=10)), 100)

    def test_margin(self):
        self.assertAlmostEqual(s.initial_margin(trade("FUTURE", "SELL", qty=2, mult=10), 100), 200)
        self.assertEqual(s.initial_margin(trade("CALL", "BUY"), 100), 0)
        self.assertGreater(s.initial_margin(trade("CALL", "SELL"), 100), 0)

    def test_price_lookup_uses_latest_on_or_before(self):
        self.assertEqual(s.price_on(PRICES, "X", date(2026, 11, 18)), 110.0)
        self.assertEqual(s.price_on(PRICES, "X", date(2026, 11, 30)), 110.0)
        with self.assertRaises(KeyError):
            s.price_on(PRICES, "X", date(2026, 11, 1))


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def test_settles_expired_and_marks_open(self):
        trades = [trade(tid="EXP", expiry=date(2026, 11, 18)),
                  trade(tid="OPEN", expiry=date(2026, 12, 30))]
        res = {r.trade_id: r for r in s.process(self.conn, trades, PRICES, date(2026, 11, 18))}
        self.assertEqual(res["EXP"].status, "SETTLED")
        self.assertEqual(res["EXP"].amount, 10)
        self.assertEqual(res["OPEN"].status, "OPEN")

    def test_processing_is_idempotent(self):
        trades = [trade("CALL", "BUY", strike=100, premium=3)]
        first = s.process(self.conn, trades, PRICES, date(2026, 11, 18))
        second = s.process(self.conn, trades, PRICES, date(2026, 11, 18))
        self.assertTrue(first[0].newly_posted)
        self.assertFalse(second[0].newly_posted)
        count = self.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        self.assertEqual(count, 2)  # one PREMIUM + one SETTLEMENT, never duplicated
        cash = s.account_cash(self.conn, date(2026, 11, 18))["A1"]
        self.assertEqual(cash, 7)   # +10 payoff - 3 premium

    def test_future_trades_ignored(self):
        t = trade()
        t.trade_date = date(2026, 12, 1)
        self.assertEqual(s.process(self.conn, [t], PRICES, date(2026, 11, 18)), [])

    def test_margin_call(self):
        trades = [trade("FUTURE", "SELL", qty=10, strike=100, mult=10, expiry=date(2026, 12, 30))]
        s.process(self.conn, trades, PRICES, date(2026, 11, 18))
        m = s.margin_summary(self.conn, trades, PRICES, {"A1": 500.0}, date(2026, 11, 18))[0]
        # MTM = -(110-100)*10*10 = -1000 ; equity = 500-1000 = -500 ; required = 0.1*110*100 = 1100
        self.assertEqual(m["status"], "MARGIN CALL")
        self.assertAlmostEqual(m["margin_call"], 1600)


if __name__ == "__main__":
    unittest.main()

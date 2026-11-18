"""
Automated Derivatives and Settlement Processing
-----------------------------------------------
A simple end-of-day engine for a derivatives book. For a given processing date it:

  1. Books option premiums (cash paid by the buyer / received by the seller)
  2. Marks open positions to market
  3. Cash-settles every contract that has reached expiry
  4. Computes margin requirements and raises margin calls per account
  5. Writes everything to a SQLite ledger and a CSV report

Supported instruments: FUTURE, FORWARD, CALL, PUT (European, cash-settled).

Usage:
    python settlement.py --date 2026-11-18
    python settlement.py --trades trades.csv --prices prices.csv \
        --accounts accounts.csv --date 2026-11-18 --db ledger.db --report report.csv

Running the same date twice is safe: ledger entries are unique per
(trade, entry type), so nothing is ever settled or booked twice.
"""

import argparse
import bisect
import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

INITIAL_MARGIN_RATE = 0.10          # 10% of notional
OPTIONS = {"CALL", "PUT"}
LINEAR = {"FUTURE", "FORWARD"}


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
@dataclass
class Trade:
    trade_id: str
    account_id: str
    trade_date: date
    instrument: str      # FUTURE | FORWARD | CALL | PUT
    underlying: str
    side: str            # BUY | SELL
    quantity: float
    strike: float        # contract price (futures/forwards) or strike (options)
    expiry: date
    premium: float       # per unit, options only
    multiplier: float    # contract size

    @property
    def sign(self):
        return 1 if self.side == "BUY" else -1


@dataclass
class Result:
    trade_id: str
    account_id: str
    instrument: str
    status: str          # OPEN | SETTLED
    spot: float
    amount: float        # unrealised MTM (OPEN) or settlement cash (SETTLED)
    newly_posted: bool


# --------------------------------------------------------------------------
# Loading input
# --------------------------------------------------------------------------
def load_trades(path):
    trades = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = Trade(
                trade_id=r["trade_id"].strip(),
                account_id=r["account_id"].strip(),
                trade_date=date.fromisoformat(r["trade_date"].strip()),
                instrument=r["instrument"].strip().upper(),
                underlying=r["underlying"].strip().upper(),
                side=r["side"].strip().upper(),
                quantity=float(r["quantity"]),
                strike=float(r["strike"]),
                expiry=date.fromisoformat(r["expiry"].strip()),
                premium=float(r["premium"] or 0),
                multiplier=float(r["multiplier"] or 1),
            )
            if t.instrument not in OPTIONS | LINEAR:
                raise ValueError(f"{t.trade_id}: unknown instrument {t.instrument}")
            if t.side not in ("BUY", "SELL"):
                raise ValueError(f"{t.trade_id}: side must be BUY or SELL")
            trades.append(t)
    return trades


def load_prices(path):
    """Returns {underlying: ([sorted dates], [prices])} for fast lookups."""
    raw = defaultdict(dict)
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            raw[r["underlying"].strip().upper()][date.fromisoformat(r["date"].strip())] = float(r["price"])
    book = {}
    for und, series in raw.items():
        days = sorted(series)
        book[und] = (days, [series[d] for d in days])
    return book


def load_accounts(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["account_id"].strip(): float(r["collateral"]) for r in csv.DictReader(f)}


def price_on(prices, underlying, on_date):
    """Latest available price on or before the given date."""
    if underlying not in prices:
        raise KeyError(f"No prices for {underlying}")
    days, values = prices[underlying]
    i = bisect.bisect_right(days, on_date)
    if i == 0:
        raise KeyError(f"No price for {underlying} on or before {on_date}")
    return values[i - 1]


# --------------------------------------------------------------------------
# Valuation and margin
# --------------------------------------------------------------------------
def trade_value(t, spot):
    """Payoff / mark-to-market value of the position at the given spot.
    Options are valued at intrinsic value (no time value) for simplicity."""
    if t.instrument in LINEAR:
        payoff = spot - t.strike
    elif t.instrument == "CALL":
        payoff = max(spot - t.strike, 0.0)
    else:  # PUT
        payoff = max(t.strike - spot, 0.0)
    return t.sign * payoff * t.quantity * t.multiplier


def premium_cash(t):
    """Cash flow of the option premium: buyer pays (negative), seller receives."""
    return -t.sign * t.premium * t.quantity * t.multiplier


def initial_margin(t, spot, rate=INITIAL_MARGIN_RATE):
    """Long options need no margin (premium already paid); everything else does."""
    if t.instrument in OPTIONS and t.side == "BUY":
        return 0.0
    return rate * abs(spot * t.quantity * t.multiplier)


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------
def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,          -- PREMIUM | SETTLEMENT
            amount REAL NOT NULL,
            value_date TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (trade_id, entry_type)
        )""")
    conn.commit()


def post(conn, trade, entry_type, amount, value_date):
    """Insert a ledger entry; returns True only if it was newly posted."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO ledger (trade_id, account_id, entry_type, amount, value_date) "
        "VALUES (?,?,?,?,?)",
        (trade.trade_id, trade.account_id, entry_type, round(amount, 2), value_date.isoformat()),
    )
    return cur.rowcount == 1


def account_cash(conn, as_of):
    rows = conn.execute(
        "SELECT account_id, SUM(amount) FROM ledger WHERE value_date <= ? GROUP BY account_id",
        (as_of.isoformat(),),
    ).fetchall()
    return {acct: total for acct, total in rows}


# --------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------
def process(conn, trades, prices, as_of):
    """Run end-of-day processing for as_of. Returns a list of Result."""
    init_db(conn)
    results = []
    for t in sorted(trades, key=lambda x: x.trade_id):
        if t.trade_date > as_of:
            continue  # not yet traded

        if t.instrument in OPTIONS:
            post(conn, t, "PREMIUM", premium_cash(t), t.trade_date)

        if t.expiry <= as_of:
            spot = price_on(prices, t.underlying, t.expiry)
            amount = trade_value(t, spot)
            new = post(conn, t, "SETTLEMENT", amount, t.expiry)
            results.append(Result(t.trade_id, t.account_id, t.instrument, "SETTLED", spot, amount, new))
        else:
            spot = price_on(prices, t.underlying, as_of)
            results.append(Result(t.trade_id, t.account_id, t.instrument, "OPEN", spot,
                                  trade_value(t, spot), False))
    conn.commit()
    return results


def margin_summary(conn, trades, prices, collateral, as_of):
    """Per-account equity vs. required margin on open positions."""
    cash = account_cash(conn, as_of)
    required, open_mtm = defaultdict(float), defaultdict(float)
    for t in trades:
        if t.trade_date <= as_of < t.expiry:
            spot = price_on(prices, t.underlying, as_of)
            required[t.account_id] += initial_margin(t, spot)
            open_mtm[t.account_id] += trade_value(t, spot)

    summary = []
    for acct in sorted(set(collateral) | set(cash) | set(required)):
        equity = collateral.get(acct, 0.0) + cash.get(acct, 0.0) + open_mtm[acct]
        shortfall = max(required[acct] - equity, 0.0)
        summary.append({
            "account_id": acct,
            "collateral": collateral.get(acct, 0.0),
            "ledger_cash": cash.get(acct, 0.0),
            "open_mtm": open_mtm[acct],
            "equity": equity,
            "margin_required": required[acct],
            "margin_call": shortfall,
            "status": "MARGIN CALL" if shortfall > 0 else "OK",
        })
    return summary


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def write_report(path, results):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trade_id", "account_id", "instrument", "status", "spot", "amount", "newly_posted"])
        for r in results:
            w.writerow([r.trade_id, r.account_id, r.instrument, r.status,
                        f"{r.spot:.4f}", f"{r.amount:.2f}", r.newly_posted])


def print_summary(as_of, results, margins):
    settled = [r for r in results if r.status == "SETTLED"]
    open_ = [r for r in results if r.status == "OPEN"]
    print(f"Processing date : {as_of}")
    print(f"Settled today   : {len(settled)} trades, net cash {sum(r.amount for r in settled):,.2f}")
    print(f"Open positions  : {len(open_)} trades, net MTM  {sum(r.amount for r in open_):,.2f}")
    print("\nAccount margin status:")
    print(f"  {'Account':<8}{'Equity':>14}{'Required':>14}{'Call':>12}  Status")
    for m in margins:
        print(f"  {m['account_id']:<8}{m['equity']:>14,.2f}{m['margin_required']:>14,.2f}"
              f"{m['margin_call']:>12,.2f}  {m['status']}")


def main():
    p = argparse.ArgumentParser(description="Derivatives settlement processing")
    p.add_argument("--trades", default="trades.csv")
    p.add_argument("--prices", default="prices.csv")
    p.add_argument("--accounts", default="accounts.csv")
    p.add_argument("--date", default=date.today().isoformat(), help="processing date, YYYY-MM-DD")
    p.add_argument("--db", default="ledger.db")
    p.add_argument("--report", default="settlement_report.csv")
    args = p.parse_args()

    as_of = date.fromisoformat(args.date)
    trades = load_trades(args.trades)
    prices = load_prices(args.prices)
    collateral = load_accounts(args.accounts)

    conn = sqlite3.connect(args.db)
    results = process(conn, trades, prices, as_of)
    margins = margin_summary(conn, trades, prices, collateral, as_of)
    conn.close()

    write_report(args.report, results)
    print_summary(as_of, results, margins)
    print(f"\nLedger saved to {args.db}; report written to {args.report}")


if __name__ == "__main__":
    main()

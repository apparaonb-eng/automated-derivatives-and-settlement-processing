# Automated Derivatives and Settlement Processing

A simple end-of-day engine for a derivatives book. Pure Python 3.8+, no external
dependencies.

## What it does
For a processing date it will:
1. Book option premiums (buyer pays, seller receives)
2. Mark open positions to market using the latest available price
3. Cash-settle every contract that has reached expiry
4. Compute initial margin per account and raise margin calls
5. Save everything to a SQLite ledger and a CSV report

Supported instruments: `FUTURE`, `FORWARD`, `CALL`, `PUT` (European, cash-settled).

## Files
| File | Purpose |
|---|---|
| `settlement.py` | Valuation, settlement, margin, ledger and CLI |
| `trades.csv` | Sample trade book |
| `prices.csv` | Sample end-of-day prices |
| `accounts.csv` | Sample collateral per account |
| `test_settlement.py` | Unit tests |

## Run
```bash
python settlement.py --date 2026-11-18
python -m unittest test_settlement.py -v
```
Outputs `ledger.db` (table `ledger`) and `settlement_report.csv`.
Running the same date again is safe: the ledger has a unique key on
(trade, entry type), so nothing is booked or settled twice.

## Formulas
| Instrument | Value at spot S (strike/contract price K) |
|---|---|
| Future / Forward | `sign * (S - K) * qty * multiplier` |
| Call | `sign * max(S - K, 0) * qty * multiplier` |
| Put | `sign * max(K - S, 0) * qty * multiplier` |

`sign` is +1 for BUY and -1 for SELL. Options are valued at intrinsic value
(no time value). Initial margin is 10% of notional for futures, forwards and
short options; long options need none.
Account equity = collateral + ledger cash + open MTM; a margin call is raised
when equity is below required margin.

## Input formats
- `trades.csv`: `trade_id,account_id,trade_date,instrument,underlying,side,quantity,strike,expiry,premium,multiplier`
- `prices.csv`: `date,underlying,price`
- `accounts.csv`: `account_id,collateral`

## Ideas to extend
- Black-Scholes or binomial option pricing
- Physical delivery and early exercise (American options)
- Daily variation margin cash flows instead of equity-based margin
- Multi-currency settlement and FX conversion
- A small web dashboard for operations staff

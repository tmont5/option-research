# Manual Contract Verification

Date: 2026-06-12

Purpose: verify that the option contract selected by the scanner/provider mapping is the same contract intended for trading before building more ingestion and backtest automation.

## Contract Checked

- Underlying: ANET
- Expiration: 2026-07-17
- Strike: 150.00
- Right: put

Canonical OCC-style contract symbol observed from market data:

- Schwab: `ANET  260717P00150000`
- Yahoo/yfinance: `ANET260717P00150000`

This decodes as:

- `ANET`: underlying
- `260717`: 2026-07-17 expiration
- `P`: put
- `00150000`: 150.000 strike

## Source Comparison

| Field | Schwab | Yahoo/yfinance |
| --- | ---: | ---: |
| Expiration | 2026-07-17 | 2026-07-17 |
| Strike | 150.00 | 150.00 |
| Put/call flag | PUT | P in contract symbol |
| Bid | 5.00 | 5.00 |
| Ask | 5.40 | 5.40 |
| Mid | 5.20 | 5.20 |
| Last | n/a | 5.31 |
| IV | 0.54758 | 0.537114 |
| Open interest | 1516 | 1516 |
| Volume | 70 | 70 |
| Multiplier | 100 | implied standard contract |
| Deliverable | 100 ANET | implied standard contract |

Schwab did not return a delta field for this contract row. The scanner's local Black-Scholes estimate using Schwab spot, strike, DTE, and IV produced absolute delta `0.2720`.

## ThetaData Mapping Check

The installed ThetaData Python client methods build a `ContractSpec` from:

- `symbol`
- `expiration`
- `strike`
- `right`

ThetaData docs/search result for options indicates:

- `C` for call
- `P` for put

The provider's internal mapping of `OptionType.PUT` to `P` and `OptionType.CALL` to `C` is therefore directionally aligned with ThetaData's contract right parameter.

## Remaining Live ThetaData Check

Live ThetaData verification could not be completed on this machine because `ThetaClient(dataframe_type="pandas")` attempted to read `creds.txt`, and no ThetaData credential file/env var was configured in the project environment.

Before relying on a full historical ingestion run, perform one live ThetaData check for this same contract:

1. `option_history_eod(start_date, end_date, symbol="ANET", expiration=2026-07-17, strike="150", right="P")`
2. `option_history_greeks_first_order(symbol="ANET", expiration=2026-07-17, strike="150", right="P", start_date=..., end_date=...)`
3. `option_history_greeks_implied_volatility(symbol="ANET", expiration=2026-07-17, strike="150", right="P", start_date=..., end_date=...)`
4. Confirm returned rows carry the expected expiration, strike, and right, and that bid/ask/IV are in the same neighborhood as the independent source for the same date.

The app-facing provider also maps `/v2/hist/option/implied_volatility` through `retrieve_implied_volatility()`, and `retrieve_first_order_greeks()` accepts `implied_volatility` or `iv` when ThetaData includes it in first-order Greek rows. Use the dedicated implied-volatility endpoint as the canonical IV check.

## Conclusion

The contract identity is verified across Schwab and Yahoo/yfinance. The main unresolved item is a live ThetaData credentialed call to confirm the same contract identity, implied volatility, and first-order Greeks returned by ThetaData.

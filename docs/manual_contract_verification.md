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

| Field | Schwab | Yahoo/yfinance | ThetaData |
| --- | ---: | ---: | ---: |
| Expiration | 2026-07-17 | 2026-07-17 | 2026-07-17 |
| Strike | 150.00 | 150.00 | 150.00 |
| Put/call flag | PUT | P in contract symbol | PUT |
| Bid | 5.00 | 5.00 | 5.00 |
| Ask | 5.40 | 5.40 | 5.40 |
| Mid | 5.20 | 5.20 | 5.20 |
| Last/close | n/a | 5.31 | 5.31 |
| IV | 0.54758 | 0.537114 | 0.5548 |
| Delta | n/a | n/a | -0.2749 |
| Open interest | 1516 | 1516 | not included in checked EOD row |
| Volume | 70 | 70 | 70 |
| Multiplier | 100 | implied standard contract | implied standard contract |
| Deliverable | 100 ANET | implied standard contract | implied standard contract |

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

## Live ThetaData Check

After configuring `THETADATA_CREDENTIALS_FILE`, live ThetaData verification completed for the same contract on 2026-06-12.

Calls checked:

1. `option_history_eod(start_date, end_date, symbol="ANET", expiration=2026-07-17, strike="150", right="P")`
2. `option_history_greeks_first_order(symbol="ANET", expiration=2026-07-17, strike="150", right="P", start_date=..., end_date=...)`
3. `option_history_greeks_implied_volatility(symbol="ANET", expiration=2026-07-17, strike="150", right="P", start_date=..., end_date=...)`

ThetaData EOD returned exactly one row with:

- `symbol`: ANET
- `expiration`: 2026-07-17
- `strike`: 150.0
- `right`: PUT
- `bid`: 5.00
- `ask`: 5.40
- `close`: 5.31
- `volume`: 70

ThetaData first-order Greeks at 16:00 ET returned:

- `delta`: -0.2749
- `theta`: -0.1287
- `vega`: 16.8641
- `rho`: -4.8021
- `implied_vol`: 0.5548
- `underlying_price`: 163.23

ThetaData implied-volatility history at 16:00 ET returned:

- `bid_implied_vol`: 0.5429
- `implied_vol`: 0.5548
- `ask_implied_vol`: 0.5666
- `iv_error`: 0.0

The live Python client uses the column name `implied_vol`, so provider parsing must accept `implied_vol` in addition to `implied_volatility` and `iv`.

## Conclusion

The contract identity is verified across Schwab, Yahoo/yfinance, and ThetaData. ThetaData returns the expected ANET 2026-07-17 150 put contract, matching bid/ask and close/last price, and provides delta and implied volatility for the same contract.

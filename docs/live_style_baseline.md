# Live-Style Scanner Wheel Baseline

## Purpose

This document defines the current baseline for scanner-style wheel research.
Use it as the reference case before comparing looser filters, broader data
windows, Tier C, or new strategy families.

## Canonical Data Set

- Data set: `runs/scanner_backfill_tier_ab_2025h1/market_data.duckdb`
- Window: `2025-01-01` through `2025-06-30`
- Universe: Tier A + Tier B from `ScannerStylePutStrategyConfig`
- Symbol count: 30
- Backfill status: Tier A+B 2025 H1 compact data with completed Tier B retry
  fixes and no known failed tasks in the final report.

Tier A symbols:

`AAPL, MSFT, GOOGL, AMZN, META, NVDA, AVGO, JPM, V, MA, COST, LLY, UNH, XOM, BRK-B`

Tier B symbols:

`AMD, QCOM, CRM, ORCL, NFLX, TSM, WMT, HD, MCD, CAT, GE, RTX, GS, MS, CVX`

## Baseline Run

The current reference run is:

- Output directory: `runs/live_style_wheel_tier_ab_combined_2025h1/`
- Report: `runs/live_style_wheel_tier_ab_combined_2025h1/report.md`
- Summary JSON: `runs/live_style_wheel_tier_ab_combined_2025h1/summary.json`
- Strategy mode: live-style scanner wheel
- Put DTE: 30-35
- Put max delta: 0.25
- Put monthly yield floor: 2.5%
- Covered-call monthly yield floor: 2.0%
- Initial cash: 500,000
- Target capital utilization: 75%
- Max capital utilization: 100%
- Max total positions: 10
- Max contracts per ticker: 3
- Candidate scan cap: top 50 candidates per day
- Challenged put policy: assign

Reproduce with:

```bash
uv run python scripts/run_live_style_wheel_from_db.py \
  --database-path runs/scanner_backfill_tier_ab_2025h1/market_data.duckdb \
  --start-date 2025-01-01 \
  --end-date 2025-06-30 \
  --tier A \
  --tier B \
  --report-path runs/live_style_wheel_tier_ab_combined_2025h1/report.md \
  --summary-path runs/live_style_wheel_tier_ab_combined_2025h1/summary.json
```

## Baseline Results

- Final equity: 520,461.30
- Total PnL: 20,461.30
- Return: 4.09%
- Realized option PnL: 21,614.35
- Average capital utilization: 14.06%
- Max capital utilization: 74.41%
- Max drawdown: 4.07%
- Closed put positions/contracts: 14 / 36
- Closed call positions/contracts: 2 / 6
- Assignments: 1 assignment / 3 contracts
- Rolls: 0
- Open shares at end: 0
- Skipped count: 41

Best PnL symbols in the baseline:

- NFLX: 6,494.10
- NVDA: 5,967.45
- AVGO: 3,711.00
- UNH: 1,781.10
- META: 1,582.70
- AMD: 1,064.90
- TSM: 1,013.10

## Baseline Diagnostics

The current diagnostic output is:

- Output directory: `runs/live_style_utilization_diagnostics_tier_ab_2025h1/`
- Report: `runs/live_style_utilization_diagnostics_tier_ab_2025h1/report.md`
- Summary JSON: `runs/live_style_utilization_diagnostics_tier_ab_2025h1/summary.json`

Reproduce with:

```bash
uv run python scripts/run_live_style_diagnostics_from_db.py \
  --database-path runs/scanner_backfill_tier_ab_2025h1/market_data.duckdb \
  --start-date 2025-01-01 \
  --end-date 2025-06-30 \
  --tier A \
  --tier B \
  --report-path runs/live_style_utilization_diagnostics_tier_ab_2025h1/report.md \
  --summary-path runs/live_style_utilization_diagnostics_tier_ab_2025h1/summary.json
```

Diagnostic summary:

- Baseline candidate availability is low: 28 eligible days out of 121 observed
  days.
- Baseline zero-candidate days: 93.
- Baseline average candidates/day: 0.45.
- Raising max positions alone did not change the result.
- Raising the candidate cap from 50 to 100 did not change the result.
- Looser filters increase utilization but also increase drawdown.

Current scenario comparison:

| Scenario | Return | Avg Util | Max Util | Max DD | Put Contracts | Eligible Days |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 4.09% | 14.06% | 74.41% | 4.07% | 36 | 28/121 |
| wider_dte_25_40 | 6.87% | 25.46% | 74.74% | 6.93% | 53 | 45/121 |
| lower_yield_2pct | 8.81% | 52.55% | 75.91% | 13.91% | 37 | 58/121 |
| higher_delta_028 | 8.40% | 31.03% | 74.79% | 9.40% | 37 | 40/121 |
| more_positions_12 | 4.09% | 14.06% | 74.41% | 4.07% | 36 | 28/121 |
| larger_ticker_size_5 | 5.29% | 18.11% | 75.16% | 5.02% | 51 | 28/121 |
| deployment_combo | 6.13% | 69.82% | 77.26% | 17.13% | 101 | 93/121 |

## Phase 1 Conclusion

The first baseline conclusion is that utilization is primarily constrained by
candidate availability under the current filters, not by the total-position cap
or candidate scan cap. The next research work should compare controlled filter
changes and report their risk tradeoffs before expanding to larger data pulls
or unrelated strategy families.

## Phase 2 Parameter Sweep

The Phase 2 sweep runner formalizes controlled comparisons against the same
canonical data set:

```bash
uv run python scripts/run_live_style_parameter_sweep_from_db.py \
  --database-path runs/scanner_backfill_tier_ab_2025h1/market_data.duckdb \
  --start-date 2025-01-01 \
  --end-date 2025-06-30 \
  --report-path runs/live_style_parameter_sweep_tier_ab_2025h1/report.md \
  --summary-path runs/live_style_parameter_sweep_tier_ab_2025h1/summary.json
```

The sweep ranks scenarios by return-to-drawdown, adjusted for deployment and
penalized for drawdown above 8%, assignment rate above 10%, and heavy
single-symbol dependence. The ranking intentionally favors useful risk tradeoffs
over the highest raw return.

Current Phase 2 comparison:

| Rank | Scenario | Return | Avg Util | Max DD | Put/Call Contracts | Assignments | Eligible Days |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | tier_a_only | 4.89% | 11.35% | 1.91% | 38 / 6 | 1 | 21/121 |
| 2 | wider_dte_25_40 | 6.87% | 25.46% | 6.93% | 53 / 12 | 3 | 45/121 |
| 3 | larger_ticker_size_5 | 5.29% | 18.11% | 5.02% | 51 / 10 | 1 | 28/121 |
| 4 | baseline | 4.09% | 14.06% | 4.07% | 36 / 6 | 1 | 28/121 |
| 5 | higher_delta_028 | 8.40% | 31.03% | 9.40% | 37 / 12 | 3 | 40/121 |
| 6 | higher_delta_030 | 10.97% | 50.59% | 12.74% | 51 / 12 | 5 | 47/121 |
| 7 | tier_b_only | 2.17% | 7.64% | 3.98% | 24 / 0 | 0 | 12/121 |
| 8 | lower_yield_2pct | 8.81% | 52.55% | 13.91% | 37 / 18 | 6 | 58/121 |
| 9 | lower_yield_1_5pct | 9.57% | 70.43% | 17.56% | 42 / 8 | 9 | 89/121 |
| 10 | deployment_combo | 6.13% | 69.82% | 17.13% | 101 / 21 | 5 | 93/121 |

Phase 2 working conclusions:

- Tier A only is the best current risk-adjusted scenario: higher return than
  baseline with less than half the max drawdown.
- Wider DTE is the most promising A+B relaxation: it improves return and
  utilization while keeping max drawdown below 7%.
- Lower yield floors and higher delta caps increase activity and raw return,
  but drawdown and assignments rise quickly.
- Tier B only is weak in this window: low activity, low utilization, and lower
  return.

## Phase 3 Explainability

The Phase 3 explainability runner focuses on why the top scenarios behaved
differently:

```bash
uv run python scripts/run_live_style_explainability_from_db.py \
  --database-path runs/scanner_backfill_tier_ab_2025h1/market_data.duckdb \
  --start-date 2025-01-01 \
  --end-date 2025-06-30 \
  --report-path runs/live_style_explainability_tier_ab_2025h1/report.md \
  --summary-path runs/live_style_explainability_tier_ab_2025h1/summary.json
```

It compares the baseline, Tier A only, wider DTE 25-40, and higher delta 0.28.
The report breaks results down by filter funnel, tier contribution, symbol
contribution, assignment count, and max-drawdown-window exposure.

Current Phase 3 findings:

- Tier A only beat the A+B baseline because quality improved more than breadth:
  return rose from 4.09% to 4.89%, while max drawdown fell from 4.07% to 1.91%.
- Tier B did not just underperform in isolation. In the A+B baseline, Tier B
  consumed some slots/exposure and reduced Tier A throughput: baseline Tier A
  contribution was 23 put contracts and about 13,042 PnL, while Tier A only
  produced 38 put contracts and about 25,614 PnL.
- The baseline filter funnel was tight: 85,417 raw joined put rows became 7,076
  DTE-pass rows, 2,742 liquidity-pass rows, 1,696 delta-pass rows, 72 yield-pass
  rows, and 55 final one-per-symbol candidates.
- Wider DTE is the cleanest A+B relaxation because it improves candidate supply
  before loosening riskier yield or delta constraints: eligible days rose from
  28/121 to 45/121, return rose to 6.87%, and max drawdown stayed below 7%.
- Higher delta 0.28 increased raw return to 8.40%, but drawdown rose to 9.40%
  and assignments increased from 1 to 3, making it a riskier lever.
- Yield is the biggest filter bottleneck after DTE/liquidity/delta. In the
  baseline, 1,696 rows passed delta but only 72 passed the 2.5% monthly yield
  floor.

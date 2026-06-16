# Scanner-Style Put Strategy

## Goal

Find cash-secured put opportunities with acceptable premium on high-quality,
liquid stocks the trader is willing to own. Prefer names coming out of a
controlled drawdown, where the put strike is at or below meaningful structural
support and assignment would be acceptable.

This document is the v1 research contract. It defines the knobs and defaults
that future backfills and simulations should support. It does not claim the
defaults are optimal.

## Universe

The v1 universe is intentionally curated to 40 names. It should behave more
like a focused ownership list than a broad market scanner.

### Tier A

Core ownership names. Lower yield may be acceptable when structure is excellent.

- AAPL
- MSFT
- GOOGL
- AMZN
- META
- NVDA
- AVGO
- JPM
- V
- MA
- COST
- LLY
- UNH
- XOM
- BRK-B

### Tier B

Quality names that should meet normal return/risk standards.

- AMD
- QCOM
- CRM
- ORCL
- NFLX
- TSM
- WMT
- HD
- MCD
- CAT
- GE
- RTX
- GS
- MS
- CVX

### Tier C

High-volatility opportunistic names. Keep them in the universe, but require
better premium, stronger cushion/support, stricter liquidity, and smaller
position sizing.

- TSLA
- PLTR
- COIN
- MSTR
- SOFI
- HOOD
- SHOP
- UBER
- MU
- INTC

## Put Entry Rules

- Strategy type: cash-secured puts.
- DTE window: 20-35 DTE.
- Normal max delta: 0.28 absolute delta.
- Base monthly put yield: 2.5%.
- Avoid earnings before expiration.
- Reject nonstandard deliverables.
- Require tradable liquidity and reasonable fill quality.
- Prefer controlled pullbacks rather than breakdowns.
- Require strike at or below structural support.
- Reject recent support failures and deteriorating trend setups.

## Tier Return Rules

- Tier A: 2.0% minimum monthly put yield, 2.5% target. Can flex lower only for
  excellent structure.
- Tier B: 2.5% minimum and target monthly put yield.
- Tier C: 3.0% minimum monthly put yield, 4.0% target. Use stricter risk,
  liquidity, and one-contract sizing.

## Technical Structure

The preferred setup is temporary weakness in a quality name, not a collapsing
trend. The strategy should evaluate:

- Recent drawdown or pullback.
- Strike location relative to support.
- Whether support has recently failed.
- RSI and overextension.
- ATR or realized-volatility risk.
- Trend health around key moving averages.
- Cushion relative to expected move.

## Covered Calls After Assignment

If assigned, the strategy wheels the shares by selling covered calls.

- Call strike should be 5-10% above breakeven.
- Covered-call monthly yield target: at least 2%.
- Avoid earnings before expiration when selecting calls.
- If a 2% covered-call yield is unavailable above breakeven, future research
  should compare waiting, extending DTE, lowering yield, or choosing a different
  strike.

## Exit Management

Current discretionary management rules to model:

- Close at 50% or better premium capture before halfway to expiration.
- Close at 75% or better premium capture around three weeks in.
- Almost always close at 90-95% premium capture near expiration.
- If assigned, wheel.
- Future research should compare assignment/wheeling against rolling.

## Portfolio Constraints

Initial constraints:

- Maximum universe size: 40 names.
- Publish top 4 ideas from up to 8 candidates per run.
- One idea per ticker.
- No more than two ideas from one sector in the top list.
- Tier C should use one-contract sizing and limited concurrent exposure.
- VIX cash-reserve guidance should eventually become enforceable.

The exact portfolio constraints are still a research area. They should be
parameterized before drawing conclusions from historical results.

## Success Metrics

Parameter comparisons should prioritize:

- Return on collateral.
- Max drawdown.
- Assignment rate and assignment quality.
- Worst trade.
- Consistency across years and regimes.
- Number of trades after filters.
- Sector and single-name concentration.

Raw premium collected is not enough. The strategy should reward premium only
when it is justified by structure, cushion, liquidity, and portfolio risk.

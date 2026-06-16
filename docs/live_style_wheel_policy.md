# Live-Style Wheel Policy

This document captures the current discretionary operating policy to model in
the next research runner. It is not an optimized strategy claim; it is the
baseline behavior the simulator should try to reproduce before drawing
conclusions about historical returns.

## Goal

Model the live Schwab-scanner-driven wheel workflow closely enough to compare
historical results against the current target of roughly 3% monthly premium
return.

The key question is not only whether a mechanical scanner can find profitable
puts. The key question is whether a portfolio policy with realistic deployment,
trade cadence, assignment handling, and covered-call follow-through can
replicate the live process.

## Entry Cadence

- Open trades on any trading day when good opportunities appear.
- Do not restrict entry to Fridays.
- The runner should support daily scans and should also allow lower-frequency
  variants for comparison.
- Candidate selection should be portfolio-aware: skip otherwise good ideas when
  exposure, earnings, liquidity, or deployment constraints make them unsuitable.

## Deployment And Position Limits

- Target capital deployment: 65-85%.
- Deployment may exceed 100% in some live cases, but this should be reported
  clearly as gross exposure.
- Typical open positions: around 10 total positions, puts and calls combined.
- Max contracts per ticker: normally 3.
- Per-symbol and sector exposure caps should be parameterized.
- The runner should report both average and max utilization.

## Put Entry Rules

- Max put delta: 0.25 absolute delta.
- Typical DTE: 30-35 days.
- Minimum monthly return target: 2.5%.
- Return calculation: premium divided by collateral.
- Avoid earnings before expiration whenever possible.
- Prefer names that are acceptable assignment candidates.
- The runner should preserve enough diagnostics to explain why each selected
  candidate was chosen over alternatives.

## Winner Management

- Close winners at 50% premium capture before the two-week mark.
- After roughly two weeks, prefer closing around 90% premium capture.
- The exact transition point should be parameterized, but the default should
  match the live behavior above.

## Challenged Put Management

The simulator should compare two primary paths:

1. Roll the put when a replacement contract remains inside the return target.
2. Take assignment and transition to covered calls.

Rolling should be allowed only when the new put still satisfies the live-style
return range and earnings rules. The runner should track whether rolling
actually improves total return and drawdown versus assignment plus covered
calls.

## Assignment And Covered Calls

- Assignment is acceptable on selected names.
- After assignment, look for 30-35 DTE covered calls.
- Covered-call monthly return target: at least 2.0-2.5%.
- Return calculation should use premium relative to share basis or assigned
  capital consistently, and the report should state which convention is used.
- If no acceptable covered call is available, the policy should allow the shares
  to remain uncovered and mark them to market.

## Research Comparisons Needed

The next runner should compare:

- Daily scan cadence vs weekly scan cadence.
- 65%, 75%, 85%, and 100% deployment targets.
- 0.20, 0.25, and 0.28 max delta.
- 2.0%, 2.5%, and 3.0% monthly put yield floors.
- Max 1, 2, and 3 contracts per ticker.
- Rolling challenged puts vs taking assignment and selling covered calls.
- Strict no-earnings policy vs looser variants if data quality allows.

## Required Reporting

Reports should separate:

- Realized premium PnL.
- Unrealized share PnL.
- Total mark-to-market equity.
- Monthly realized return.
- Monthly total equity return.
- Capital deployment by day.
- Open positions by day.
- Assignment events.
- Roll events.
- Covered-call availability after assignment.
- Max drawdown and drawdown duration.
- Single-symbol and sector concentration.

The report should explicitly call out when returns are driven primarily by open
share appreciation instead of repeatable option premium.

## Open Questions

- Exact sector caps.
- Exact behavior when a challenged put can be rolled for some credit but below
  the normal return target.
- Whether covered-call return should be measured against assigned collateral,
  adjusted basis, or current market value.
- Whether to allow rolling covered calls, and under what conditions.
- How to model discretionary rejection of candidates that pass numeric filters
  but look technically weak.

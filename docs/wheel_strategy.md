# Wheel Strategy Definition

This is the intended v1 strategy contract before implementing assignment-aware backtests.

## Goal

Model the actual cash-secured put / covered-call wheel instead of treating every short put as cash-settled at expiration.

The current portfolio validator is useful for collateral gating, but it does not carry assigned shares forward. This strategy definition closes that gap before the full long-range backfill is used for research conclusions.

## Account Assumptions

- Starting cash: 100,000 by default.
- Underlying: SPY by default.
- Contract size: 1 option contract = 100 shares.
- Short puts must be cash-secured.
- Short calls must be covered by assigned shares.
- No margin borrowing in v1.
- No fractional share lots.
- Commissions follow the existing option commission setting.

## Lifecycle

### 1. Flat State

When the account has no SPY shares and no open SPY option position:

- Select a put candidate.
- Default put window: 30-60 DTE.
- Default put target delta: -0.10.
- Sell one cash-secured put only if available cash can reserve strike * 100.

### 2. Short Put State

While a short put is open:

- Do not open another SPY put by default.
- Mark the short put daily.
- Optional take-profit / stop-loss rules may close the put early.
- If the put expires OTM, keep the premium and return to flat state.
- If the put expires ITM, assign shares.

### 3. Assigned Shares State

On put assignment:

- Buy 100 SPY shares at the put strike.
- Keep the put premium.
- Track share cost basis as strike minus net put premium per share.
- Mark shares daily.
- Do not realize a stock loss by default.
- Sell covered calls only against owned shares.

### 4. Covered Call State

When holding 100 SPY shares and no open call:

- Select a covered call candidate.
- Default call window: 30-45 DTE.
- Default call target delta: +0.20.
- Call strike must be at or above cost basis unless explicitly overridden later.
- Sell one covered call.

While a covered call is open:

- If it expires OTM, keep the call premium and continue holding shares.
- If it expires ITM, shares are called away at the call strike.
- Realized stock PnL is call strike minus share cost basis, times shares.
- Return to flat state after shares are called away.

## Cost Basis

For assigned puts:

    share_cost_basis = put_strike - net_put_premium_per_share

For covered calls, premium reduces effective wheel basis for reporting, but the minimum call strike rule uses assigned share cost basis by default.

## Risk Rules

V1 defaults:

- No put stop-loss.
- No put take-profit unless configured.
- No covered-call stop-loss.
- Do not sell calls below assigned cost basis.
- Do not sell shares at a realized loss.
- Only one SPY wheel lot at a time for a 100,000 account unless position sizing is explicitly changed.

## Required Backtest Enhancements

Before claiming wheel performance, the engine must support:

- Stock positions.
- Assignment events.
- Covered-call exercise.
- Share cost basis tracking.
- Combined option premium plus stock PnL reporting.
- Portfolio state with cash, stock market value, option marks, reserved collateral, and covered-call obligations.

## First Implementation Target

Use the existing six-month SPY window first:

- Start with 100,000.
- Sell weekly candidate only when flat.
- Carry assigned shares.
- Sell calls after assignment.
- Compare against the current cash-settled short-put portfolio result.


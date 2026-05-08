# ADR-003: 90-Day Paper Trading Gate Before Real Money

## Status
ACCEPTED (2026-04-07) | ACTIVE (Gate window: 2026-04-29 → 2026-07-27)

## Context
After building the trading system, the question arose: when is it safe to use real money?
Common mistakes in retail algo trading:
- Going live too early on backtest results only
- Using real money without proven out-of-sample performance
- No objective exit criteria if strategy fails

## Decision
Require 90 days of live paper trading with measurable gate criteria before
any real money is deployed.

Gate criteria (enforced by gate_progress.py):
- Sharpe Ratio > 1.0 (annualized from daily P&L)
- Max Drawdown < 15%
- Profit Factor > 1.5 (sum of wins / sum of losses)
- Minimum 30 trades (FILLED orders, excluding test fills)

## Rationale
- 90 days covers multiple market regimes (FOMC, earnings, macro events)
- 30+ trades provides minimal statistical significance
- Profit Factor > 1.5 is more appropriate than win-rate for momentum strategies
- gate_progress.py computes metrics daily and stores in Cloud SQL audit table

## Consequences
Positive:
- Prevents premature real money allocation
- Creates objective, falsifiable success criteria
- Daily gate_progress.py provides accountability

Negative:
- Delays real money by at least 90 days
- Small sample size (30 trades) still has wide confidence intervals
- Bull market during paper period may overstate strategy edge

## Gate Window
- Started: 2026-04-29 (after precious metals universe rebalance)
- Ends: 2026-07-27 (Day 90)
- Current: Day 9/90 as of 2026-05-07

## If Gate Passes
Deploy $1,000-2,000 USD real money with half position sizing for 30-day observation.
Scale to $2,500 only after live profitability confirmed.

## If Gate Fails
Freeze strategy, write postmortem, pivot 80% of hours to consulting/career.
Do not modify failed strategy in-place — treat as falsified hypothesis.

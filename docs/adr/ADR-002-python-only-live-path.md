# ADR-002: Python-only Live Trading Path (Retire Rust gRPC)

## Status
ACCEPTED (2026-04-17)

## Context
Phase 1 built a Rust OMS (Order Management System) with gRPC interface.
The Rust engine enforced risk rules and submitted orders.
When Cloud Run Jobs was adopted (ADR-001), the Rust gRPC server could not run
in a stateless container environment.

## Decision
Retire the Rust gRPC live path. Use alpaca_direct.py (Python REST client) for
all live order submission. Keep Rust codebase for unit testing only.

## Rationale
- Cloud Run Jobs cannot maintain a persistent gRPC server
- alpaca_direct.py replicates all risk checks (sector limits, stop loss, dedup)
- Python REST is simpler to debug and monitor in Cloud Logging
- Rust codebase still validates risk logic via 47 unit tests

## Consequences
Positive:
- Simplified deployment (Python-only runtime)
- Easier debugging via structured logging
- Reduced Docker image complexity

Negative:
- Rust latency advantage lost (irrelevant for daily momentum strategy)
- Risk logic now duplicated in Python (must keep in sync with Rust tests)
- Future HFT scaling would require re-activating Rust path

## Current State
Rust codebase archived in core/ directory.
All live orders go through: run_strategy.py → alpaca_direct.py → Alpaca REST API

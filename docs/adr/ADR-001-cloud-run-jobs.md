# ADR-001: Cloud Run Jobs as Primary Execution Environment

## Status
ACCEPTED (2026-04-07)

## Context
QuantAI requires a scheduled daily execution at 22:00 UTC (after US market close).
Options considered:
1. Always-on VPS ($20-30/month, manual maintenance)
2. Cloud Run Service (HTTP-triggered, cold start issues)
3. Cloud Run Jobs (task-oriented, pay-per-second)
4. Local cron job (not reliable, single point of failure)

## Decision
Use Cloud Run Jobs with a cron schedule via Cloud Scheduler.

## Rationale
- Strategy runs for ~5 minutes/day — paying for 1,435 idle minutes is wasteful
- Jobs are stateless and versioned via Docker images
- Built-in retry logic and structured logging to Cloud Logging
- Cost: ~$0.85/month vs $20-30/month for always-on

## Consequences
Positive:
- Total GCP cost reduced to $11/month
- Zero maintenance overhead for compute layer
- Automatic failure detection and alerting

Negative:
- Cold start adds ~30 seconds to each run
- Cannot run Rust gRPC server (stateless environment)
- Requires Docker image rebuild for every code change

## Related Decisions
- ADR-002: Python-only live path (consequence of this decision)

-- trading-system/scripts/seed_ohlcv.sql
--
-- Seed 30 days of realistic daily OHLCV bars for AAPL, BTC-USD, EUR-USD.
-- Run:  psql $DATABASE_URL -f scripts/seed_ohlcv.sql
--
-- Price models (sin-wave variation for realistic-looking motion):
--   AAPL:    base $178, ±$6 amplitude
--   BTC-USD: base $67,000, ±$4,000 amplitude
--   EUR-USD: base $1.0870, ±$0.009 amplitude
--
-- OHLCV relationships guaranteed:
--   high  = max(open, close) + intraday range extension
--   low   = min(open, close) - intraday range extension
--   vwap  = (open + high + low + close) / 4

-- Remove any existing data for the seeded range so this script is idempotent
DELETE FROM ohlcv
WHERE symbol IN ('AAPL', 'BTC-USD', 'EUR-USD')
  AND timestamp >= '2026-02-25 00:00:00+00'::timestamptz
  AND timestamp <  '2026-03-27 00:00:00+00'::timestamptz;

-- ── AAPL: ~$172–$184 range (30 days) ─────────────────────────────────────────
INSERT INTO ohlcv (symbol, timestamp, open, high, low, close, volume, vwap)
SELECT
  'AAPL',
  ('2026-02-25 00:00:00+00'::timestamptz + (i || ' days')::interval),
  -- open
  ROUND((178.0 + 6.0 * sin(i * 0.40))::numeric, 2),
  -- high = max(open, close) + intraday extension
  ROUND((
    GREATEST(178.0 + 6.0 * sin(i * 0.40), 178.0 + 6.0 * sin((i + 1) * 0.40))
    + 2.0 + 2.5 * abs(sin(i * 1.7))
  )::numeric, 2),
  -- low = min(open, close) - intraday extension
  ROUND((
    LEAST(178.0 + 6.0 * sin(i * 0.40), 178.0 + 6.0 * sin((i + 1) * 0.40))
    - 2.0 - 2.5 * abs(cos(i * 1.7))
  )::numeric, 2),
  -- close
  ROUND((178.0 + 6.0 * sin((i + 1) * 0.40))::numeric, 2),
  -- volume (~45M shares/day ± 8M)
  ROUND((45000000.0 + 8000000.0 * abs(sin(i * 0.70)))::numeric, 0),
  -- vwap ≈ typical price
  ROUND((
    (178.0 + 6.0 * sin(i * 0.40))
    + (178.0 + 6.0 * sin((i + 1) * 0.40))
    + (GREATEST(178.0 + 6.0 * sin(i * 0.40), 178.0 + 6.0 * sin((i + 1) * 0.40)) + 2.0 + 2.5 * abs(sin(i * 1.7)))
    + (LEAST(178.0 + 6.0 * sin(i * 0.40), 178.0 + 6.0 * sin((i + 1) * 0.40)) - 2.0 - 2.5 * abs(cos(i * 1.7)))
  )::numeric / 4.0, 4)
FROM generate_series(0, 29) AS i;

-- ── BTC-USD: ~$63,000–$71,000 range (30 days) ────────────────────────────────
INSERT INTO ohlcv (symbol, timestamp, open, high, low, close, volume, vwap)
SELECT
  'BTC-USD',
  ('2026-02-25 00:00:00+00'::timestamptz + (i || ' days')::interval),
  -- open
  ROUND((67000.0 + 4000.0 * sin(i * 0.35))::numeric, 2),
  -- high
  ROUND((
    GREATEST(67000.0 + 4000.0 * sin(i * 0.35), 67000.0 + 4000.0 * sin((i + 1) * 0.35))
    + 700.0 + 1500.0 * abs(sin(i * 1.4))
  )::numeric, 2),
  -- low
  ROUND((
    LEAST(67000.0 + 4000.0 * sin(i * 0.35), 67000.0 + 4000.0 * sin((i + 1) * 0.35))
    - 700.0 - 1500.0 * abs(cos(i * 1.4))
  )::numeric, 2),
  -- close
  ROUND((67000.0 + 4000.0 * sin((i + 1) * 0.35))::numeric, 2),
  -- volume (~25,000 BTC/day ± 5,000)
  ROUND((25000.0 + 5000.0 * abs(sin(i * 0.90)))::numeric, 4),
  -- vwap
  ROUND((
    (67000.0 + 4000.0 * sin(i * 0.35))
    + (67000.0 + 4000.0 * sin((i + 1) * 0.35))
    + (GREATEST(67000.0 + 4000.0 * sin(i * 0.35), 67000.0 + 4000.0 * sin((i + 1) * 0.35)) + 700.0 + 1500.0 * abs(sin(i * 1.4)))
    + (LEAST(67000.0 + 4000.0 * sin(i * 0.35), 67000.0 + 4000.0 * sin((i + 1) * 0.35)) - 700.0 - 1500.0 * abs(cos(i * 1.4)))
  )::numeric / 4.0, 2)
FROM generate_series(0, 29) AS i;

-- ── EUR-USD: ~$1.075–$1.099 range (30 days) ───────────────────────────────────
INSERT INTO ohlcv (symbol, timestamp, open, high, low, close, volume, vwap)
SELECT
  'EUR-USD',
  ('2026-02-25 00:00:00+00'::timestamptz + (i || ' days')::interval),
  -- open
  ROUND((1.0870 + 0.009 * sin(i * 0.45))::numeric, 5),
  -- high
  ROUND((
    GREATEST(1.0870 + 0.009 * sin(i * 0.45), 1.0870 + 0.009 * sin((i + 1) * 0.45))
    + 0.0012 + 0.0015 * abs(sin(i * 1.5))
  )::numeric, 5),
  -- low
  ROUND((
    LEAST(1.0870 + 0.009 * sin(i * 0.45), 1.0870 + 0.009 * sin((i + 1) * 0.45))
    - 0.0012 - 0.0015 * abs(cos(i * 1.5))
  )::numeric, 5),
  -- close
  ROUND((1.0870 + 0.009 * sin((i + 1) * 0.45))::numeric, 5),
  -- volume (~3.5M units/day ± 500K)
  ROUND((3500000.0 + 500000.0 * abs(sin(i * 0.60)))::numeric, 0),
  -- vwap
  ROUND((
    (1.0870 + 0.009 * sin(i * 0.45))
    + (1.0870 + 0.009 * sin((i + 1) * 0.45))
    + (GREATEST(1.0870 + 0.009 * sin(i * 0.45), 1.0870 + 0.009 * sin((i + 1) * 0.45)) + 0.0012 + 0.0015 * abs(sin(i * 1.5)))
    + (LEAST(1.0870 + 0.009 * sin(i * 0.45), 1.0870 + 0.009 * sin((i + 1) * 0.45)) - 0.0012 - 0.0015 * abs(cos(i * 1.5)))
  )::numeric / 4.0, 5)
FROM generate_series(0, 29) AS i;

-- ── Verification query ────────────────────────────────────────────────────────
SELECT
  symbol,
  COUNT(*)                          AS days,
  ROUND(MIN(low)::numeric, 4)       AS price_min,
  ROUND(MAX(high)::numeric, 4)      AS price_max,
  ROUND(AVG(close)::numeric, 4)     AS price_avg,
  MIN(timestamp)::date              AS first_date,
  MAX(timestamp)::date              AS last_date
FROM ohlcv
WHERE symbol IN ('AAPL', 'BTC-USD', 'EUR-USD')
  AND timestamp >= '2026-02-25'::date
GROUP BY symbol
ORDER BY symbol;

# trading-system/strategy/src/signals/momentum.py
#
# Dual Moving Average crossover + RSI mean-reversion strategy.
#
# Signal sources (any can generate a trade; highest-priority fires):
#   1. RSI mean-reversion: RSI(rsi_period) enters oversold/overbought zone
#      - RSI < rsi_oversold → BUY  (buy the dip; standalone, no MA required)
#      - RSI > rsi_overbought → SELL  (take profit; standalone)
#      - No volume requirement; disabled for FX (sparse volume)
#      - Long-term trend filter (trend_period) applies if configured
#   2. Bollinger Band mean-reversion: price touches 2σ band
#      - price < BB_lower → BUY (oversold, requires MA uptrend + volume)
#      - price > BB_upper → SELL (overbought, requires volume)
#      - Disabled for FX (sparse volume)
#   3. MA crossover: fast MA crosses slow MA (trend-following)
#      - Requires volume confirmation (or sparse-volume auto-bypass for FX)
#      - Noise filter: spread must be >= noise_filter_bps
#
# Signal score [0.0, 1.0]:
#   - Base: 0.55 (minimum to pass risk engine)
#   - Bonus up to 0.45 from:
#     - RSI extremity (distance from rsi_oversold/overbought): up to +0.30
#     - MA spread magnitude (normalised by price): up to +0.15
#
# Position sizing (ATR-based, when atr_period > 0):
#   qty = (portfolio * atr_risk_pct) / ATR(atr_period)
#   Capped at 5% of portfolio (matching risk engine hard limit).
#   Stop loss placed at entry ± 1× ATR instead of slow MA ± 1%.
#
# Parameters: 10 (fast_period, slow_period, vol_period, noise_filter_bps,
#                rsi_oversold, rsi_overbought, rsi_period, bb_period,
#                bb_std_dev, trend_period, atr_period, atr_risk_pct)
#
# WARNING: 30 days of data is INSUFFICIENT for production use.
# The design requires min 252 trading days lookback. This implementation
# uses all available data and flags the result accordingly.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd

from . import Direction, SignalResult

logger = logging.getLogger(__name__)

STRATEGY_ID = "momentum_v1"
MIN_REQUIRED_BARS = 30   # Absolute minimum to compute slow MA
PRODUCTION_MIN_BARS = 252  # Per design — flag if below this

# Default RSI period — 7-period (fast, tuned in Phase 3 simulation).
# Override via MomentumConfig.rsi_period for RSI(14) mean-reversion layer.
_DEFAULT_RSI_PERIOD = 7


def _compute_rsi(close: pd.Series, period: int) -> pd.Series:
    """Wilder Smoothed RSI.  Returns NaN for bars before the first full window."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


@dataclass
class MomentumConfig:
    """Tunable parameters."""

    fast_period: int = 10    # Fast MA lookback
    slow_period: int = 30    # Slow MA lookback
    vol_period: int = 20     # Volume average lookback
    noise_filter_bps: float = 5.0   # Ignore crossovers where spread < 5bps
    rsi_oversold: float = 30.0      # RSI buy threshold (oversold)
    rsi_overbought: float = 70.0    # RSI sell threshold (overbought)
    rsi_period: int = _DEFAULT_RSI_PERIOD  # RSI lookback (7 = fast/Phase3, 14 = Wilder standard)
    bb_period: int = 0              # Bollinger Band period (0 = disabled; BB SELL removed as it cuts trend profits)
    bb_std_dev: float = 2.0         # Bollinger Band std dev multiplier
    trend_period: int = 0           # Long-term trend filter period (0 = disabled)

    # ATR-based position sizing & stop loss (0 = disabled, use fixed position_pct)
    # When enabled: qty = (portfolio * atr_risk_pct) / ATR(atr_period), capped at 5%.
    # Stop loss placed at entry ± 1× ATR instead of slow_ma ± 1%.
    atr_period: int = 14
    atr_risk_pct: float = 0.01      # fraction of portfolio risked per ATR unit

    # RSI score multiplier filter (applied after direction is determined):
    #   BUY + RSI < rsi_oversold  → score × 1.5  (boost oversold conviction)
    #   BUY + RSI > rsi_overbought → score × 0.3  (suppress buying overbought)
    #   SELL or HOLD → no change
    # Distinct from the standalone RSI signal: this adjusts existing MA/BB scores.
    rsi_filter: bool = True

    # Trend continuation BUY: enter established uptrends on a mild RSI pullback.
    #
    # Fires when ALL of:
    #   - fast MA has been above slow MA for ≥ trend_ride_min_bars consecutive bars
    #   - rsi_oversold < RSI < trend_ride_rsi   (pullback zone, not yet oversold)
    #   - price > slow MA  (uptrend intact)
    #   - non-FX symbol (sparse-volume instruments excluded)
    #
    # Priority: RSI oversold → RSI overbought → trend_ride → BB → MA crossover.
    # Regime filter applies normally (blocked in BEAR, reduced in NEUTRAL).
    # Set trend_ride_rsi=0 to disable.
    trend_ride_rsi: float = 45.0      # RSI pullback threshold in established uptrend
    trend_ride_min_bars: int = 10     # Min consecutive bars with fast > slow

    # Market regime filter — suppress BUY signals in bear markets.
    #
    # Regime is detected from a proxy symbol (default: SPY) using a long-period MA:
    #   BULL:    price > MA(regime_ma_period) × (1 + regime_neutral_pct)   → BUY allowed
    #   NEUTRAL: price within ±regime_neutral_pct of MA                    → BUY score × 0.7
    #   BEAR:    price < MA(regime_ma_period) × (1 - regime_neutral_pct)   → BUY blocked
    #
    # SELL signals always pass regardless of regime (take profit / exit).
    # When SPY data is unavailable, defaults to BULL (no blocking).
    #
    # Live path: call strategy.update_regime(spy_df) before the symbol loop.
    # Backtest path: pass regime_df=spy_df to BacktestEngine.run()/walk_forward().
    regime_filter: bool = True
    regime_symbol: str = "SPY"        # Proxy for market regime (metadata only)
    regime_ma_period: int = 200       # MA period for regime detection (200 = classic)
    regime_neutral_pct: float = 0.02  # ±2% band around MA200 treated as NEUTRAL


@dataclass
class MomentumFeatures:
    """Feature vector logged to signals table for audit/ML research."""

    fast_ma: float
    slow_ma: float
    ma_spread_bps: float        # (fast - slow) / slow * 10_000
    vol_ratio: float            # current_volume / avg_volume
    prev_fast_ma: float
    prev_slow_ma: float
    bars_available: int
    production_ready: bool      # True if bars >= 252


class MomentumStrategy:
    """Dual MA crossover with volume confirmation + market regime filter.

    Usage::

        strategy = MomentumStrategy()
        # Live: set regime once from SPY before the symbol loop
        strategy.update_regime(spy_df)      # caches BULL/BEAR/NEUTRAL
        df = fetcher.fetch("AAPL", days=30)
        result = strategy.generate_signal("AAPL", df, portfolio_value=100_000)
        # result: SignalResult with direction, score, stop_loss, features

        # Backtest: pass SPY data for bar-by-bar regime filtering
        engine.walk_forward("AAPL", df, strategy, regime_df=spy_df)
    """

    def __init__(self, config: MomentumConfig = MomentumConfig()) -> None:
        self.config = config
        # Cached market regime (updated by update_regime(); default BULL = permissive)
        self._regime: str = "BULL"
        self._spy_price: Optional[float] = None
        self._spy_ma200: Optional[float] = None

    # ── Market regime ─────────────────────────────────────────────────────────

    def update_regime(self, spy_df: pd.DataFrame) -> str:
        """Compute and cache the current market regime from SPY (or proxy) data.

        Call this once before the symbol loop in the live path.  The cached
        regime is then used by every subsequent generate_signal() call.

        Regime logic (SPY price vs MA(regime_ma_period)):
          BULL:    price > MA × (1 + regime_neutral_pct)
          NEUTRAL: price within ±regime_neutral_pct of MA
          BEAR:    price < MA × (1 - regime_neutral_pct)

        Args:
            spy_df: OHLCV DataFrame for the proxy symbol (SPY).
                    Needs ≥ regime_ma_period bars; falls back to BULL if short.

        Returns:
            Regime string: "BULL", "NEUTRAL", or "BEAR".
        """
        if not self.config.regime_filter:
            return "BULL"  # filter disabled — no-op

        period = self.config.regime_ma_period
        if spy_df.empty or len(spy_df) < period:
            logger.warning(
                "Insufficient SPY data for regime detection "
                "(%d bars, need %d) — defaulting to BULL.",
                len(spy_df) if not spy_df.empty else 0,
                period,
            )
            self._regime = "BULL"
            return "BULL"

        # Staleness check: warn if latest bar is more than 7 calendar days old.
        # Stale data during a correction could lock the regime in BEAR and block
        # all BUY signals even after the market recovers.
        import datetime as _dt
        latest_date = spy_df.index[-1]
        if hasattr(latest_date, "date"):
            latest_date = latest_date.date()
        data_age_days = (_dt.date.today() - latest_date).days
        if data_age_days > 30:
            logger.warning(
                "SPY data is %d days stale (latest bar: %s) — "
                "regime may be incorrect; defaulting to BULL. "
                "Run seed_alpaca.py or seed_yfinance.py to refresh.",
                data_age_days,
                latest_date,
            )
            self._regime = "BULL"
            return "BULL"
        elif data_age_days > 7:
            logger.warning(
                "SPY data is %d days stale (latest bar: %s) — "
                "regime computation may not reflect current market.",
                data_age_days,
                latest_date,
            )

        spy_close = spy_df["close"]
        ma_val = float(spy_close.rolling(period).mean().iloc[-1])
        spy_price = float(spy_close.iloc[-1])

        if np.isnan(ma_val) or ma_val == 0:
            self._regime = "BULL"
            return "BULL"

        ratio = (spy_price - ma_val) / ma_val
        neutral_pct = self.config.regime_neutral_pct

        if ratio > neutral_pct:
            regime = "BULL"
        elif ratio < -neutral_pct:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        prev_regime = self._regime
        self._regime = regime
        self._spy_price = spy_price
        self._spy_ma200 = ma_val

        logger.info(
            "Market regime: %s  SPY=%.2f  MA%d=%.2f  delta=%.2f%%",
            regime, spy_price, period, ma_val, ratio * 100,
        )
        if prev_regime != regime:
            logger.warning(
                "REGIME CHANGE: %s → %s  (SPY %.2f vs MA%d %.2f)",
                prev_regime, regime, spy_price, period, ma_val,
            )
        return regime

    @property
    def current_regime(self) -> str:
        """Return the cached market regime ('BULL', 'NEUTRAL', or 'BEAR')."""
        return self._regime

    def generate_signal(
        self,
        symbol: str,
        df: pd.DataFrame,
        portfolio_value: float = 100_000.0,
        position_pct: float = 0.02,      # Target 2% of portfolio per trade
    ) -> SignalResult:
        """Generate a trading signal from OHLCV bars.

        Args:
            symbol:          Ticker symbol.
            df:              OHLCV DataFrame (index=timestamp, float64 columns).
            portfolio_value: Current portfolio value — used for position sizing.
            position_pct:    Fraction of portfolio to allocate (default 2%).

        Returns:
            SignalResult — always returns a result (HOLD if insufficient data
            or no crossover detected).

        Raises:
            ValueError: If DataFrame is missing required columns.
        """
        if df.empty or len(df) < self.config.slow_period:
            logger.warning(
                "%s: insufficient bars (%d < %d required). HOLD.",
                symbol,
                len(df) if not df.empty else 0,
                self.config.slow_period,
            )
            return SignalResult(
                strategy_id=STRATEGY_ID,
                symbol=symbol,
                direction=Direction.HOLD,
                score=0.0,
                features={"reason": "insufficient_data", "bars": len(df)},
            )

        # ── Compute indicators ────────────────────────────────────────────────
        close = df["close"]
        volume = df["volume"]

        fast_ma = close.rolling(self.config.fast_period).mean()
        slow_ma = close.rolling(self.config.slow_period).mean()
        vol_avg = volume.rolling(self.config.vol_period).mean()

        # Current bar values
        curr_fast = fast_ma.iloc[-1]
        curr_slow = slow_ma.iloc[-1]
        prev_fast = fast_ma.iloc[-2]
        prev_slow = slow_ma.iloc[-2]
        curr_price = close.iloc[-1]
        curr_vol = volume.iloc[-1]
        avg_vol = vol_avg.iloc[-1]

        if any(np.isnan(v) for v in [curr_fast, curr_slow, prev_fast, prev_slow, avg_vol]):
            return SignalResult(
                strategy_id=STRATEGY_ID,
                symbol=symbol,
                direction=Direction.HOLD,
                score=0.0,
                features={"reason": "nan_in_indicators"},
            )

        # ── RSI mean-reversion layer ──────────────────────────────────────────
        # Uses rsi_period from config (default 7 for speed, 14 for Wilder standard).
        # RSI BUY is a standalone mean-reversion signal: no MA uptrend required.
        # Only the long-term trend filter (if trend_period > 0) gates BUY direction.
        rsi_series = _compute_rsi(close, self.config.rsi_period)
        _rsi_raw = float(rsi_series.iloc[-1])
        if np.isnan(_rsi_raw):
            logger.debug(
                "%s: RSI NaN (insufficient history for period=%d) — using neutral 50.0",
                symbol, self.config.rsi_period,
            )
            curr_rsi = 50.0
        else:
            curr_rsi = _rsi_raw
        rsi_buy = curr_rsi < self.config.rsi_oversold
        rsi_sell = curr_rsi > self.config.rsi_overbought

        # ── Bollinger Bands (mean-reversion BUY signal only; disabled if bb_period=0)
        # Note: BB SELL is intentionally omitted — it cuts momentum profits prematurely.
        if self.config.bb_period > 0:
            bb_mid_s = close.rolling(self.config.bb_period).mean()
            bb_std_s = close.rolling(self.config.bb_period).std()
            bb_upper_val = float(bb_mid_s.iloc[-1] + self.config.bb_std_dev * bb_std_s.iloc[-1])
            bb_lower_val = float(bb_mid_s.iloc[-1] - self.config.bb_std_dev * bb_std_s.iloc[-1])
            bb_valid = not (np.isnan(bb_upper_val) or np.isnan(bb_lower_val))
        else:
            bb_upper_val = float('nan')
            bb_lower_val = float('nan')
            bb_valid = False

        # ── MA spread and crossover detection ─────────────────────────────────
        ma_spread_bps = (curr_fast - curr_slow) / curr_slow * 10_000
        bullish_cross = prev_fast <= prev_slow and curr_fast > curr_slow
        bearish_cross = prev_fast >= prev_slow and curr_fast < curr_slow
        spread_abs_bps = abs(ma_spread_bps)

        # ── Volume confirmation (MA signals only) ─────────────────────────────
        # Sparse-volume detection: FX instruments (EUR-USD, etc.) have no
        # centralised exchange volume. If >50% of bars are zero, skip the
        # volume filter entirely so MA crossovers still generate signals.
        sparse_volume = (volume == 0).sum() / len(volume) > 0.5
        vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0.0
        volume_confirmed = sparse_volume or vol_ratio >= 1.0

        # Price momentum confirmation for MA BUY: close must be above where it
        # was fast_period bars ago.  Prevents buying dead-cat bounces where the
        # MA crosses on a temporary uptick inside a larger downtrend.
        prev_price = (
            close.iloc[-1 - self.config.fast_period]
            if len(close) > self.config.fast_period else curr_price
        )
        price_momentum_up = curr_price > prev_price

        # Higher noise threshold for sparse-volume (FX): weak crossovers cause
        # whipsaws in trending markets. 4x multiplier requires ~20bps min spread.
        noise_threshold = self.config.noise_filter_bps * (4.0 if sparse_volume else 1.0)

        # ── Trend continuation BUY ────────────────────────────────────────────
        # Catches entries in established uptrends that started before live trading.
        # Fires when fast > slow for N consecutive bars AND RSI is in a pullback zone.
        # Disabled for sparse-volume (FX) instruments — same rule as RSI mean-reversion.
        trend_ride_buy = False
        if (
            self.config.trend_ride_rsi > 0
            and not sparse_volume
            and not rsi_buy  # not already at oversold threshold
            and not rsi_sell
            and curr_fast > curr_slow
            and self.config.rsi_oversold < curr_rsi < self.config.trend_ride_rsi
        ):
            n = self.config.trend_ride_min_bars
            if len(fast_ma) >= n and len(slow_ma) >= n:
                fa_tail = fast_ma.values[-n:]
                sa_tail = slow_ma.values[-n:]
                if not np.any(np.isnan(fa_tail)) and not np.any(np.isnan(sa_tail)):
                    trend_ride_buy = bool(np.all(fa_tail > sa_tail))

        # MA-based direction (requires noise filter + volume confirmation + price momentum)
        if spread_abs_bps >= noise_threshold and volume_confirmed:
            if bullish_cross and price_momentum_up:
                ma_direction = Direction.BUY
            elif bearish_cross:
                ma_direction = Direction.SELL
            else:
                ma_direction = Direction.HOLD
        else:
            ma_direction = Direction.HOLD

        # Long-term trend filter: BUY signals only fire when close > trend_ma.
        # Prevents buying into sustained downtrends (e.g., 2025 tech correction).
        if self.config.trend_period > 0 and len(close) >= self.config.trend_period:
            trend_ma_val = float(close.rolling(self.config.trend_period).mean().iloc[-1])
            in_uptrend = not np.isnan(trend_ma_val) and curr_price > trend_ma_val
        else:
            in_uptrend = True  # trend filter disabled or insufficient data

        # Mean-reversion signals (RSI + BB) disabled for sparse-volume (FX):
        # FX trends persistently at daily scale; mean-reversion fights the trend.
        if sparse_volume:
            rsi_buy = False
            rsi_sell = False
            bb_valid = False

        # Bollinger Band BUY only: price at/below lower band = oversold dip entry.
        # BB SELL intentionally excluded — it exits trend rides too early.
        bb_buy = (
            bb_valid
            and volume_confirmed
            and curr_price < bb_lower_val
            and curr_fast > curr_slow
            and in_uptrend
        )

        # MA direction also gated by trend filter for BUY
        if ma_direction == Direction.BUY and not in_uptrend:
            ma_direction = Direction.HOLD

        # Combined direction: RSI → trend_ride → BB → MA crossover (priority order)
        # RSI BUY is a standalone mean-reversion signal (no MA uptrend required).
        # trend_ride BUY catches established uptrends that started before live trading.
        # Long-term trend filter (in_uptrend) gates all BUY signals.
        if rsi_buy and in_uptrend:
            direction = Direction.BUY
        elif rsi_sell:
            direction = Direction.SELL  # take profit regardless of trend
        elif trend_ride_buy and in_uptrend:
            direction = Direction.BUY
        elif bb_buy:
            direction = Direction.BUY
        elif ma_direction != Direction.HOLD:
            direction = ma_direction
        else:
            direction = Direction.HOLD

        # ── Score calculation ─────────────────────────────────────────────────
        if direction == Direction.HOLD:
            score = 0.0
        else:
            base = 0.55
            # MA spread component: max +0.15 (saturates at 50 bps)
            spread_component = min(spread_abs_bps / 50.0, 1.0) * 0.15
            # RSI extremity component: max +0.30 (saturates at 0 / threshold)
            if direction == Direction.BUY:
                rsi_component = max(min((self.config.rsi_oversold - curr_rsi) / self.config.rsi_oversold, 1.0), 0.0) * 0.30
            else:
                rsi_component = max(min((curr_rsi - self.config.rsi_overbought) / (100.0 - self.config.rsi_overbought), 1.0), 0.0) * 0.30
            score = round(min(base + spread_component + rsi_component, 1.0), 4)

        # ── RSI score multiplier ──────────────────────────────────────────────
        # Boosts conviction when price is genuinely oversold (RSI < threshold),
        # and suppresses it when a BUY fires into overbought territory.
        # This is separate from the standalone RSI signal above: it modifies
        # the score of MA/BB-triggered BUY signals to reflect RSI context.
        if self.config.rsi_filter and direction == Direction.BUY and score > 0.0:
            if curr_rsi < self.config.rsi_oversold:
                score = round(min(score * 1.5, 1.0), 4)
            elif curr_rsi > self.config.rsi_overbought:
                score = round(score * 0.3, 4)

        # ── Market regime filter ──────────────────────────────────────────────
        # BEAR:    suppress ALL BUY signals → direction=HOLD, score=0
        # NEUTRAL: allow BUY with reduced conviction (score × 0.7)
        # SELL signals always pass regardless of regime (exit / take profit).
        # SPY data must be pre-loaded via update_regime() before calling this.
        if self.config.regime_filter and direction == Direction.BUY:
            if self._regime == "BEAR":
                direction = Direction.HOLD
                score = 0.0
            elif self._regime == "NEUTRAL":
                score = round(score * 0.7, 4)
                if score < 0.55:
                    direction = Direction.HOLD
                    score = 0.0

        # ── ATR computation ───────────────────────────────────────────────────
        # ATR(atr_period) used for adaptive position sizing and stop placement.
        # Falls back to fixed position_pct sizing when atr_period=0 or ATR is NaN.
        atr_val: Optional[float] = None
        if self.config.atr_period > 0 and len(df) >= self.config.atr_period + 1:
            high = df["high"].astype(float)
            low  = df["low"].astype(float)
            prev_close = close.shift(1)
            tr = pd.concat([
                high - low,
                (high - prev_close).abs(),
                (low  - prev_close).abs(),
            ], axis=1).max(axis=1)
            _atr_raw = float(tr.rolling(self.config.atr_period).mean().iloc[-1])
            if not np.isnan(_atr_raw) and _atr_raw > 0:
                atr_val = _atr_raw

        # ── Position sizing ───────────────────────────────────────────────────
        # ATR-based: risk atr_risk_pct of portfolio per ATR unit.
        #   qty = (portfolio * atr_risk_pct) / ATR
        #   Capped at 5% of portfolio (mirrors Rust risk engine hard limit).
        # Fallback: fixed position_pct of portfolio when ATR unavailable.
        if curr_price > 0 and direction != Direction.HOLD:
            if atr_val is not None:
                risk_dollars = portfolio_value * self.config.atr_risk_pct
                raw_qty = risk_dollars / atr_val
                max_qty = (portfolio_value * 0.05) / curr_price   # 5% cap
                raw_qty = min(raw_qty, max_qty)
            else:
                raw_qty = (portfolio_value * position_pct) / curr_price

            # Instrument-appropriate rounding
            if symbol.endswith("-USD") and curr_price > 1000:
                quantity = Decimal(str(round(raw_qty, 4)))  # Crypto: 4dp
            elif curr_price < 10:
                quantity = Decimal(str(round(raw_qty, 0)))  # FX/cheap: whole units
            else:
                quantity = Decimal(str(int(raw_qty)))       # Equities: whole shares
        else:
            quantity = None

        # ── Stop loss ─────────────────────────────────────────────────────────
        # ATR-based: entry ± 1× ATR (adaptive to recent volatility).
        # Fallback: slow MA ± 1% (structure support/resistance).
        # Safety: stop MUST be below entry for BUY and above for SELL.
        # This matters when RSI BUY fires with price < slow_ma (standalone
        # mean-reversion entry): slow_ma * 0.99 would be ABOVE the entry price.
        if direction == Direction.BUY:
            if atr_val is not None:
                stop_price = curr_price - atr_val
            else:
                # Cap at 1% below entry when slow MA is above current price
                stop_price = min(curr_slow * 0.99, curr_price * 0.99)
            stop_loss = Decimal(str(round(stop_price, 5)))
        elif direction == Direction.SELL:
            if atr_val is not None:
                stop_price = curr_price + atr_val
            else:
                # Floor at 1% above entry when slow MA is below current price
                stop_price = max(curr_slow * 1.01, curr_price * 1.01)
            stop_loss = Decimal(str(round(stop_price, 5)))
        else:
            stop_loss = None

        # ── Build features dict ───────────────────────────────────────────────
        bars_available = len(df)
        production_ready = bars_available >= PRODUCTION_MIN_BARS
        if not production_ready and direction != Direction.HOLD:
            logger.warning(
                "%s: signal generated with only %d bars (need %d for production). "
                "Use for backtesting/development only.",
                symbol,
                bars_available,
                PRODUCTION_MIN_BARS,
            )

        features = {
            "fast_ma": round(float(curr_fast), 4),
            "slow_ma": round(float(curr_slow), 4),
            "ma_spread_bps": round(float(ma_spread_bps), 2),
            "vol_ratio": round(float(vol_ratio), 4),
            "prev_fast_ma": round(float(prev_fast), 4),
            "prev_slow_ma": round(float(prev_slow), 4),
            "rsi": round(curr_rsi, 2),
            "rsi_period": self.config.rsi_period,
            "atr": round(atr_val, 5) if atr_val is not None else None,
            "atr_period": self.config.atr_period,
            "bb_upper": round(bb_upper_val, 5) if bb_valid else None,
            "bb_lower": round(bb_lower_val, 5) if bb_valid else None,
            "bars_available": bars_available,
            "production_ready": production_ready,
            "volume_confirmed": volume_confirmed,
            "noise_filter_bps": self.config.noise_filter_bps,
            "regime": self._regime if self.config.regime_filter else "DISABLED",
            "regime_spy_price": round(self._spy_price, 4) if self._spy_price is not None else None,
            "regime_spy_ma200": round(self._spy_ma200, 4) if self._spy_ma200 is not None else None,
            "trend_ride": trend_ride_buy,
        }

        return SignalResult(
            strategy_id=STRATEGY_ID,
            symbol=symbol,
            direction=direction,
            score=score,
            suggested_stop_loss=stop_loss,
            suggested_quantity=quantity,
            features=features,
        )

    def generate_signals_series(
        self,
        symbol: str,
        df: pd.DataFrame,
        regime_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Generate signal for every bar in `df` (used by the backtester).

        Returns a DataFrame aligned with `df` index with columns:
          - direction: "BUY", "SELL", or "HOLD"
          - score:     float in [0.0, 1.0]
          - fast_ma:   float
          - slow_ma:   float
          - ma_spread_bps: float
          - vol_ratio: float
        """
        if len(df) < self.config.slow_period:
            raise ValueError(
                f"Need at least {self.config.slow_period} bars, got {len(df)}"
            )

        close = df["close"]
        volume = df["volume"]

        fast_ma = close.rolling(self.config.fast_period).mean()
        slow_ma = close.rolling(self.config.slow_period).mean()
        vol_avg = volume.rolling(self.config.vol_period).mean()

        signals = pd.DataFrame(index=df.index)
        signals["fast_ma"] = fast_ma
        signals["slow_ma"] = slow_ma
        signals["ma_spread_bps"] = (fast_ma - slow_ma) / slow_ma * 10_000
        signals["vol_ratio"] = volume / vol_avg

        # RSI mean-reversion layer (standalone — no MA uptrend required for BUY)
        rsi = _compute_rsi(close, self.config.rsi_period)
        signals["rsi"] = rsi
        rsi_buy = rsi < self.config.rsi_oversold    # oversold → buy the dip
        rsi_sell = rsi > self.config.rsi_overbought  # overbought → take profit

        # MA crossover detection
        prev_fast = fast_ma.shift(1)
        prev_slow = slow_ma.shift(1)
        bullish = (prev_fast <= prev_slow) & (fast_ma > slow_ma)
        bearish = (prev_fast >= prev_slow) & (fast_ma < slow_ma)

        # Volume confirmation (MA + BB signals only)
        # Sparse-volume detection: skip for FX instruments (>50% zero volume bars)
        sparse_volume = (volume == 0).sum() / len(volume) > 0.5
        if sparse_volume:
            vol_confirmed = pd.Series(True, index=df.index)
        else:
            vol_confirmed = signals["vol_ratio"] >= 1.0

        # Higher noise threshold for sparse-volume (FX): weak crossovers cause
        # whipsaws in trending markets. 4x multiplier requires ~20bps min spread.
        noise_threshold = self.config.noise_filter_bps * (4.0 if sparse_volume else 1.0)
        significant = signals["ma_spread_bps"].abs() >= noise_threshold

        # Price momentum confirmation for MA BUY: close must be above where it
        # was fast_period bars ago.  Prevents buying dead-cat bounces inside a
        # larger downtrend.  SELL crossover has no momentum requirement.
        price_momentum_up = close > close.shift(self.config.fast_period)

        # Long-term trend filter: BUY signals only when close > trend_ma.
        if self.config.trend_period > 0 and len(close) >= self.config.trend_period:
            trend_ma = close.rolling(self.config.trend_period).mean()
            in_uptrend = close > trend_ma
        else:
            in_uptrend = pd.Series(True, index=df.index)

        # MA-based direction (crossover + vol + noise filter + price momentum + trend)
        ma_buy = bullish & vol_confirmed & significant & price_momentum_up & in_uptrend
        ma_sell = bearish & vol_confirmed & significant

        # Bollinger Band BUY only (disabled for sparse-volume/FX or if bb_period=0).
        # BB SELL intentionally excluded — exits trend rides prematurely.
        if self.config.bb_period > 0:
            bb_mid = close.rolling(self.config.bb_period).mean()
            bb_std_s = close.rolling(self.config.bb_period).std()
            bb_upper = bb_mid + self.config.bb_std_dev * bb_std_s
            bb_lower = bb_mid - self.config.bb_std_dev * bb_std_s
            bb_valid_s = ~(bb_upper.isna() | bb_lower.isna())
        else:
            bb_valid_s = pd.Series(False, index=df.index)
            bb_lower = pd.Series(float('nan'), index=df.index)

        if sparse_volume:
            rsi_buy = pd.Series(False, index=df.index)
            rsi_sell = pd.Series(False, index=df.index)
            bb_buy = pd.Series(False, index=df.index)
        elif self.config.bb_period == 0:
            bb_buy = pd.Series(False, index=df.index)
        else:
            # BB BUY: price below lower band + uptrend + volume + long-term trend
            bb_buy = (close < bb_lower) & bb_valid_s & vol_confirmed & (fast_ma > slow_ma) & in_uptrend

        # RSI BUY is a standalone mean-reversion signal: MA uptrend NOT required.
        # Only the long-term trend filter (in_uptrend) gates BUY direction.
        # RSI SELL fires regardless of trend (take profit / exit).
        rsi_buy_filtered = rsi_buy & in_uptrend

        # Combined direction: RSI → BB → MA crossover (priority order, last wins)
        signals["direction"] = Direction.HOLD.value
        signals.loc[ma_buy, "direction"] = Direction.BUY.value
        signals.loc[ma_sell, "direction"] = Direction.SELL.value
        signals.loc[bb_buy, "direction"] = Direction.BUY.value
        signals.loc[rsi_buy_filtered, "direction"] = Direction.BUY.value
        signals.loc[rsi_sell, "direction"] = Direction.SELL.value

        # Score: base + RSI extremity component + MA spread component
        spread_component = (signals["ma_spread_bps"].abs() / 50.0).clip(0, 1) * 0.15
        rsi_buy_strength = ((self.config.rsi_oversold - signals["rsi"].clip(upper=self.config.rsi_oversold)) / self.config.rsi_oversold).clip(0, 1) * 0.30
        rsi_sell_strength = ((signals["rsi"].clip(lower=self.config.rsi_overbought) - self.config.rsi_overbought) / (100.0 - self.config.rsi_overbought)).clip(0, 1) * 0.30

        signals["score"] = 0.0
        buy_mask = signals["direction"] == Direction.BUY.value
        sell_mask = signals["direction"] == Direction.SELL.value
        signals.loc[buy_mask, "score"] = (0.55 + spread_component[buy_mask] + rsi_buy_strength[buy_mask]).clip(0.55, 1.0)
        signals.loc[sell_mask, "score"] = (0.55 + spread_component[sell_mask] + rsi_sell_strength[sell_mask]).clip(0.55, 1.0)

        # ── RSI score multiplier (vectorised) ─────────────────────────────────
        # BUY into oversold → boost 1.5×; BUY into overbought → suppress 0.3×.
        if self.config.rsi_filter:
            oversold_buy  = buy_mask & (signals["rsi"] < self.config.rsi_oversold)
            overbought_buy = buy_mask & (signals["rsi"] > self.config.rsi_overbought)
            signals.loc[oversold_buy,  "score"] = (signals.loc[oversold_buy,  "score"] * 1.5).clip(upper=1.0)
            signals.loc[overbought_buy, "score"] = signals.loc[overbought_buy, "score"] * 0.3

        # Drop rows before slow MA is warm
        signals = signals.iloc[self.config.slow_period - 1:]

        # ── Market regime filter (vectorised) ─────────────────────────────────
        # Applies bar-by-bar regime derived from regime_df (SPY OHLCV).
        # Falls back to the cached self._regime when regime_df is None.
        # BEAR:    BUY → HOLD  (score=0)
        # NEUTRAL: BUY score × 0.7; if score drops below 0.55 → HOLD
        # SELL always passes — take profit regardless of regime.
        signals["regime"] = self._regime  # default: cached (from update_regime)

        if self.config.regime_filter:
            if regime_df is not None and not regime_df.empty:
                # Compute bar-by-bar SPY ratio vs MA200
                spy_close = regime_df["close"]
                spy_ma = spy_close.rolling(
                    self.config.regime_ma_period,
                    min_periods=self.config.regime_ma_period,
                ).mean()
                valid = spy_ma > 0
                spy_ratio = (spy_close - spy_ma).where(valid) / spy_ma.where(valid)

                # Align to signals index (forward-fill gaps; NaN = insufficient history)
                ratio_s = spy_ratio.reindex(signals.index, method="ffill")
                neutral_pct = self.config.regime_neutral_pct
                has_data = ~ratio_s.isna()

                is_bear    = has_data & (ratio_s < -neutral_pct)
                is_neutral = has_data & (ratio_s >= -neutral_pct) & (ratio_s <= neutral_pct)

                # Label regime column
                signals.loc[is_bear,    "regime"] = "BEAR"
                signals.loc[is_neutral, "regime"] = "NEUTRAL"
                signals.loc[has_data & ~is_bear & ~is_neutral, "regime"] = "BULL"

                buy_mask = signals["direction"] == Direction.BUY.value

                # BEAR: suppress all BUY signals
                bear_buy = buy_mask & is_bear
                signals.loc[bear_buy, "direction"] = Direction.HOLD.value
                signals.loc[bear_buy, "score"] = 0.0

                # NEUTRAL: reduce BUY conviction
                buy_after_bear = signals["direction"] == Direction.BUY.value
                neutral_buy = buy_after_bear & is_neutral
                signals.loc[neutral_buy, "score"] = (
                    signals.loc[neutral_buy, "score"] * 0.7
                ).round(4)
                # Convert to HOLD if score fell below risk engine minimum
                low_neutral = neutral_buy & (signals["score"] < 0.55)
                signals.loc[low_neutral, "direction"] = Direction.HOLD.value
                signals.loc[low_neutral, "score"] = 0.0

            else:
                # No bar-by-bar data — fall back to cached regime for all bars
                if self._regime == "BEAR":
                    bear_mask = signals["direction"] == Direction.BUY.value
                    signals.loc[bear_mask, "direction"] = Direction.HOLD.value
                    signals.loc[bear_mask, "score"] = 0.0
                elif self._regime == "NEUTRAL":
                    buy_mask = signals["direction"] == Direction.BUY.value
                    signals.loc[buy_mask, "score"] = (
                        signals.loc[buy_mask, "score"] * 0.7
                    ).round(4)
                    low = buy_mask & (signals["score"] < 0.55)
                    signals.loc[low, "direction"] = Direction.HOLD.value
                    signals.loc[low, "score"] = 0.0

        return signals

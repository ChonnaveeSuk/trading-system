# trading-system/strategy/src/signals/momentum.py
#
# Dual Moving Average crossover + RSI mean-reversion strategy.
#
# Signal sources (any can generate a trade; highest-priority fires):
#   1. RSI mean-reversion: RSI(RSI_PERIOD) enters oversold/overbought zone
#      - RSI < rsi_oversold (35) → BUY  (buy the dip, requires MA uptrend)
#      - RSI > rsi_overbought (65) → SELL  (take profit)
#      - No volume requirement; disabled for FX (sparse volume)
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
# Parameters: 8 (fast_period, slow_period, vol_period, noise_filter_bps,
#               rsi_oversold, rsi_overbought, bb_period, bb_std_dev)
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

# RSI period — fixed (Wilder's standard 7-period for daily charts)
RSI_PERIOD = 7


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
    bb_period: int = 0              # Bollinger Band period (0 = disabled; BB SELL removed as it cuts trend profits)
    bb_std_dev: float = 2.0         # Bollinger Band std dev multiplier
    trend_period: int = 0           # Long-term trend filter period (0 = disabled)


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
    """Dual MA crossover with volume confirmation.

    Usage::

        strategy = MomentumStrategy()
        df = fetcher.fetch("AAPL", days=30)
        result = strategy.generate_signal("AAPL", df, portfolio_value=100_000)
        # result: SignalResult with direction, score, stop_loss, features
    """

    def __init__(self, config: MomentumConfig = MomentumConfig()) -> None:
        self.config = config

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

        # ── RSI (mean-reversion signal source) ───────────────────────────────
        rsi_series = _compute_rsi(close, RSI_PERIOD)
        _rsi_raw = float(rsi_series.iloc[-1])
        if np.isnan(_rsi_raw):
            logger.debug(
                "%s: RSI NaN (insufficient history for period=%d) — using neutral 50.0",
                symbol, RSI_PERIOD,
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

        # Combined direction: RSI → BB → MA crossover (priority order)
        # All BUY signals require uptrend confirmation.
        if rsi_buy and curr_fast > curr_slow and in_uptrend:
            direction = Direction.BUY
        elif rsi_sell:
            direction = Direction.SELL  # take profit regardless of trend
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

        # ── Position sizing ───────────────────────────────────────────────────
        # Allocate position_pct of portfolio, rounded to instrument precision
        notional = portfolio_value * position_pct
        if curr_price > 0 and direction != Direction.HOLD:
            raw_qty = notional / curr_price
            # Instrument-appropriate rounding
            if symbol.endswith("-USD") and curr_price > 1000:
                # Crypto: 4 decimal places
                quantity = Decimal(str(round(raw_qty, 4)))
            elif curr_price < 10:
                # FX / cheap instruments: whole units
                quantity = Decimal(str(round(raw_qty, 0)))
            else:
                # Equities: whole shares
                quantity = Decimal(str(int(raw_qty)))
        else:
            quantity = None

        # ── Stop loss ─────────────────────────────────────────────────────────
        # BUY: stop at slow MA (structure support)
        # SELL: stop at slow MA (structure resistance)
        if direction == Direction.BUY:
            # 1% buffer below slow MA as stop
            stop_price = curr_slow * 0.99
            stop_loss = Decimal(str(round(stop_price, 5)))
        elif direction == Direction.SELL:
            # 1% buffer above slow MA as stop
            stop_price = curr_slow * 1.01
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
            "rsi_period": RSI_PERIOD,
            "bb_upper": round(bb_upper_val, 5) if bb_valid else None,
            "bb_lower": round(bb_lower_val, 5) if bb_valid else None,
            "bars_available": bars_available,
            "production_ready": production_ready,
            "volume_confirmed": volume_confirmed,
            "noise_filter_bps": self.config.noise_filter_bps,
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

        # RSI mean-reversion signal source
        rsi = _compute_rsi(close, RSI_PERIOD)
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

        # RSI BUY requires uptrend confirmation + long-term trend filter.
        # RSI SELL fires regardless (take profit / exit).
        rsi_buy_filtered = rsi_buy & (fast_ma > slow_ma) & in_uptrend

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

        # Drop rows before slow MA is warm
        signals = signals.iloc[self.config.slow_period - 1:]
        return signals

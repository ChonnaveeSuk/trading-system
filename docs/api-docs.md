# QuantAI API Documentation

This document outlines the public interfaces for the core Strategy and Execution layers of the QuantAI Trading System.

---

## Module: `strategy.src.signals.momentum`

### Class: `MomentumStrategy`
The primary mathematical engine generating Dual MA Crossover and Mean-Reversion signals.

#### `__init__(self, config: MomentumConfig = MomentumConfig()) -> None`
Initializes the strategy with the provided configuration parameters.
- **Parameters:**
  - `config` (*MomentumConfig*, default=`MomentumConfig()`): A dataclass containing tuning parameters (MA lengths, RSI thresholds, etc.).
- **Returns:** `None`

#### `update_regime(self, spy_df: pd.DataFrame) -> str`
Computes and caches the current broad market regime based on a proxy symbol (usually SPY).
- **Parameters:**
  - `spy_df` (*pd.DataFrame*): Historical OHLCV data for the proxy symbol. Must contain a `close` column and index of timestamps. Minimum length should exceed `regime_ma_period`.
- **Returns:** (*str*) One of `"BULL"`, `"NEUTRAL"`, or `"BEAR"`.
- **Notes:** If data is extremely stale (>30 days), it logs a warning and defaults to `"BULL"` to prevent locking the system.

#### `update_vix(self, vixy_df: pd.DataFrame) -> str`
Computes and caches the volatility state from a VIX proxy (usually VIXY).
- **Parameters:**
  - `vixy_df` (*pd.DataFrame*): Historical OHLCV data for the VIX proxy.
- **Returns:** (*str*) One of `"CALM"`, `"CAUTION"`, or `"PANIC"`.
- **Notes:** CAUTION halves position sizes. PANIC blocks all `BUY` signals.

#### `generate_signal(self, symbol: str, df: pd.DataFrame, portfolio_value: float = 100_000.0, position_pct: float = 0.02, as_of_date: Optional[date] = None) -> SignalResult`
Evaluates OHLCV bars to produce a highly contextual trading signal.
- **Parameters:**
  - `symbol` (*str*): The ticker symbol to evaluate.
  - `df` (*pd.DataFrame*): OHLCV history for the symbol.
  - `portfolio_value` (*float*, default=`100000.0`): The current account equity used for ATR position sizing.
  - `position_pct` (*float*, default=`0.02`): Fallback sizing percentage if ATR fails.
  - `as_of_date` (*Optional[date]*, default=`None`): Override date for calendar blackout checks. If `None`, uses the timestamp of the latest bar.
- **Returns:** (*SignalResult*) A dataclass containing the `direction` (BUY/SELL/HOLD), `score` (0.0 to 1.0), `suggested_quantity`, `suggested_stop_loss`, and `features` metadata.
- **Raises:**
  - `KeyError`: If `df` is missing required columns (`close`, `volume`, `high`, `low`).
- **Example Usage:**
  ```python
  strategy = MomentumStrategy(config)
  strategy.update_regime(spy_df)
  result = strategy.generate_signal("AAPL", aapl_df, portfolio_value=105000)
  if result.direction == Direction.BUY:
      print(f"Buy {result.suggested_quantity} shares!")
  ```

---

## Module: `strategy.src.bridge.alpaca_direct`

### Class: `AlpacaDirectClient`
A REST client facilitating direct interaction with the Alpaca Paper API, circumventing the Rust gRPC architecture for serverless execution.

#### `__init__(self) -> None`
Initializes empty session state. Credentials are not loaded until `connect()` is called.

#### `connect(self) -> None`
Authenticates against Alpaca using environment variables or Google Secret Manager.
- **Parameters:** None
- **Returns:** `None`
- **Raises:**
  - `RuntimeError`: If `ALPACA_API_KEY` or `ALPACA_SECRET_KEY` cannot be resolved.
  - `RuntimeError`: If a live trading endpoint is detected without Phase 4 authorization.

#### `disconnect(self) -> None`
Closes the active `requests.Session`.
- **Parameters:** None
- **Returns:** `None`

#### `health_check(self) -> HealthStatus`
Pings the Alpaca account endpoint to verify connectivity and retrieve equity balances.
- **Parameters:** None
- **Returns:** (*HealthStatus*) A dataclass containing boolean `healthy` state, active `portfolio_value`, and `open_orders` count.
- **Raises:**
  - `requests.HTTPError`: If the Alpaca API rejects the token or is down.

#### `check_and_trigger_stops(self, stop_loss_pct: float = 0.05, warn_pct: float = 0.03, telegram_alert: Optional[Callable] = None) -> list[StopLossResult]`
Evaluates all current Alpaca positions. If unrealized loss exceeds the threshold, liquidates the position instantly via a MARKET order.
- **Parameters:**
  - `stop_loss_pct` (*float*, default=`0.05`): The negative P&L fraction trigger (-5%).
  - `warn_pct` (*float*, default=`0.03`): The warning threshold (-3%).
  - `telegram_alert` (*Callable*, default=`None`): A function to execute upon stop loss trigger for notification routing.
- **Returns:** (*list[StopLossResult]*) An array containing the result of every checked position.
- **Notes:** Non-fatal function. If an API call to liquidate one asset fails, the loop catches the exception and continues evaluating the rest of the portfolio.

#### `submit_signal(self, signal: SignalResult, current_price: float, quantity_override: Optional[Decimal] = None) -> Optional[BridgeResponse]`
Validates and routes a `SignalResult` to the Alpaca execution queue.
- **Parameters:**
  - `signal` (*SignalResult*): The generated strategy signal.
  - `current_price` (*float*): The current execution price for calculating notional limits.
  - `quantity_override` (*Optional[Decimal]*, default=`None`): Overrides the strategy's suggested sizing if provided.
- **Returns:** (*Optional[BridgeResponse]*) A response object containing the broker execution ID and acceptance status. Returns `None` if the signal was `HOLD` or the symbol is completely unsupported (e.g., FX).
- **Raises:** None. Catches and logs all `HTTPError` exceptions, returning an `ERROR` status in the response object.
- **Example Usage:**
  ```python
  with AlpacaDirectClient() as client:
      health = client.health_check()
      if health.healthy:
          client.submit_signal(signal, current_price=150.00)
  ```

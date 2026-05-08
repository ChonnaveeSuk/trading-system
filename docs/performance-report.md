# QuantAI: Performance Profiling Report

## 1. Time & Memory Complexity Analysis

### `MomentumStrategy.generate_signal`
- **Time Complexity:** $O(N)$ where $N$ is the number of bars (90 days). The rolling means (`close.rolling(15).mean()`), standard deviations, and max logic are computed using highly optimized C-backed NumPy/Pandas operations.
- **Memory Complexity:** $O(N)$. The system duplicates small Pandas series (e.g., `fast_ma`, `slow_ma`, `bb_upper`, `rsi_series`). Since $N \le 90$, this consumes barely a few kilobytes of RAM per symbol. 

### `AlpacaDirectClient.check_and_trigger_stops`
- **Time Complexity:** $O(P + E \times HTTP)$ where $P$ is the number of open positions and $E$ is the number of breached stops requiring an HTTP `DELETE` call. The loop is strictly limited to `_MAX_OPEN_POSITIONS` (10).
- **Memory Complexity:** $O(P)$. Holds a lightweight JSON dictionary of Alpaca positions.

### `run_live` (The Daily Orchestrator)
- **Time Complexity:** $O(S \times (DB_{fetch} + HTTP_{submit}))$ where $S$ is the number of symbols in the universe (16). 
- **Memory Complexity:** $O(S \times N)$. Holding 16 DataFrames of 90 rows is negligible.

## 2. Bottlenecks in the Daily Runner

The architecture of `run_strategy.py` processes symbols synchronously. 
For 16 symbols, the loop:
1. Opens a Postgres transaction.
2. Fetches 90 rows via psycopg2.
3. Computes the math natively in Python.
4. Executes an Alpaca API call if a signal fires.
5. Sleeps for `_API_SLEEP_S` (0.3 seconds) to respect rate limits.

While the math execution takes $<5$ milliseconds, the network I/O (Database fetch + Alpaca API) consumes 99% of the execution time. If the universe scales to 1,000 symbols, the synchronous DB fetches and 0.3s sleep delays will cause the script to run for over 10 minutes, potentially crashing into Cloud Run timeouts or causing late executions for market opens.

## 3. Database Query Optimization (N+1 Queries)
Currently, `fetcher.fetch(symbol, days=90)` executes an individual `SELECT` query for each symbol inside the loop. This is an N+1 query problem.
- **Current State:** 16 symbols = 16 network round trips to Cloud SQL.
- **Optimization:** Execute a single batch query for all symbols and pivot the results in Pandas. 

## 4. Pandas Operations Optimization
The strategy computes rolling moving averages and RSI for the *entire* 90-day dataset on every single daily run, only to discard everything except `.iloc[-1]` (the current day) and `.iloc[-2]` (yesterday).
- **Current State:** Computing `RSI(7)` across 90 days.
- **Optimization:** If state was tracked between executions, the system would only need to ingest the single new daily bar and update the exponential moving average incrementally. However, for a serverless (stateless) architecture, the 90-day recalculation is necessary. Replacing Pandas rolling functions with Numba-JIT compiled numpy arrays would speed this up by 10x, though the absolute time saved is minimal (milliseconds).

## 5. API Call Optimization (Batching)
During `check_and_trigger_stops()`, if 5 positions breach their stop-losses, the system executes 5 sequential `DELETE /positions/{symbol}` REST calls.
- **Optimization:** Alpaca does not offer a bulk "Delete Specific Positions" endpoint, but it *does* offer concurrent API access. By utilizing `aiohttp` or `asyncio` combined with `httpx`, the script can submit multiple close orders simultaneously, reducing a 2-second liquidation loop into 200ms.

## 6. Profiling Recommendations
Before implementing any optimization, empirical data must be gathered.
- **`cProfile`:** Run `python -m cProfile -s cumtime run_strategy.py --mode backtest` to identify CPU-bound bottlenecks (e.g., Pandas overhead).
- **`py-spy`:** Run `py-spy record -o profile.svg -- pid <job_id>` in the production container to generate a flame graph of network I/O delays.

## 7. When Optimization Matters vs. Premature Optimization
Currently, the `quantai-daily-runner` completes its task in under 15 seconds and costs $0.00 in compute. **Do not optimize this yet.**
Optimizing Pandas vectorization or deploying async I/O is a severe case of premature optimization that will make the code harder to read and debug for zero tangible benefit. 

Optimization only becomes necessary when:
1. The universe expands past 100 symbols (N+1 DB fetches become a liability).
2. The strategy shifts to intraday (minute/tick data) where computational latency eats into the Alpha.

## 8. Code Snippets for Top Optimizations

### Optimization 1: Batch Database Fetching (Fixing N+1)
```python
# BEFORE (Inside symbol loop)
df = fetcher.fetch(symbol, days=90) # 16 queries

# AFTER (Before symbol loop)
def fetch_all_symbols(symbols, days=90):
    query = """
        SELECT symbol, timestamp, open, high, low, close, volume 
        FROM ohlcv 
        WHERE symbol = ANY(%s) 
        AND timestamp >= NOW() - INTERVAL '%s days'
    """
    cur.execute(query, (symbols, days))
    df = pd.DataFrame(cur.fetchall(), columns=["symbol", "timestamp", ...])
    # Group by symbol in memory
    return dict(tuple(df.groupby('symbol')))

all_data = fetch_all_symbols(symbols)
for symbol in symbols:
    df = all_data.get(symbol, pd.DataFrame())
```

### Optimization 2: Single Connection Passing
```python
# BEFORE (Opening connections inside helper functions)
def _check_max_drawdown_alert():
    conn = psycopg2.connect(db_url)
    # ...

# AFTER (Dependency Injection)
def _check_max_drawdown_alert(conn):
    with conn.cursor() as cur:
        # ...

with psycopg2.connect(db_url) as master_conn:
    _check_max_drawdown_alert(master_conn)
    _check_and_record_regime_change(..., master_conn)
```

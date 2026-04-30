# Looker Studio Dashboard — Setup Runbook

Looker Studio dashboards must be created interactively in the browser; there
is no Terraform / `gcloud` API to provision them programmatically.  This
runbook captures the data source binding and the recommended chart layout so
the dashboard can be rebuilt deterministically.

## 1. Connect a BigQuery data source

1. Open <https://lookerstudio.google.com/datasources> while signed in as the
   `chonnaveesukyao@gmail.com` GCP-billing account.
2. **Create** → **BigQuery connector** → **My projects** → `quantai-trading-paper`.
3. Add three data sources, one per table.  Use the **Custom query** tab when
   noted so daily aggregates render fast.

| Data source name | Project | Dataset | Table / query |
|---|---|---|---|
| `quantai_trades`  | `quantai-trading-paper` | `quantai_trading` | table `trades` |
| `quantai_signals` | `quantai-trading-paper` | `quantai_trading` | table `signals` |
| `quantai_daily_pnl` | `quantai-trading-paper` | `quantai_trading` | custom query (below) |

Custom query for `quantai_daily_pnl` (BigQuery copy of the Postgres
`daily_pnl` table — note: requires a scheduled BQ load, not in scope of this
ticket; if the table is empty, point the data source at `trades` and aggregate
in Looker instead):

```sql
SELECT
  DATE(timestamp)                      AS trading_date,
  COUNTIF(side = 'BUY')                AS buy_count,
  COUNTIF(side = 'SELL')               AS sell_count,
  SUM(IF(side = 'SELL', realized_pnl, 0)) AS realized_pnl,
  COUNT(*)                             AS trade_count
FROM `quantai-trading-paper.quantai_trading.trades`
GROUP BY trading_date
ORDER BY trading_date
```

## 2. Recommended dashboard layout

Create a new report at <https://lookerstudio.google.com/reporting/create> and
add the three data sources.  Suggested page layout (single-page report):

| Row | Component | Data source | Configuration |
|---|---|---|---|
| 1 | Scorecard — Cumulative realized P&L | `quantai_trades` | metric: `SUM(realized_pnl)` |
| 1 | Scorecard — Trades today | `quantai_trades` | filter: `DATE(timestamp) = TODAY()` |
| 1 | Scorecard — Open positions | `quantai_signals` | metric: distinct symbols where last direction = BUY without subsequent SELL |
| 2 | Time series — Daily P&L | `quantai_daily_pnl` | dimension `trading_date`, metric `realized_pnl` |
| 2 | Time series — Daily trade count | `quantai_daily_pnl` | dimension `trading_date`, metric `trade_count` |
| 3 | Bar chart — Realized P&L by symbol | `quantai_trades` | dimension `symbol`, metric `SUM(realized_pnl)`, sort desc |
| 3 | Bar chart — Trades by signal_type | `quantai_trades` | dimension `signal_type`, metric `COUNT(*)` |
| 4 | Table — Last 50 trades | `quantai_trades` | dimensions: `timestamp, symbol, side, quantity, price, realized_pnl` (sort by `timestamp desc`, limit 50) |
| 5 | Time series — Signals over time (BUY/SELL stacked) | `quantai_signals` | dimension `DATE(timestamp)`, breakdown `direction`, metric `COUNT(*)` |

## 3. Sharing + URL

After saving the report:

1. **Share** → set link sharing to *Restricted* (no link sharing).
2. Add `chonnaveesukyao@gmail.com` (and any operator emails) as **Viewer**.
3. Copy the report URL — it has the form:

   `https://lookerstudio.google.com/reporting/<REPORT_ID>`

4. Paste the URL into `CLAUDE.md` under a new "Dashboards" section so future
   sessions can find it without re-doing this runbook.

## 4. Refresh cadence

BigQuery data sources cache for 12 hours by default.  Since the trading
system writes trades via Pub/Sub → BigQuery in near real-time, lower the
refresh cadence on each component:

- **File** → **Report settings** → **Data freshness** → 1 hour for the
  `quantai_trades` and `quantai_signals` sources.

## 5. Cost note

Looker Studio itself is free.  BigQuery storage + query cost stays under
the 1 TB/month free-tier quota for this volume of data (∼1 MB / day).

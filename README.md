# massive

Collects stock market data at the finest resolution your plan allows, keeps it
in a local database, and analyses and charts it. Massive (massive.com, formerly
Polygon.io) is the first data source; others plug in behind the same interface.

## What it collects

For every ticker on the watchlist it probes your plan and uses the finest
resolution available, as far back as the plan allows:

1. **trades** — every tick, nanosecond timestamps, with the exchange it printed on
2. **second** — 1-second bars
3. **minute** — 1-minute bars (available on the free plan)

Runs fill newest-first and save page by page, so a run that stops part-way loses
nothing and the next run continues where it left off.

## Staying inside your plan

- A rate limiter keeps requests under the plan's per-minute limit (free: 5/min).
- Hard caps per run and per day (default 250 / 2000) are checked before every request.
- History is never requested beyond the plan's window (free: 2 years).
- A refusal (403) or persistent rate limiting (429) stops the run instead of retrying.
- A lock file stops two runs overlapping and doubling the request rate.
- Every run is logged with the number of requests it used (`status`).

## Setup

```bash
python -m pip install -e ".[dev]"
```

Set your key and plan as user environment variables (Windows PowerShell):

```powershell
[Environment]::SetEnvironmentVariable("MASSIVE_API_KEY", "<your key>", "User")
[Environment]::SetEnvironmentVariable("MASSIVE_TIER", "free", "User")
```

Data is stored in `~/MassiveData/massive.db` (override with `MASSIVE_DB`), with
logs, charts and backups beside it. Nothing is stored in the repository.

## Usage

```bash
python -m massive_collate probe SPCX --watch   # find the finest resolution, add to watchlist
python -m massive_collate sync                 # update + backfill the watchlist
python -m massive_collate schedule install     # run sync every hour (Windows Task Scheduler)
python -m massive_collate status               # what's stored, limits, recent runs
python -m massive_collate exchanges --refresh  # exchange list
python -m massive_collate analyze SPCX         # statistics + per-exchange breakdown
python -m massive_collate chart SPCX --rule 1s --sma 20 50 --ticks
python -m massive_collate export SPCX --kind trades --out spcx.csv
python -m massive_collate admin check|vacuum|backup
python -m massive_collate gui                  # chart app in your browser (localhost only)
```

## Chart app

`python -m massive_collate gui` serves a TradingView-style chart on http://127.0.0.1:8765 using
[KLineChart](https://github.com/klinecharts/KLineChart) 10.1.0 (Apache-2.0, bundled, works offline).

- Intervals from 1s (when ticks or second bars are stored) to monthly, built exactly from the finest
  stored data; day/week/month use the official daily bars (closing auction included).
- Regular hours (09:30–16:00 ET) or extended hours (ETH).
- Feeds: each ticker can be viewed per feed or merged; overlapping slots come from the
  highest-priority feed only (`SOURCE_PRIORITY`), so nothing is double counted.
- Drawing tools: trend line, ray, extended/horizontal/vertical lines, price line, parallel and price
  channels, Fibonacci, measure (price change, %, bars, time), notes; magnet mode; lock/hide/delete.
  Drawings are saved per ticker in the browser.
- Keys: type to search symbols, `+`/`-` zoom, `End` latest bar, `Esc` cancel, `Delete` removes the
  selected drawing.

## Layout

| Module | Purpose |
| --- | --- |
| `sources/base.py` | `DataSource` interface every provider implements |
| `sources/massive.py` | Massive client (auth, rate limit, cap, retries, pagination) and source |
| `models.py` | `Instrument` (`Stock`, `ETF`) → `Exchange` → `Country` |
| `storage.py` | SQLite: ticks, bars, exchanges, watchlist, run log |
| `collector.py` | Sync logic: resolution probing, forward update, backfill |
| `analysis.py` | Tick→bar aggregation, per-exchange stats, returns, indicators, risk |
| `charts.py` | Plotly HTML charts |
| `cli.py` | Command line |

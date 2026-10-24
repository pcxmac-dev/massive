# massive

Collects stock market data at the finest resolution your plan allows, keeps it
in a local database, and analyses and charts it. Two feeds plug in behind the same interface:
Massive (massive.com, formerly Polygon.io) and Alpaca (alpaca.markets: US stocks from 2016 with
keys, crypto from Kraken's book without).

## Assets on several exchanges: reference, composite, quality

Exchanges differ by a little, and a consolidated tape's extremes are set by whichever exchange
printed furthest out (Massive's Bitcoin highs are Bitfinex's premium prints). So an asset traded on
several exchanges has, beside each exchange's own data:

- a reference model: one exchange, the gold standard (Coinbase for crypto; for a US stock its
  primary listing exchange, from exchange-tagged ticks once they are stored);
- a composite model: per bar, every exchange's bar weighted by volume x quality, with the deviation
  across exchanges (standard deviation of the closes, spread, envelope of highs and lows), drawn as
  a band on the chart;
- quality scores (0-1) for every source: measured for exchanges (each against the median of the
  others: distance, outlier share, completeness), a prior by provenance otherwise. A low score lowers
  a source's weight in the composite and its rank for display and reporting.

Crypto is the case study (four exchanges, recent 90 days, `CRYPTO_VENUE_DAYS`); the same models
apply to stocks once exchange-tagged consolidated ticks are collected (Alpaca keys).

## Several feeds, one download per span

Each span of a symbol's series is downloaded by exactly one feed of a market (two consolidated
tapes are copies; two exchanges are not, and the composite needs both). A ledger (`coverage` table)
records which feed holds which span; before any request a feed drops what any feed already holds
and what a better-ranked feed can serve now. Ranking: resolution first (ticks, seconds, minutes),
then accuracy measured in audits, then `SOURCE_PRIORITY`. The app shows where each bar came from
(cursor box "Src", the Data panel's Sources).

**Audits** are the only second downloads: another feed's copy of a stored span is compared bar
by bar (timing to the second, OHLC, high-low range, volume, VWAP, trade count, as box stats with a
small margin of error) and only the comparison is kept. They run after new data arrives and at
random (`AUDITS_PER_RUN`, `AUDITS_RANDOM_PER_RUN`), or with `python -m massive_collate audit
[TICKER] [--day D] [--list]`; results are in the Fetcher view.

## What it collects

For every ticker on the watchlist it probes your plan and uses the finest
resolution available, as far back as the plan allows:

1. **trades** — every tick, nanosecond timestamps, with the exchange it printed on: going forward
   always, backfilled one year (`TICK_HISTORY_DAYS`); one-second and minute bars are built from
   them here with the tape's condition rules, and seconds charts read them by window
2. **second** — 1-second bars
3. **minute** — 1-minute bars (available on the free plan), for all history before the tick window

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
- Feeds: each ticker can be viewed per feed or merged; each span comes from one feed only (see
  above), and the cursor box shows the source of the bar under the cursor.
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
| `sources/alpaca.py` | Alpaca source: stocks (SIP, keys) and crypto (Kraken, no key), same caps and retries |
| `coverage.py` | One feed per span: what a feed may download, given the ledger and the ranking |
| `audit.py` | Feed-against-feed audits: comparison, box stats, verdicts |
| `composite.py` | Composite and reference models across exchanges; quality scores |
| `models.py` | `Instrument` (`Stock`, `ETF`) → `Exchange` → `Country` |
| `storage.py` | SQLite: ticks, bars, exchanges, watchlist, run log |
| `collector.py` | Sync logic: resolution probing, gap filling (newest first), shared listing checks |
| `analysis.py` | Tick→bar aggregation, per-exchange stats, returns, indicators, risk |
| `charts.py` | Plotly HTML charts |
| `cli.py` | Command line |

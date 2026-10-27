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

Save each API key with `tools/set-key.ps1`: it asks for the key with the input hidden (not shown,
not kept in the shell's history, not written to a file) and saves it as a user environment variable,
where the app reads keys. Without a name it shows a menu of the data providers (each a plugin of the
app, so a new provider appears by itself), their keys and which are set: pick one to set, replace or
remove its keys. With a provider's name (`tiingo`) or one key's variable (`TIINGO_API_KEY`) it goes
straight there; `-Remove` deletes, `-List` only shows. Restart running processes (the sync, the
chart app) after setting a key. `python -m massive_collate plugins` lists the same providers.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\set-key.ps1 tiingo
```

Set your plan the same way, as a user environment variable (not a secret):

```powershell
[Environment]::SetEnvironmentVariable("MASSIVE_TIER", "free", "User")
```

Data is stored in `~/MassiveData` (override the main file with `MASSIVE_DB`), with
logs, charts and backups beside it. Nothing is stored in the repository. Each feed has files of
its own: `feeds/<feed>.db` holds its bars (the composite model's file also its deviation stats)
and `feeds/<feed>-ticks.db` its trades. The main file `massive.db` keeps what the feeds share: the
watchlist, the coverage ledger, audits, quality scores, the directory and the chart app's layouts.
Nothing is overwritten silently: a corrected bar keeps its replaced version (with when it was stored
and replaced) and every dataset value is kept in every published version, so a series can be read
as it stood at any moment (`as_of`), for backtests that see only what was known then. Results
computed here (bars built from ticks, the composite model, quality scores, audits) are kept apart
from what the sources published and record how they were made: inputs, code version (git commit)
and settings (`python -m massive_collate provenance`).
Every standard frame (1 s to 1 year) is kept for every series: the ones a feed sends as it sent
them, the others built here, upward only, from that feed's finer bars and marked derived (a gap
in the finer data stays a gap). The sync builds a symbol's frames as soon as its fetch is stored.
`python -m massive_collate inventory TICKER` (and "Data by feed" in the chart's side panel) shows,
feed by feed and frame by frame, what was sent, what was derived and from what, and what is not
built yet.
A database from before this layout is moved once with `admin split-feeds` (it backs up first,
checks every copy against its original, and only then removes the old copies); until then the
app refuses to open it.

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
python -m massive_collate datasets [list|fetch [NAME...]|show NAME [SERIES]]  # research datasets
python -m massive_collate admin check|vacuum|backup [--ticks]|split-feeds
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
- Themes (top right): Dark, Midnight, Graphite, Light and Paper; the chart's grid, axes and crosshair
  follow each one.
- Stock and Analysis views (right rail) show the same symbol, each with its own chart content:
  interval, chart type, model, indicators and their pane heights, zoom and position, drawings.
  Switching between them keeps both; saved layouts keep both.
- Datasets view (right rail): the research datasets as lines on one time axis, up to eight series,
  one chart per unit. Fama/French returns compounded into the growth of $1 (log scale), any series
  indexed to 100, Shiller CAPE, COT positioning per market (net long - short per trader group, in
  contracts or as a share of open interest); ranges 1Y to All, hover or arrow keys to read every
  series at a date, and a table of the latest values. Click a legend entry to hide or show it (for
  COT a trader group: speculators, hedgers, small traders, in every view and market). Zoom with the
  wheel or + / −, drag to pan, double-click or Reset (0) for the whole range. Markup: trend lines,
  levels, date lines, date zones and notes, kept in the browser per dataset (per market for COT);
  date lines and zones show in every view, lines, levels and notes in the view they were drawn in.
  Click a mark to select it, Delete removes it.

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

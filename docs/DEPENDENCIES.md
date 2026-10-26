# Dependency register

Everything the app relies on that it does not own: where it comes from, how and how often it is
fetched, what it costs, how it goes stale, and what is still missing. Keep this current when a
dependency is added or changes.

**Health check:** `python -m massive_collate deps` checks every item below against its live state
(last success, age, coverage) and prints OK / WARN / FAIL with the action to take. The Fetcher view
shows the same checks.

Status: ✅ handled · ⚠️ partly handled (see notes) · ❌ gap (not handled yet)

---

## 1. Massive REST API (api.massive.com)

Plan: free (5 requests/min, 2 years of history, end-of-day stocks/indices, currencies and crypto through the previous UTC day, futures 8 h delayed).
Hard caps: 250 requests per run, 2,000 per day (`MASSIVE_MAX_REQUESTS_PER_RUN/DAY`). Every request,
retries included, counts. The rate windows and the daily caps hold per key on this machine
(`ratelimit.MachineLedger`, beside the live recorder's machine lock), whichever database a process
uses: a second copy of the app cannot double them.

| Data | Endpoint | Fetched by | When | Cost | Status |
|---|---|---|---|---|---|
| Minute/day bars (stocks, indices, FX `C:`, crypto `X:`) | `/v2/aggs/ticker/{T}/range/...` | `collector.sync_bars`, `sync_daily` | each sync, only when the plan should have published a new session (`freshness`) | 1+ per symbol per new session; backfill ~12 per stock | ✅ |
| Crypto bars | same aggregates endpoint, `X:BTCUSD` (consolidated across the exchanges Massive covers; per-exchange data needs trades, a paid plan) | `collector.sync_bars` | each sync once the UTC day is published (free plan: minute, not second; probed 2026-10-23) | ~21 requests to backfill 2 years, then 1-2 a day | ✅ |
| Trades (ticks) | `/v3/trades/{T}` | `collector.sync_trades` | not on the free plan (probed once a week) | — | ✅ |
| Futures bars | `/futures/v1/aggs/{contract}` | `collector.sync_future` | each sync (8 h delayed data moves continuously) | 1–2 per product per run | ✅ |
| Plan entitlements | probes of the above, limit 1 | `resolve_resolution` | once per asset class per week (`entitlements` table) | 1 per probe | ✅ |
| Ticker reference (listing date, FIGI, name, branding links, market cap) | `/v3/reference/tickers/{T}` | `check_listing` (watched), `fill_directory_icons` (directory) | weekly for watched; once, then every 90 days for directory companies | 1 | ✅ |
| **Company logos** | branding image URL from the reference data (`/v1/reference/company-branding/...`) | `fetch_icon` | watched stocks: after their backfill; directory: biggest first, leftover budget; again whenever the logo link changes (rebrand) | 1 per logo | ✅ |
| Exchanges (venues, MICs) | `/v3/reference/exchanges` | `sync_exchanges` | each sync (cached) | 1 | ✅ |
| **Directory of all Nasdaq/NYSE listings** | `/v3/reference/tickers?exchange=XNAS|XNYS&active=true` | `refresh_directory` | weekly, leftover budget | ~10 | ✅ |
| Futures products | `/futures/v1/products` | `refresh_directory` | weekly | 1+ | ⚠️ schema assumed (product_code/name/trading_venue); verify on first run |
| **Upcoming US market holidays** | `/v1/marketstatus/upcoming` | `refresh_calendars` | weekly | 1 | ✅ overrides the rule calendar |
| **CME session schedules** | `/futures/v1/schedules` (60 days ahead) | `refresh_calendars` | weekly, per watched product | 1+ per product | ✅ overrides the CME rules |
| **News headlines** | `/v2/reference/news` | `fetch_news` | each sync, at most every 30 min | 1 | ✅ US-centric (see gaps) |
| **Whole-market daily file** (every US ticker's day bar) | `/v2/aggs/grouped/locale/us/market/stocks/{day}` | `fetch_market_moves` | once per trading day, after it is published (overnight on the free plan; "before end of day" 403 = not out yet) | 1 per day | ✅ feeds the globe colours and the directory ranking |
| Newest bar (scan probe) | aggs with limit 1, newest first | `collector.scan` | on demand (Scan button, opening a symbol) | 1 per behind symbol | ✅ |

## 1b. Alpaca market data (data.alpaca.markets) — second feed

Plan: Basic (free). Stocks: every US exchange (SIP) from 2016, minute bars and ticks, all but the
latest 15 minutes; 200 requests/min (we use 190). Crypto: no key needed; `us-1` is Kraken's book
(about 2,400 BTC/day against ~4 on Alpaca's own venue), minute bars from 2021. No seconds, indices,
currencies or futures. Caps: 3,000 requests per run, 30,000 per day (`ALPACA_MAX_REQUESTS_PER_RUN/DAY`).
Keys only in the environment (`ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`); without them stock
requests are refused before anything is sent.

| Data | Endpoint | Fetched by | When | Cost | Status |
|---|---|---|---|---|---|
| Stock minute/day bars (split-adjusted, like Massive) | `/v2/stocks/bars?feed=sip&adjustment=split` | `sync_bars` | spans no better-ranked feed serves: history before Massive's 2 years, and the current session up to 15 min ago | 10,000 bars per request | ⚠️ waiting for keys |
| Stock ticks | `/v2/stocks/trades` | `sync_trades` | every watched stock: new ticks each sync, backfilled `TICK_HISTORY_DAYS` (365); minute bars before that | 10,000 trades per request (~2,200 a day for 101 stocks) | ⚠️ waiting for keys |
| Crypto ticks (taker side kept) | `/v1beta3/crypto/us-1/trades` | `sync_trades` | as stocks; Kraken history from 2021 | 10,000 trades per request | ✅ |
| Crypto minute/day bars | `/v1beta3/crypto/us-1/bars` | `sync_bars` | spans nobody holds (BTC: 2021 to Massive's start, and today) | 10,000 bars per request | ✅ |
| Asset record (name, exchange, active) | trading API `/v2/assets/{T}` | `check_listing` | weekly, unless another feed checked the ticker this week (shared) | 1 | ✅ |
| Exchange codes (trade venues) | `/v2/stocks/meta/exchanges` | `sync_exchanges` | once | 1 | ✅ |

### Exchanges, composite and reference models, quality

| Exchange feed | Endpoint | Key | History kept | Cost | Status |
|---|---|---|---|---|---|
| Coinbase (reference for crypto) | `api.exchange.coinbase.com/products/{P}/candles` (300 per request) | none | `CRYPTO_VENUE_DAYS` (90) | ~430 requests per pair for 90 days | ✅ |
| Bitfinex | `api-pub.bitfinex.com/v2/candles/trade:1m:t{P}/hist` (10,000 per request; 5 significant digits) | none | 90 days | ~13 requests | ✅ |
| Bitstamp | `bitstamp.net/api/v2/ohlc/{p}/` (1,000 per request) | none | from 2011 | ~8,000 requests once | ✅ |
| Kraken (via Alpaca `us-1`) | see Alpaca | none | ticks 1 year, minute from 2021 | - | ✅ |

The one-download rule holds within a market (feeds whose data is the same market: SIP via Massive,
Alpaca or Tiingo); different exchanges are different data and each keeps its own history. The
composite (`composite.py`, source `composite`, `composite_stats` table) is rebuilt after each sync
from the exchange feeds (consolidations are not inputs); exchange quality is re-measured daily over
30 days. Quality priors: exchange / official 1.0, consolidated or vendor 0.9, research 0.85, mirror 0.8.

### Ticks: highest resolution within reason

Measured from stored bars (Oct 2026): the 101 watched stocks print about 21 million trades a day
(NVDA 1.8M, median stock 120k); Bitcoin on Kraken about 160k. A stored tick takes 72 bytes, so
ticks grow by about 1.5 GB a day (~390 GB a year) for ~2,200 requests a day. Every tick back to
2016 would be ~2.2 TB and 3 million requests, so: ticks are collected going forward always and
backfilled one year (`TICK_HISTORY_DAYS`), minute bars cover everything older. Each feed's ticks
live in a file of their own (`feeds/<feed>-ticks.db`; `admin backup --ticks` copies them). Tick backfill pauses
below `TICK_MIN_FREE_GB` (250) free, all tick downloads below a fifth of that.

One-second and minute bars are built here from the ticks, applying the consolidated tape's
condition rules (odd lots set no high/low, average-price prints no open/close, ...). The rules
come from Massive's `/v3/reference/conditions` (weekly, 1 request; `conditions` table). Where a
feed holds or is collecting ticks no bars are downloaded; audits compare the bars built from ticks
with another feed's own bars. Charts: minute bars are the base for all history; second intervals
read the one-second layer for the window on screen.

### One feed per span, and audits

The `coverage` table records which feed holds which span of every series. Before a request a feed
removes what any feed holds and what a better-ranked feed can serve now (ranking: resolution, then
audited accuracy, then `SOURCE_PRIORITY`); see `coverage.py`. Only audits download a span twice:
`audit.py` compares the second copy bar by bar (timing to the second, OHLC, high-low range, volume,
VWAP, trade count; box stats; margin of error 5 bps or 1 cent per price, 5 % volume) and stores the
comparison, never the bars. Audits are spot checks, not inspections of whole datasets: each
compares one random hour of a span, and a run's audits stop once they have used 2 % of its request
cap (`audit.AUDIT_SHARE`). They run after new data (queued for the other feed) and at random
(`AUDITS_PER_RUN`, `AUDITS_RANDOM_PER_RUN`), or with `python -m massive_collate audit`.

### Research datasets (`datasets.py`)

Free public files, each provider in its own file (`datasets/<provider>.db`), point in time: every
version of every value is kept with when its source published it, when it was stored here and the
file version it came from; `datasets show NAME SERIES --as-of DATE` reads what was public then.

- **GitHub-hosted datasets are git submodules** of this repository (`data/github/<owner>/<repo>`,
  pinned commits; `git submodule update --init` after cloning). Their whole commit history is
  imported, each commit a published version at its commit time; a fetch reads new commits and checks
  the submodule out at the newest (commit the new pin to record it).
- **Downloaded files** are kept locally, every distinct version, named by checksum
  (`<data>/sources/datasets/<provider>/<file>.<sha256[:12]>.<ext>`); a version is dated by the
  file's Last-Modified time. A file that will not change again is closed and never fetched again.
- A file's first import is history published on a schedule, dated by the source's rule (never later
  than the file): COT as of Tuesday, released Friday 15:30 ET; French data for month M around the
  15th of M+2; Shiller early the next month; IMF around the 5th of the next month; S&P 500 members
  from their "date added".

Refresh: in the hourly sync on each dataset's cadence, but only once fetched by hand the first time.
Each dataset carries a quality prior by provenance: official 1.00, the researcher's own library 0.85,
a mirror of someone else's data 0.80.

| Dataset | Source | Span (Oct 2026) | Refresh | Notes |
|---|---|---|---|---|
| S&P 500 constituents | submodule datasets/s-and-p-500-companies (mirror of the Wikipedia list, ODC PDDL) | 503 current members; 200 versions, 870 joins and 367 departures recorded | weekly | GICS sector of members drives the market map's sectors; membership readable as of a date |
| Fama/French 3 factors (daily, monthly), 5 factors (daily), 49 industries (daily, value-weighted) | Kenneth R. French Data Library (Tuck) | 1926-07 (5 factors 1963-07) to 2026-08 | monthly; revised history replaces the old | percent returns; -99.99 = missing |
| Shiller US market (S&P composite, dividends, earnings, CPI, long rate, CAPE) | submodule datasets/s-and-p-500 (mirror of shillerdata.com) | price 1871-01 to 2026-09; the other series stop in 2023 (mirror lags); 38 versions, the mirror not updated 2018-2024 | monthly | the mirror writes 0 for missing (read as missing); its first versions date months as decimal years; real series are restated every month (revisions) |
| IMF primary commodity prices (53 commodities, 10 indexes, 2005 = 100) | submodule datasets/commodity-prices (mirror of IMF data, frozen) | 1980-02 to 2017-06; 4 versions | none (frozen) | IMF terms: personal, non-commercial use |
| CFTC Commitments of Traders, legacy futures-only | cftc.gov (public domain) | 1986-01 to the latest Tuesday | weekly; past years closed | values are as of Tuesday, published Friday 15:30 ET |

### Sources by class: history, live, keys and where each is kept

Each source keeps its own files: a feed's bars in `feeds/<feed>.db` and its ticks in
`feeds/<feed>-ticks.db`, a dataset provider's values in `datasets/<provider>.db`; live data from a
source goes to that source's files (ticks raw, bars built from them in `built_bars` with a
derivation record). Keys are read from user environment variables only. Plan prices are approximate
(Oct 2026 listings); check the providers' pricing pages before buying.

| Class – source | History here | Live stream | Key | Kept in | Quality |
|---|---|---|---|---|---|
| stock – massive | minute and daily, 2 years; published end of day (free plan, 5 req/min) | none on the free plan; 15-min delayed from Stocks Starter (~$29/mo), real-time SIP on Advanced (~$199/mo) | `MASSIVE_API_KEY` set | feeds/massive.db | 0.90 prior (consolidated) |
| stock – alpaca | SIP minute and daily from 2016, latest 15 min held back | IEX real time, 30 symbols, 1 connection (free Basic); SIP real time on Algo Trader Plus (paid) | `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` set (paper keys) | feeds/alpaca.db; IEX live in feeds/iex.db | 0.90 prior |
| stock – tiingo | daily from 1962 (split-adjusted); IEX intraday | IEX stream (free-tier access unconfirmed) | `TIINGO_API_KEY` **not set** | feeds/tiingo.db | 0.90 prior |
| index – massive | end of day (free plan) | paid Indices plans | set | feeds/massive.db | 0.90 prior |
| index – cboe | daily CSV archive (VIX family), closed feed | none | none | feeds/cboe.db | 1.00 prior (official) |
| fx – massive | previous UTC day (free plan) | paid Currencies plans | set | feeds/massive.db | 0.90 prior |
| fx – tiingo | daily and intraday | forex stream (free-tier access unconfirmed) | **not set** | feeds/tiingo.db | — |
| crypto – coinbase | 1-minute from 2015 (90-day window here) | **public WebSocket, no key** (`ws-feed.exchange.coinbase.com`: ticker, matches) | none | feeds/coinbase.db (+ -ticks.db) | 0.93 measured; the reference exchange |
| crypto – kraken (via alpaca) | trades and minute bars (Alpaca's crypto REST needs no key) | Alpaca's crypto stream needs keys; Kraken's own public WebSocket v2 needs none (not wired yet) | none for REST | feeds/alpaca.db (+ -ticks.db, 1.7 GB) | 0.95 measured |
| crypto – bitstamp | 1-minute from 2011 | **public WebSocket, no key** (`live_trades_<pair>`) | none | feeds/bitstamp.db | 0.93 measured |
| crypto – bitfinex | 1-minute | public WebSocket, no key | none | feeds/bitfinex.db | 0.14 measured: a steady premium, not for live |
| crypto – massive | consolidated, previous UTC day (free plan) | paid Crypto plans | set | feeds/massive.db | 0.90 prior |
| crypto – composite | built here from the exchanges above | follows its exchanges | — | feeds/composite.db (derived) | — |
| future – massive | 8 hours delayed; CME Group product list daily | paid Futures plans (CME licensing) | set | feeds/massive.db | 0.90 prior |
| factors – french | daily/monthly, 1926–; month M around the 15th of M+2 | none (monthly release) | none | datasets/french.db | 0.85 prior |
| valuation – shiller | monthly, 1871–; git mirror (lags) | none | none | datasets/shiller.db | 0.80 prior |
| commodities – imf | monthly 1980–2017, frozen git mirror | none | none | datasets/imf.db | 0.80 prior |
| positioning – cftc | weekly, 1986–; as of Tuesday, released Friday 15:30 ET | none (weekly release) | none | datasets/cftc.db | 1.00 prior (official) |
| membership – sp500 | git mirror, 200 versions | none | none | datasets/sp500.db | 0.80 prior |

**Best live source per class, without a paid plan:**
- **Crypto: Coinbase's public WebSocket** (no key; the reference exchange), with Kraken's and
  Bitstamp's public WebSockets for a live composite. Not Bitfinex (its premium). Well-known coins
  only: BTC, ETH, SOL, XRP, ADA, DOGE, LTC, LINK (listed on all three).
- **Stocks: Alpaca's IEX stream** (wired, with the free Alpaca keys): real-time trades for up to 30
  symbols, those open on a chart first, then the rail's watchlists. IEX is one exchange (a few % of
  volume): prices are sound, volumes partial, so its trades are a feed of their own (`iex`) that a
  merged chart shows only where no whole-market feed has bars yet. Consolidated real time (SIP)
  needs a paid plan (Alpaca Algo Trader Plus, Massive Stocks Advanced).
- **Indices:** no free live index; their ETFs (SPY, QQQ, DIA, IWM) on Alpaca's IEX stream as proxies.
- **Currencies:** Tiingo's forex stream if its free tier allows (key needed; to be confirmed), else
  a paid Massive Currencies plan.
- **Futures:** no free source; Massive's free data stays 8 hours delayed.
- **Datasets:** no live data; each refreshes on its release schedule (the COT report, Fridays 15:30
  ET, is the time-critical one).

## 2. Calendars and time

| Dependency | Used for | Source / version | How it goes stale | Upkeep | Status |
|---|---|---|---|---|---|
| NYSE/Nasdaq rules (`sessions.py`) | chart time axis, freshness, US market state | own rules + special closures list; checked day by day against exchange_calendars 2000-2027 (test) | unscheduled closures (days of mourning) | Massive's upcoming list overrides automatically; `deps` flags disagreements; add lasting ones to `NYSE_SPECIAL_CLOSURES` | ✅ |
| CME Globex rules (`sessions.py`) | futures sessions, trade dates | own rules (equity vs energy halts, jobs-report Good Friday); validated against stored bars | product-specific holiday changes | official schedules override automatically | ✅ equity + energy groups; other products use equity rules ⚠️ |
| FX feed hours (`sessions.py`) | FX sessions | observed from Massive's feed (holiday eves, Good Friday) | feed behaviour changes | re-run the validation in `tools/backtest_projection.py` | ⚠️ derived from 2 years of observation |
| `exchange_calendars` (PyPI) | ~66 world exchanges: sessions, lunch breaks, holidays | 4.13.2 | exchanges publish next year's holidays late (China, Singapore, India record to year end) | `pip install -U exchange_calendars` each Nov–Jan; `deps` warns when coverage is < 60 days | ✅ |
| Python `tzdata` | IANA zones for the time-zone grid build, freshness | 2026e | governments change rules | `pip install -U tzdata`; rebuild the grid if zone borders moved | ✅ |
| Browser's time-zone database (Intl) | live GMT offsets on the globe | the browser's copy | browser updates | none (browsers update themselves) | ✅ |
| Solar/lunar/planet positions (`gui/astro.py`) | globe sun, Moon, planets | Schlyter elements; tested against eclipses and conjunctions | accuracy degrades over centuries | none | ✅ |

## 3. Vendored files (in `gui/static/vendor/`, served locally)

| File | Source | Licence | Update |
|---|---|---|---|
| `klinecharts/klinecharts.min.js` | KLineChart 10.1.0 (npm) | Apache-2.0 | manual; the app patches two store methods (`timeaxis.js`), re-test the time axis on upgrade |
| `world/countries-110m.json`, `countries-50m.json`, `countries-10m.json` (3.7 MB, 477k points; used past 6x zoom) | world-atlas 2.0.2 (Natural Earth) | ISC / public domain | rarely |
| `world/coast-h/*.bin` + `index.json` (433 tiles of 10°, 2.0M points, 8.7 MB; used past 12x zoom, GPU only) | **built** by `tools/build_coastlines.py` from GSHHG 2.3.7 high resolution (~200 m): `gshhs_h.b` shorelines + `wdb_borders_h.b` national borders. The 119 MB archive stays outside the repo (`~/MassiveData/sources/`), from https://www.soest.hawaii.edu/pwessel/gshhg/ | LGPL-3.0 (`LICENSE.TXT`, `COPYING.LESSERv3` alongside) | rarely (GSHHG updates every few years) |
| `world/time-zones.json` | **built** by `tools/build_timezones.py` from Natural Earth 1:10m time zones v5.1.2 + countries-50m + tz database | public domain | rebuild when zone borders change (rare); offsets are live anyway |
| `world/blue-marble.jpg` (1.9 MB), `world/black-marble.jpg` (0.8 MB): Earth by day and its city lights by night; not used at present (the Markets rail icon is the wireframe globe, from `countries-50m.json`) | NASA Earth Observatory: Blue Marble Next Generation (Dec 2004, 5400x2700) and Black Marble 2016 (0.1 degree, 3600x1800); URLs and sha256 in `world/VERSION` | public domain (NASA; credit in `world/LICENSE-NASA.txt`) | never needed |
| `sky/stars.6.json`, `constellations*.json` | d3-celestial 0.7.35 | BSD-3-Clause | never needed |

## 4. Hand-maintained lists (in code)

| List | Where | How it goes stale | Status |
|---|---|---|---|
| World exchanges, cities, flagship indices | `gui/world.py` `EXCHANGES` | new exchanges, renamed indices | ⚠️ review yearly |
| Market proxy funds (US-listed country ETFs; CurrencyShares FXA/FXB/FXC/FXE/FXF/FXY, UUP; IBIT) | `gui/world.py` `proxy=` (FX centres and crypto prefer the stored pair: `series=`) | funds close or change ticker (GXG became COLO in 2023, found by `deps`) | ✅ `deps` flags any proxy missing from the market file |
| FX dealing centres (city, local exchange calendar for its holidays, currency pair, fund) | `gui/world.py` (`kind="fx"`) | centres' importance shifts slowly; business hours are a convention (08:00-17:00 local) | ✅ |
| Crypto exchanges (headquarters or registration, main market) | `gui/world.py` (`kind="crypto"`) | exchanges move domicile, merge or close (Bitstamp is now Robinhood's; Binance declares no HQ) | ⚠️ review yearly |
| Futures product groups | `sessions.py` `EQUITY_FUTURES`, `ENERGY_FUTURES` | new products | ⚠️ only products on the watchlist matter |
| Company types counted as companies | `collector.COMPANY_TYPES` | Massive adding types | ✅ |

## 5. Gaps and ideas (not handled yet)

### Using every source fully (audit of the free tiers, 2026-10-24)

From the providers' current documentation (sources listed in the audit notes). Ranked by value for
the cost; requests are estimates.

| # | Gap | Source | Cost | Notes |
|---|---|---|---|---|
| 1 | **Massive's 5 requests/min is per asset class** (stocks, options, indices, currencies, futures each have their own; reference endpoints a separate bucket) and no daily cap is documented | Massive (massive.com knowledge base) | none | the limiter runs one 5/min window for all classes, and 2,000/day is our own cap: a window per class would multiply throughput within the plan |
| 2 | Full 1-minute history on the crypto exchanges | Coinbase 1m from 2015 (~19,700 requests for BTC at 10/s, ~33 min); Bitfinex 1m from 2013 (~715 per pair at 30/min) | as shown | we keep 90 days on both |
| 3 | Each exchange's own 5 min / 1 h / 1 day candles (received, replacing derived) | Coinbase (300/request), Bitstamp (1,000), Bitfinex (10,000), Alpaca crypto | 5m ~3,950, 1h ~330, 1d ~14 per Coinbase pair | `derive.py` builds them now; received ones replace them |
| 4 | **Real order books (level 2) for crypto**, live | Coinbase `level2_batch` (public; full `level2` needs auth) or Advanced Trade `level2` (public), Bitstamp `diff_order_book` (+ REST snapshot per connect), Kraken `book` (depth 1000), Bitfinex `book` (len 250) | no REST cost; heavy storage | the Analysis view's level 2 is virtual (traded volume) until then |
| 5 | Massive economy series: treasury yields (1962-), inflation (1947-), expectations, labour market, funding conditions | `/fed/v1/...` (every plan) | ~10 to backfill, ~5 a day | reference bucket |
| 6 | Massive dividends, splits, IPOs, short interest and short volume, float, ticker events | `/stocks/v1/...`, `/vX/reference/...` | ~150 to backfill, a few a day | corporate actions also make adjustment explicit |
| 7 | Massive Basic for options (contracts, per-contract end-of-day bars), indices (non-flagship), currencies (grouped daily), futures (from 2017) | Massive | per class, own 5/min | SPX, DJI, RUT and VIX are not on Indices Basic |
| 8 | Kraken directly (trades from 2013 at ~1 req/s; WS trades and OHLC) | Kraken public | trades backfill very large | Kraken data now comes via Alpaca (`us-1`) |
| 9 | L3 order flow and funding | Bitstamp `live_orders` (public), `order_data` (30 days); Bitstamp/Bitfinex funding and derivatives status | low | |
| 10 | Alpaca with free keys: SIP bars older than 15 min (10,000 per request at 200/min), IEX live (30 symbols), corporate actions, Benzinga news, screener, options chains with Greeks (from Feb 2024) | Alpaca | — | keys set; IEX live wired (live.py); the rest not yet |
| 11 | Cboe put/call ratios (CSVs 2006-2019, archive 1995-2003; daily pages after), CFE volume and open interest; check SKEW/VIX3M on the same URL pattern | Cboe public files | a few files, 1 page a day | |
| 12 | Tiingo with a free key: adjusted end of day (500 symbols/month, 1,000 requests/day), Dow 30 fundamentals, IEX reference-price and crypto WebSockets | Tiingo | — | waiting for `TIINGO_API_KEY` |


| Gap | Why it matters | Possible source | Status |
|---|---|---|---|
| News outside the US | the ticker's headlines are US-only, so world exchanges light up only for calendar events and big moves | a non-US news API (cost, licence) | ❌ |
| Real index levels for non-US markets | colours use US-listed proxy funds (USD, US hours), not the local index | Massive indices plan, or an index data vendor | ❌ |
| ~20 exchanges without a proxy fund (Prague, Budapest, Baltics, Dhaka...) | they stay uncoloured | local index data | ❌ |
| Ticks before the one-year window | seconds charts and tick analysis stop a year back | raise `TICK_HISTORY_DAYS` (≈390 GB and ~550k requests per extra year for the current watchlist) | ⚠️ by choice |
| Alpaca stock keys | without them Alpaca collects crypto only; with them it serves stock history from 2016 and the current session (15 min delayed) | a free Alpaca account's API keys, as user environment variables | ✅ set |
| Audit referee | with two feeds an audit says how far they differ, not which is right (their scores move together) | a third feed, or official closing prints | ⚠️ |
| Live recorder (websocket) | stocks stream from IEX only (30 symbols, one exchange's volume); the whole market's current session arrives with fetcher runs | Massive or Alpaca SIP websockets (paid plans) | ⚠️ IEX |
| Icons for futures products and indices | lettered badges only | none from Massive; would need a curated set | ❌ |
| Directory details refresh | details re-checked every 90 days with spare budget only | — | ⚠️ |
| Shiller fundamentals after 2023 | the mirror stops dividends, earnings, CPI and CAPE in 2023 | Shiller's own `ie_data.xls` (shillerdata.com; needs an .xls reader) | ⚠️ mirror lags |
| Current commodity prices | the IMF mirror ends in 2017 | World Bank Pink Sheet (`CMO-Historical-Data-Monthly.xlsx`) or the IMF's own PCPS | ❌ |
| Dataset series beside prices | the Datasets view draws dataset series on their own; they cannot yet be overlaid on a symbol's price chart | an overlay indicator fed from a dataset series | ❌ |
| Exchange holidays beyond what exchange_calendars records | China/Singapore/India: to year end until the package updates | the exchanges' own announcements | ⚠️ `deps` warns |
| Corporate actions (splits, ticker changes) for watched stocks | prices are split-adjusted by Massive (`adjusted=true`); ticker reuse is caught by the weekly FIGI check | `/v3/reference/splits`, `/v3/reference/dividends` | ⚠️ adjustments not stored separately |

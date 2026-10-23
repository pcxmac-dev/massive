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

Plan: free (5 requests/min, 2 years of history, end-of-day stocks/indices, futures 8 h delayed).
Hard caps: 250 requests per run, 2,000 per day (`MASSIVE_MAX_REQUESTS_PER_RUN/DAY`). Every request,
retries included, counts.

| Data | Endpoint | Fetched by | When | Cost | Status |
|---|---|---|---|---|---|
| Minute/day bars (stocks, indices, FX) | `/v2/aggs/ticker/{T}/range/...` | `collector.sync_bars`, `sync_daily` | each sync, only when the plan should have published a new session (`freshness`) | 1+ per symbol per new session; backfill ~12 per stock | ✅ |
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
| `world/countries-110m.json`, `countries-50m.json` | world-atlas 2.0.2 (Natural Earth) | ISC / public domain | rarely |
| `world/time-zones.json` | **built** by `tools/build_timezones.py` from Natural Earth 1:10m time zones v5.1.2 + countries-50m + tz database | public domain | rebuild when zone borders change (rare); offsets are live anyway |
| `sky/stars.6.json`, `constellations*.json` | d3-celestial 0.7.35 | BSD-3-Clause | never needed |

## 4. Hand-maintained lists (in code)

| List | Where | How it goes stale | Status |
|---|---|---|---|
| World exchanges, cities, flagship indices | `gui/world.py` `EXCHANGES` | new exchanges, renamed indices | ⚠️ review yearly |
| Market proxy funds (US-listed country ETFs) | `gui/world.py` `proxy=` | funds close or change ticker (GXG became COLO in 2023, found by `deps`) | ✅ `deps` flags any proxy missing from the market file |
| Futures product groups | `sessions.py` `EQUITY_FUTURES`, `ENERGY_FUTURES` | new products | ⚠️ only products on the watchlist matter |
| Company types counted as companies | `collector.COMPANY_TYPES` | Massive adding types | ✅ |

## 5. Gaps and ideas (not handled yet)

| Gap | Why it matters | Possible source | Status |
|---|---|---|---|
| News outside the US | the ticker's headlines are US-only, so world exchanges light up only for calendar events and big moves | a non-US news API (cost, licence) | ❌ |
| Real index levels for non-US markets | colours use US-listed proxy funds (USD, US hours), not the local index | Massive indices plan, or an index data vendor | ❌ |
| ~20 exchanges without a proxy fund (Prague, Budapest, Baltics, Dhaka...) | they stay uncoloured | local index data | ❌ |
| Live recorder (websocket) | the Live view's "recording" is grey; current-session data arrives only with fetcher runs | Massive websockets (paid plans) | ❌ |
| Icons for futures products and indices | lettered badges only | none from Massive; would need a curated set | ❌ |
| Directory details refresh | details re-checked every 90 days with spare budget only | — | ⚠️ |
| Exchange holidays beyond what exchange_calendars records | China/Singapore/India: to year end until the package updates | the exchanges' own announcements | ⚠️ `deps` warns |
| Corporate actions (splits, ticker changes) for watched stocks | prices are split-adjusted by Massive (`adjusted=true`); ticker reuse is caught by the weekly FIGI check | `/v3/reference/splits`, `/v3/reference/dividends` | ⚠️ adjustments not stored separately |

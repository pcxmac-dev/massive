# Progress

Working notes so work can resume in a fresh session. Newest first.

## State (2026-10-24)

- **Live charts**: the live recorder (`python -m massive_collate live`, started by `sync`) streams
  Coinbase and Bitstamp trades for 8 coins. The chart server pushes a live symbol's new bars over
  `/api/stream` (server-sent events) and keeps cached series current by splicing in only what was
  stored since (`BarEngine._refresh_tail`); fingerprints are per feed. The time axis has a handle
  with the clock (drag to place the current time on screen) and a dashed line through every pane.
- **Opening a symbol** asks every feed holding it to update it first (`/api/priority`: a
  `sync --tickers` run, or the running sync takes it next). Limiters share each source's window
  through the database (`ratelimit.KvLedger`); waits are recorded and the Fetcher view lists
  every server's limits.
- **Received over derived**: a feed's own bar replaces one built here for the same time (trigger
  `bars_replace_built`, keeping the built version in `bar_revisions`). `derive.py` builds 5 min,
  1 h and 1 day from each feed's minutes (after every sync; `derive` on demand); the chart marks
  derived candles. Daily bars run to today (built from minutes after the last official day);
  crypto and forex days are UTC days.
- **Analysis view**: virtual level 2 (volume by price, derived prices starred); box plots chart
  type; models fixed (short stretches, edges).
- **Rail**: Live icon heartbeat (or ROYGBIV pulse) while the open symbol streams; Markets icon
  is the Earth (`vendor/world/blue-marble.jpg`, `black-marble.jpg`, NASA, public domain): the
  day side while an exchange trades, the night side when none does (forex and crypto aside).
- **Testing**: `massive-charts-test` (sessions folder `.claude/launch.json`) serves an empty
  scratch database on port 8766; the old scratch copy predates the per-feed split.

## State (2026-10-22)

- **Collector** (`python -m massive_collate ...`): runs hourly via Windows Task Scheduler
  (`MassiveCollateSync`, `.venv\Scripts\pythonw.exe -m massive_collate sync`). Free Massive plan.
  Data in `~/MassiveData`: shared tables in `massive.db`, each feed's bars in `feeds/<feed>.db`, its ticks in
  `feeds/<feed>-ticks.db` (split 2026-10-23; pre-split backups `backups/pre-split-20261023-215938*.db`, 12 GB);
  log in `~/MassiveData/logs/massive-collate.log`.
- **Watchlist**: `SPCX::NASDAQ` (SpaceX Class A, listed 2026-06-12). Free plan gives 1-minute bars
  (ticks and 1-second refused). Stored: 85k minute bars 04:00–20:00 ET + 91 official daily bars.
  Overnight (BOATS, 20:00–04:00) is not in Massive's data; needs another source.
- **Chart app** (`python -m massive_collate gui`, http://127.0.0.1:8765): KLineChart 10.1.0 vendored
  under `gui/static/vendor/klinecharts`. Intervals built exactly from the finest stored data; official
  daily bars for D/W/M; regular/extended hours toggle; feed picker (single feed or merged);
  drawing tools incl. custom `measure`; drawings saved per ticker in browser localStorage.
- Tests: `.venv\Scripts\python.exe -m pytest -q` (169 passing). CI on GitHub: Python 3.12/3.14/3.15,
  Windows + Ubuntu. Branch `feature/collector`, not merged to `main`.

- **Highlight tool** (sidebar, 2nd; Alt+H): box, dashed center lines, start/center/end labels and
  exact range statistics from `/api/range`. Verified with real mouse clicks.
- **Chart types** (all of TradingView's): bars, candles, hollow, volume candles, line, line with
  markers, step, area, HLC area, baseline, columns, high-low, Heikin Ashi, Renko, Line break, Kagi,
  Point & figure, Range. Transforms in `gui/charttypes.py` (server, unit-tested; ATR(14) defaults;
  Range flagged approximate without ticks). Custom-drawn types use the `CHART_STYLE` renderer in
  `app.js` over invisible candles. All 18 checked visually on SPCX.

- **Multi-asset**: `symbols.resolve` maps NQ1!/CL1!/NG1! (continuous futures, `futures.py` rolls on
  session volume), USDEUR/EURUSD (fx, exact inversion), IXIC (I:COMP). SPX/RUT/DJI are not on the
  free plan. Free Futures Basic data is 8 h behind.
- **Layouts** (TradingView-style menu, top right): up to 5 saved markups in the `layouts` table —
  symbol, feed, interval, chart type + params, session, adjustment, indicators, drawings per ticker.
  Autosave (1.5 s debounce), Ctrl+S, rename, copy, new, delete. Writes need the `X-Massive-Charts`
  header (blocks cross-site writes).

- **Rendering performance (night of 2026-10-22)**: goal 60 fps on modest hardware.
  - `gui/static/gpu.js`: WebGL2 (OpenGL ES 3) instanced candle renderer under KLineChart's canvas;
    bars uploaded once, shader does placement with the chart's own rounding (x exact; y differs by
    1 px for 0.005% of prices at half-pixel ties). GPU toggle in the toolbar shows the device.
  - Level of detail: when candles are < 0.5 px the server sends exact aggregates of k consecutive
    bars (`lod` param, `lod_aggregate`), k a power of two, capped so ≥ half the chart width is used.
  - Measured (SPCX 1m ETH, 85k minutes on screen, volume pane on): 516 ms/frame → 16.7 ms (60 fps,
    vsync-capped) with or without GPU. KLineChart alone costs ~77 ms/frame at 85k bars, which is why
    LOD, not the GPU, was the decisive fix.
  - LOD also covers brick types (Renko, line break, range, Heikin Ashi): Renko full zoom 328 → 16.6 ms.
    Kagi / point & figure stay at full detail. NQ1! (688k minutes) full zoom: 60 fps at ×512.
- **Futures collection (rewritten tonight)**: Massive's contract listing returns one row per
  contract per day, so contracts are derived from tickers instead (`futures.candidate_contracts`),
  trying one-digit (CLZ6) and two-digit (NGX26) year names; each costs one session-bar request,
  and ended/missing contracts are remembered. Roll gaps come from settlement closes when intraday
  bars don't overlap. State after the night: NQ1!, CL1!, NG1! all 2024-10-23 → 2026-10-22 on
  1-minute bars (~710k / 727k / ~677k), 8 NQ quarterly rolls (gaps +206 to +296), IXIC 194k,
  USDEUR 734k minute bars. An hourly sync now costs ~24 requests.
- **Reliability fixes found tonight**: usage is recorded per request so killed runs still count
  against the daily cap; a sync lock left by a dead process is taken over at once; the LOD cache
  key includes chart type + parameters (it once served Renko aggregates for line break).

- **Side panel** (Info button; drag to resize): symbol, highlight statistics (the only place they
  appear — the highlight draws no text on the chart), key stats from local daily bars, business
  details from the weekly listing check (`instrument_details` table), data coverage. One highlight
  at a time.
- **Models** (`gui/models.py`): Price, 1st derivative, 2nd derivative, Integral; Savitzky–Golay
  window from the Detail slider (5–401 bars, log scale), basis price/log %, units per
  bar/minute/hour/day, reset at gaps. Drawings are stored per model (`drawingsKey()` in app.js).
- **Watchlist**: the top 100 S&P 500 stocks by market cap (stockanalysis.com ranking, Oct 2026;
  re-rank later from `market_cap` in `instrument_details`) plus SPCX, IXIC, USDEUR, NQ1!, CL1!, NG1!.
  First backfill ≈1,200 requests (~5 hourly runs at 250/run). `MASSIVE_REFRESH_HOURS` (default 12 on
  free) skips symbols synced recently so 100+ symbols stay under 2,000 requests/day; plan entitlements
  are probed once per asset class and kept 7 days (`entitlements` table). A stock's exchange is pinned
  from its first listing check.
- **Fetcher activity** (toolbar chip → dialog; `gui/activity.py`, `/api/activity`, `static/activity.js`):
  live run (symbol in progress via `runs.current`, requests vs caps), per-symbol backfill state
  (queued / backfilling / fetching / due / fresh) with stored range and a request estimate, recent
  runs, and the log tail. Read-only; never calls the API. Click a symbol to open it.
- **Calendars** (`sessions.py`): one market calendar for any date: NYSE rules (checked day by day
  against exchange_calendars XNYS 2000-2027, 0 mismatches), CME Globex hours with holiday halts per
  product group and their trade dates, the FX feed's own holiday hours; Massive's upcoming holidays
  and futures schedules (weekly, ~5 requests) override the rules. Validated against every stored bar.
- **Forward time axis** (`gui/projection.py`, `/api/future`, `static/timeaxis.js`): slots after the
  last bar are the starts of the bars that will really exist, through the same binning, daily keying
  and LOD grouping as real bars. `tools/backtest_projection.py`: 0 missed bars, 1m-1W, five symbols.
  KLineChart lets you scroll about one screen past the last bar; zoom out for further.
- **Bars** are binned on New York wall-clock time (fixed DST drift); regular hours follow the calendar
  (13:00 early closes, no weekend prints); indices are regular-hours only.
- **Rail = main views**: Stock / Analysis (chart + side panel), Markets (globe.js: wireframe globe at
  the subsolar point, ~50 world exchanges via exchange_calendars with lunch breaks and holidays),
  Fetcher (history; Scan button), Live (current-session feeds; red dot when recording). No pop-ups
  except the symbol switcher (tiles, sorted by 30-session dollar volume).
- **Opening a symbol** checks it against the calendar; if a newer session should exist it runs
  `scan --probe --fetch --only TICKER` in the background with progress in the status bar.
- **Topo data for the globe**: `static/vendor/world/countries-110m.json` (world-atlas, Natural Earth)
  is optional; without it the globe draws the graticule only.
- Testing without disturbing the user's open app: `.claude/launch.json` in the sessions folder has a
  `massive-charts-test` config (port 8766) pointing `MASSIVE_DB` at a scratch copy of the database.

## Next

1. Verify the remaining drawing tools by hand (trend line, channel, fib, note); confirm right-click
   and Delete remove a drawing.
1b. Optional: GPU volume pane (the remaining CPU-heavy pane when LOD is off, e.g. price-based types
   with very many bricks).
2. "Go to date" control (load older pages until the date is in range, then scroll to it).
3. Show which feed supplied each bar in the tooltip when viewing merged feeds.
4. A second source (e.g. an overnight/BOATS feed) as a `DataSource` subclass + `SOURCE_PRIORITY`.
5. Merge `feature/collector` into `main` once the user is happy.

## Hard requirements (from the user)

Never exceed plan limits or spend extra; data stays local; finest resolution as far back as allowed;
only the current instrument for a symbol; multi-source and multi-exchange; accuracy first;
unit tests with every change.

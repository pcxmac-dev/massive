# Progress

Working notes so work can resume in a fresh session. Newest first.

## State (2026-10-27)

- **Profiles** (no passwords, the user's choice): a login page (`login.html`, a Vitruvian Man) picks
  or makes one; each keeps its own watchlists (`live.watchlists_key`), layouts (markups), preferences
  (the page's, under `mc:p<id>:`) and register of the stocks it opened (`visits`; the watchlist
  panel's "Visited"). "Default" holds what came before; a new one starts empty. The fetcher follows
  the signed-in profile's watchlists (the one signed in last). The rail's foot shows the profile;
  a click signs out. Signing in opens the globe (`/#markets`; nothing saved: the globe too). The
  register notes only the symbols you open (`openSymbol` → `visit=1`), not the first one the page
  opens by itself; another profile's "Collected" is its own (its lists and register), so a new one
  shows none ("Default" keeps every symbol with data).
- **Blue Ocean queued**: every watched stock (7,885) is on the `boats` feed; its history fills
  within Alpaca's limits, the signed-in profile's watchlists first.
- **Month-end allowance** (the user: every feed's allowance spent each month): the Fetcher's
  Sources table shows each feed's month (used, lapsed on past days, left, where the pace takes it:
  `activity.month_allowance`). Found 2026-10-28: Alpaca's Blue Ocean history starts 2024-09-16 and
  Tiingo's has nothing earlier (probed: none 2021-01 to 2024-09-25 for AAPL), so Tiingo adds no
  overnight range; Massive's backfill is nearly done (~32k requests left, 9.4 TB free, massive.db
  629 GB, ~6 MB a request). Once the backlogs finish, spare allowance needs new data; the user
  chose Tiingo's forex minutes (not more 1-second years, not Tiingo's IEX).
- **Tiingo forex** (`tiingofx`, sources/plugins/tiingofx.py): currency pairs' 1-minute bars from
  2020-01-01 (Tiingo's first), three UTC days a request, history through the day before; its own
  feed file and market, on Tiingo's key, rate window and daily cap. The app's 7 pairs: about 3,100
  requests and ~2 GB of Tiingo's 40 GB a month. More pairs would need a guard on that transfer cap.
- **Feeds in parallel** (docs/APPRAISAL.md #6): a run lock per feed (`massive-fetch-<feed>.lock`;
  `massive.lock` is now the whole database's, for moves of the data). The hourly sync starts each
  feed that is set up and not fetching in a process of its own, does not wait for them, and builds
  the derived frames once an hour; a symbol opened in the chart fetches at once on the feeds it
  needs that are idle. A sync from before this change still holds `massive.lock`: the new hourly
  syncs start no feed until it ends.
- **Plugins find their own plan; the Plugins view** (rail: a plug; docs/ARCHITECTURE.md, "Each
  plugin refreshes its own plan info", "The Plugins view"): scans may name the plan (Massive's
  stocks plan, Alpaca's), applied where the user set none; scans on each chart app start, weekly,
  and from Rescan. Tiingo's scan tells Starter from a paid plan by its News API (Power and Commercial alike: a paid
  key gets Power's limits unless TIINGO_PLAN=commercial). The user's key (2026-10-27): News served
  (paid), Blue Ocean served (the add-on, refused earlier that day), fundamentals add-on not. The
  Exchanges view no longer shows beside other views.
- **Providers are plugins** (docs/ARCHITECTURE.md, "Providers are plugins"; `sources/registry.py`,
  `sources/plugins/`): one module per provider holds its build, keys, plan, rate, markets and every
  fact the app asked about it by name; installed packages add providers through the
  `massive_collate.providers` entry points. `tools/set-key.ps1` is a menu of the plugins (set,
  replace or remove a provider's keys); `python -m massive_collate plugins` lists them. Not moved
  yet: the live recorder's streams. The chart app on 8765 and the live recorder run it once restarted.
- **Live streams only for what is selected** (LIVE_STREAMS=selected, default): coins and stocks
  stream only while open on a chart; with none open nothing is connected. `always` keeps the old way.
- **Collection priority** (the user): #1 daily bars, #2 1-minute, #3 1-second.
- **Delivery scheme** (docs/ARCHITECTURE.md, "Which source delivers what"; `sourcing.py`): every
  plugin's standard report (plan, limits, capabilities, streams), eras unique or common per job and
  market, given to sources by cost (days of their limits) in the user's order; the fetcher follows
  it. `python -m massive_collate sourcing` shows it. Not done: a Fetcher panel for it.
- **Overnight session** (docs/ARCHITECTURE.md, "The fourth session"): Blue Ocean's overnight minute
  bars through Alpaca (`boats` feed, free Basic plan, Alpaca's limits shared), the calendar's
  overnight, the chart's ETH+ON. Symbols go on the feed as they are opened in the chart (or with
  `watch add --every-feed`). Tiingo has it only as a paid add-on ($9/month): not used.
- **CME Group tracked** (docs/ARCHITECTURE.md, "CME Group alongside NYSE and Nasdaq"): every
  readable CME/NYMEX/CBOT/COMEX product as a continuous future, best-known first; futures capped
  per run (FUTURES_PER_RUN), less traded ones weekly. Estimated 30-100 GB once filled (1-2 weeks).
- **Exchanges view** (rail: a pair of scales; docs/ARCHITECTURE.md, "The Exchanges view"): every
  tracked venue, its products, the venue's or a product's insight on the right.

## State (2026-10-26, afternoon)

- **Review fixes merged** (CODE_REVIEW.md, "Implemented"); the sync and the chart app run them.
- **Fetch cutoff fixed**: a symbol opened hours into a run was fetched only up to the run's start
  (SPCX opened at 15:06 ET stopped at 12:54, a two-hour hole) and stayed "next in line"; each
  symbol now takes the run's clock when it is served (test_fetch_cutoff.py). The sync runs it.
- **Resources view** (rail: a chip; resources.py, resources.js): the app's processes (CPU, memory,
  reads and writes, TCP connections and their remote ends), the machine's CPU, memory and
  bandwidth, the disks, the data files with growth. Windows counters through ctypes (no package);
  sampled every 30 s in the background, every 2 s while viewed, within 5 % of a core (the user's
  limit: 10 %), history capped at 16 MB. Bandwidth is the machine's: Windows has no per-process
  network bytes without administrator tracing. The chart app on 8765 runs it once restarted.
- **Still to do**: restart the live recorder (Oct 25 code); "next in line" during market hours and
  the redesign below; the chart's GMT−8 axis is Pacific standard time (Pacific is GMT−7 until 1 Nov).

## State (2026-10-26, early morning)

- **Code review**: CODE_REVIEW.md (top level): five reviews by area, fixes made, 44 open findings
  ranked, disk measurements and the decisions they need. Start there.
- **Fetcher principles** the user set (also in Claude's memory): live feeds (real time or delayed)
  own the session under way, history takes completed sessions only (per venue: BOATS makes US
  stocks ~24/5); fetch only 1 s, 1 min and 1 day (1 min only before 1-second data starts), derive
  the rest; interrogate each source's full range; a second source with more range wins; resume with
  an overlap audit; picked symbols (opened, on a watchlist) updated regularly, the rest can be paused.
- **Done tonight** (committed; the sync and chart app on 8765 still run older code until restarted):
  history horizon (completed sessions only), writes sorted oldest first (pages were half empty:
  1.76x disk), no fetching of feeds' own 5 min / 1 h candles, fetch order (opened, watchlists, the
  last session's big movers, then most traded), daily-bars-first sweep, Tiingo with its key (AAPL
  daily complete from 1980), `audit --daily`, updates pause (7 days to 2 Nov), Gov view, live key
  reading, `tools/set-key.ps1`, listing-identity fix (Alpaca's empty check blanked Massive's).
- **Needs the user**: restart the sync and chart app to run tonight's code; decide on compacting
  massive.db (~150 GB), dropping built-bar revisions (~14 M rows+), the pre-split backups (12 GB),
  moving tick files (82 GB), and which symbols get 1-second depth.
- **Redesign still to build**: per-source range interrogation and the 1 s / 1 min / 1 day map per
  symbol (with the transition rule), per-venue session calendars (BOATS), best current-session feed
  per symbol, resume with an overlap audit.

## State (2026-10-24, night)

- **Bug sweeps done** (5, with erroneous process behaviour in mind; 564 tests): processes and
  locks (`proc.py`: "pid start" stamps, no age-based staleness), fetching (per-symbol errors skip
  the symbol, network retries, empty windows recorded), storage and derived data, the chart
  engine and server, and the front end (no snap-back on redraw or resize, calendar and globe
  loaders that cannot storm, one outage = one step of the request pause, last symbol picked
  wins, crypto/fx days in UTC, http(s)-only headline links, watchlist rows updated in place).
- **Appraisal quick wins** (docs/APPRAISAL.md says which): `Store.kv_json` / `kv_update` (atomic
  across processes), 30 s SQLite busy wait, one `clear_denied`, the Fetcher's state order one
  list. `synchronous=NORMAL` deliberately not done (bars and coverage are in different files).
- **Per-symbol data across feeds**: `inventory.py` → `python -m massive_collate inventory TICKER`,
  `/api/inventory`, "Data by feed" in the side panel (● sent, * derived, ○ not built yet, with a
  "Build missing" button that runs `derive --tickers` locally). The sync now builds each symbol's
  derived frames right after its fetch (`derive.build_symbol`); a rebuild covers only the changed
  span (`changed_span`); chunks read a day either side, so CME sessions across a chunk edge are
  whole. The real database had no v3 derive markers yet (the long Massive run started before);
  the first build per series covers its whole history.
- **Needs the user**: ~34 stray live recorders (old code, started before the machine lock) still
  run and reconnect to Coinbase every second ("subscription limit reached", ~20 refusals a minute
  at 11:36): stop them (command in the session) and start one recorder (`live start`). The chart
  app on 8765 runs the old server code until restarted (the page scripts are new; the inventory
  panel stays empty until then).
- **Deferred** (found in the sweeps, not done): splits after download (needs corporate actions);
  a stored_at index or series catalog for change detection at 7,900 symbols; composite gap
  rebuild; an archive hole when a better feed backfills later; load_bars start/end date
  semantics; derivation row counts inflated by the live recorder; preferred shares (ticker case);
  an intermittent test_datasets checksum failure seen once, not reproduced (12/12 passes).

## State (2026-10-24, evening)

- **Plans and caps**: Massive **Advanced** for stocks (`python -m massive_collate plan` probes it:
  trades, quotes, seconds, history from 2003-09-10); the other Massive classes stay on the free
  plan (`MASSIVE_TIER_INDICES/_CURRENCIES/_CRYPTO/_FUTURES`, each with its own history, delay and
  5 req/min rate). Saved user env vars (read from the registry by running processes):
  `MASSIVE_TIER=advanced`, `MASSIVE_MAX_REQUESTS_PER_RUN=10000`, `MASSIVE_MAX_REQUESTS_PER_DAY=200000`
  (the user approved these caps). Alpaca paper keys set (free Basic: SIP from 2016, 15 min behind).
- **Watchlist**: `initialize` put every NYSE and Nasdaq listing (7,894; preferreds left out: the
  directory loses their ticker case) on massive, alpaca and tiingo at minute resolution, ranked
  by traded value (`watchlist:order`); backfills go most traded first. 7,913 symbols in all.
- **Which feed downloads a span** (`coverage.rank`): availability, precision, freshness (real time
  over delayed over end of day), quality, priority. A plan change re-resolves each symbol's
  resolution (bulk symbols stay at minutes). Charts open on every feed merged, the reference
  exchange first (Coinbase for crypto), other feeds filling where it has nothing.
- **Sanity checks for repeated failures**: the page shares in-flight requests and pauses failing
  endpoints (a 60/s retry loop sent 1.8 M requests); live streams back off up to 5 min; a symbol
  refused 3 runs in a row waits a day (`refusals:<feed>`); empty history walked by a capped run is
  recorded (Coinbase windows); symbols still backfilling are never skipped; one live recorder per
  machine (`MASSIVE_LIVE_LOCK`). Stray recorders from old test runs (~35, old code) still need
  stopping by the user (Claude is not allowed to stop processes).
- **Live**: Alpaca IEX stream for the open stock and the rail's watchlists (≤30, own `iex` feed,
  ranked below whole-market feeds); the Live icon pulses red while data arrives (blue/white selected).
- **Derived data**: every interval stored, only upward (30 s; 5/30 m, 1/2/4 h, 1 D; W/M/Y); a feed's
  own bars at the chart's interval are prime; the status bar shows builds; Fourier model and
  indicator; the Analysis highlight greys out everything outside it.
- **Fetcher**: every source's status (ok/fetching/limited/down/off), "Unable to fetch data
  currently", pause/resume fetching, 300 most relevant rows of 7,913 with counts.
- **Scale**: /api/symbols 0.6 s and the activity poll 0.5 s with 7,913 symbols on the real database.
- **Next**: results of five bug sweeps and a modularity/style appraisal; then per-symbol data from
  several feeds and derived data everywhere. A private web portal (Tailscale + token) awaits the
  user's go-ahead.

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
  is the Markets view's wireframe globe in miniature (1:50m coastlines and borders), turned so
  the point where it is noon faces you (24 hourly frames). The NASA images in `vendor/world` are
  kept but unused.
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

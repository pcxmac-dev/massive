# Progress

Working notes so work can resume in a fresh session. Newest first.

## State (2026-10-22)

- **Collector** (`python -m massive_collate ...`): runs hourly via Windows Task Scheduler
  (`MassiveCollateSync`, `.venv\Scripts\pythonw.exe -m massive_collate sync`). Free Massive plan.
  Data in `~/MassiveData/massive.db`; log in `~/MassiveData/logs/massive-collate.log`.
- **Watchlist**: `SPCX::NASDAQ` (SpaceX Class A, listed 2026-06-12). Free plan gives 1-minute bars
  (ticks and 1-second refused). Stored: 85k minute bars 04:00–20:00 ET + 91 official daily bars.
  Overnight (BOATS, 20:00–04:00) is not in Massive's data; needs another source.
- **Chart app** (`python -m massive_collate gui`, http://127.0.0.1:8765): KLineChart 10.1.0 vendored
  under `gui/static/vendor/klinecharts`. Intervals built exactly from the finest stored data; official
  daily bars for D/W/M; regular/extended hours toggle; feed picker (single feed or merged);
  drawing tools incl. custom `measure`; drawings saved per ticker in browser localStorage.
- Tests: `.venv\Scripts\python.exe -m pytest -q` (147 passing). CI on GitHub: Python 3.12/3.14/3.15,
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

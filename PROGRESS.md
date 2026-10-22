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
- Tests: `.venv\Scripts\python.exe -m pytest -q` (69 passing). CI on GitHub: Python 3.12/3.14/3.15,
  Windows + Ubuntu. Branch `feature/collector`, not merged to `main`.

- **Highlight tool** (sidebar, 2nd; Alt+H): box, dashed center lines, start/center/end labels and
  exact range statistics from `/api/range`. Verified with real mouse clicks.
- **Chart types** (all of TradingView's): bars, candles, hollow, volume candles, line, line with
  markers, step, area, HLC area, baseline, columns, high-low, Heikin Ashi, Renko, Line break, Kagi,
  Point & figure, Range. Transforms in `gui/charttypes.py` (server, unit-tested; ATR(14) defaults;
  Range flagged approximate without ticks). Custom-drawn types use the `CHART_STYLE` renderer in
  `app.js` over invisible candles. All 18 checked visually on SPCX.

## Next

1. Verify the remaining drawing tools by hand (trend line, channel, fib, note); confirm right-click
   and Delete remove a drawing.
2. "Go to date" control (load older pages until the date is in range, then scroll to it).
3. Show which feed supplied each bar in the tooltip when viewing merged feeds.
4. A second source (e.g. an overnight/BOATS feed) as a `DataSource` subclass + `SOURCE_PRIORITY`.
5. Merge `feature/collector` into `main` once the user is happy.

## Hard requirements (from the user)

Never exceed plan limits or spend extra; data stays local; finest resolution as far back as allowed;
only the current instrument for a symbol; multi-source and multi-exchange; accuracy first;
unit tests with every change.

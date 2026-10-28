# Code audit, 2026-10-28

Six read-only reviews of the whole package (fetcher core, sources, storage and data quality, live
data, the chart app, the rest), each finding checked against the code before acting on it. What was
fixed is in the commits of that day; what is open is listed with where it is and what it breaks.

## Fixed

- **Data deleted:** the weekly listing check hard-deleted every feed's bars and recorded trades of a
  ticker whose FIGI or listing date changed. Now moved to the feed's archive tables
  (`Store.archive_before`), kept.
- **Limits per account:** a 429 or refused key on Alpaca left Blue Ocean sending into it (and Tiingo,
  Tiingo forex); kills now reach the feeds sharing an account (`Provider.account`) and last at
  least the server's Retry-After. The weekly scan passes a 429 on instead of swallowing it.
- **Keys:** read without the spaces or line breaks around them; a key a request cannot carry is
  never retried or repeated in an error (it went to the log and the kill's reason).
- **Blue Ocean:** history takes completed nights only (every stock was re-asked each hourly run of
  the night); a frontier with nothing of the night under way is no longer recorded held.
- **Bars from trades:** prior-reference and derivatively priced prints set no high or low (SPCX's
  spikes); each feed's own condition rules first (Massive's id 4 was read as the letter 4).
- **Chart app:** a tab of one profile can no longer write over another's lists (409, reload); the
  profile cookie is checked off the shared lock; odd cookies and Content-Lengths answered; sign-out
  saves unsaved markups; a symbol's run is not "failed" on the poll before its start; small fixes.
- **Paper trading:** an order on a series it cannot fill from is refused, one gone bad no longer
  breaks the whole book, a day order on a symbol with nothing stored expires.
- **Elsewhere:** the SEC contact never follows a redirect; Bitfinex's list errors are read; COT
  markets sort as meant.

## Open

Most severe first within each area.

### Storage and data quality
- `storage/feeds.py` `_excluded` / `storage/pruned.py`: a pruned entry hides whatever bar has that
  time, for good: a corrected bar the feed sends later, or a repair rebuilds, stays hidden. Exclude
  only while the stored bar is the pruned version (compare `stored_at`/`derivation`).
- `storage/feeds.py` `withdraw_trades`: deletes withdrawn trades (no record kept) and matches on
  `seq` over 24 h (ids unique only within a day elsewhere). Keep them flagged; match the exact time.
- `live.py` ticks coverage: entered from the floor of the minute of the first trade, not from when
  the stream started hearing; `baddata.inconsistent` then prunes the feed's good minute. Round the
  start up, or require the trades span to cover the bar.
- `composite.py` `refresh`: rebuilds the last two days only; late fills or prunes mid-history never
  reach the composite. Rebuild each exchange's changed span (as derive does).
- `storage/pruned.py` `built_rows` (repair-ticks): reads every built frame, then filters; filter in SQL.
- `storage/feeds.py` `stored_at` stamped before the lock wait; derive/baddata watermarks look back 60 s.
- `storage/common.py` `BEGIN IMMEDIATE` also locks the attached ticks file.
- `storage/admin.py` `compact_file`: no re-check right before the rename.
- `storage/feeds.py` trigger upgrade drops and recreates `bars_revised` in two transactions.

### Live data
- `live.py` / `cli.cmd_live`: the recorder never rereads its settings (keys, LIVE_STOCKS, LIVE_STREAMS).
- `app.js` / `server.py` `/api/stream`: one open stream per tab, six per host: polls queue behind them.
- `live.py` `_status[...]["pairs"]`: grows with every symbol streamed (the Blue Ocean poller's does not).
- `live.py`: trades missed while the IEX stream reconnects are never recovered.
- `server.py` stream check through the heavy-request cap: a refused stream polls `/api/bars` for good.

### Fetcher core
- `collector._monthly_symbol`: Tiingo's 500 symbols a month counted per database, not per key; Tiingo
  forex not counted with it.
- `coverage.frontier_ms` (day bars, 24/7 assets): a finished UTC day counted a day late.
- `collector.py` futures: one-digit-year tickers (ESZ6) collide across decades in `futures_contracts`.
- `collector._due`: an end-of-day frontier not published at the first try waits for the next publication.
- `sourcing.offers`: built without settings, so Tiingo's plan does not reach the delivery scheme.
- `collector.py`: a watch row removed during a run raises IndexError and ends the whole run.
- Resume "with an overlap audit" (the owner's principle) is not implemented: a fill resumes exactly
  at the ledger's edge (safe, but the seam is not audited).

### Sources
- `sources/plugins/tiingo.py` scan: any answer from the News API is taken for a paid plan.
- Tiingo's monthly transfer (1 GB Starter, 40 GB Power) is not counted.

### Chart app
- `gui/window.py` older pages: a page ending mid-bar returns that bar cut, never corrected.
- `gui/window.py` replay: the `after_ms` loop is not clamped to the data's first bar (years read at once).
- `app.js` `selectSymbol`: a symbol with nothing stored (404) is never asked of the fetcher.

### Elsewhere
- `gui/world.py`: FX centres' hours wrong under pandas 2.x (a pytz zone in `datetime.combine`); fine on 3.x.
- `gui/projection.py` / `/api/future`: no cap on `offset` (past 2262 the timestamp overflows).

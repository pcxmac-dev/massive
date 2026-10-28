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

## Review of the news plugins and the evening's fixes, 2026-10-28

Three reviews of the code since the audit (the news and publications plugins, the limits checks,
the Blue Ocean status), each finding checked and fixed with a test (tests/test_publications.py,
tests/test_open_findings.py, tests/test_plugins.py, tests/test_live_boats.py), and the open findings
below worked through.

- **The agencies' reader** (publications.py), live since the restart that day: a 304 was taken for a
  redirect, so every unchanged feed was asked twice; a refusal held one feed while the publisher's
  others went on asking in the same second; a Retry-After counted only on a refusal; robots.txt's
  crawl-delay spaced each feed from itself, not the host's requests; a failing feed was asked every
  10 minutes for good; a parser's or a plugin's surprise escaped unnoted, so its feed was asked every
  minute and the publishers after it skipped; a redirect's target was not checked against its
  robots.txt, and a moved robots.txt read as allowing all; the Recheck ignored pauses, kills, holds
  and refusals, and read robots.txt on every press. Now a refusal or a Retry-After holds the whole
  publisher (gov.db `holds`), a crawl-delay spaces every request to its host, failures back off to
  the feed's own cadence, every error is noted, redirects and robots.txt follow RFC 9309 on the
  publisher's own hosts, and nothing is sent while paused, killed (its own or its account's), held,
  or while background fetching is paused or halted (the chart's button). Massive's headlines are
  one request, never retried: a 429 kills Massive's fetching, anything else waits its schedule.
- **The Plugins view**: an account's kill showed as "Paused by you" with a Resume that did nothing;
  one publisher's failing calendar took the whole view down; a robots.txt 429 read "allowing all";
  two Recheck presses at once started two; the terms link was not checked for its scheme.
- **Server limits** (registry.py): a plan with no limit of its own ignored its server's; Blue
  Ocean's scan kept its limit under its own name while it paces on Alpaca's limiter; a few a second
  were rounded up to one a second (twice a server's 30 a minute); an hour's limit was cut to 0 a
  minute; the draft's several policies ("10;w=1, 1000;w=3600") failed the scan. Now every limit
  heard, with its window, on every feed of the account, keeps the window inside it
  (`registry.within`), and a lower one found mid-run tightens the running client and, hourly, the
  recorder's Blue Ocean poller.
- **The recorder**: the news thread died without a word when gov.db would not open, and a saved bad
  setting stopped every publisher; Blue Ocean stayed "connected" on stale quotes through any error
  but a refusal (now idle on Tiingo's day cap, down on anything else).
- **Fixed from the list below**: pruned bars hide only the version pruned (a correction or a repair
  shows, and is judged again); withdrawn trades are archived, never deleted, and found by id, price
  and size (the newest), not an id alone; the recorder claims a bar from the first one it heard
  whole; the composite rebuilds each exchange's changed span; `built_rows` filters its frames in
  SQL; Tiingo's 500 symbols a month are counted per key on the machine, its forex with them; a
  symbol taken off a watchlist mid-run is skipped, not the run ended; Tiingo's scan takes a paid
  plan only from news articles; a replay reads from the data's first bar; FX centres' hours use
  the zone's real offsets; `/api/future` caps its offset.

## Open

Most severe first within each area.

### Storage and data quality
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
- `coverage.frontier_ms` (day bars, 24/7 assets): a finished UTC day counted a day late.
- `collector.py` futures: one-digit-year tickers (ESZ6) collide across decades in `futures_contracts`.
- `collector._due`: an end-of-day frontier not published at the first try waits for the next publication.
- `sourcing.offers`: built without settings, so Tiingo's plan does not reach the delivery scheme.
- Resume "with an overlap audit" (the owner's principle) is not implemented: a fill resumes exactly
  at the ledger's edge (safe, but the seam is not audited).

### Sources
- Tiingo's monthly transfer (1 GB Starter, 40 GB Power) is not counted.

### Chart app
- `gui/window.py` older pages: a page ending mid-bar returns that bar cut, never corrected.
- `app.js` `selectSymbol`: a symbol with nothing stored (404) is never asked of the fetcher.

# Code review — 26 October 2026

Scope: the whole application, ~21,000 lines of Python (`src/massive_collate`) and ~10,000 lines of
front end (`gui/static`), on branch `feature/collector` as of commit `4cb826b`.

Method: five independent read-only reviews, one per area, each told to confirm every finding against
the code and to drop what it could not confirm: the fetching core, storage, sources and live feeds,
the chart server and calculations, and the front end. The most severe findings were then checked again
by hand (marked **checked**). Line numbers are as of `4cb826b`; later commits moved some by a few lines.

Alongside the review, the fetcher was examined for disk use and needless updates (see
[Disk and updates](#disk-and-updates)). Fixes already made are listed first; everything after
[Open findings](#open-findings) is still to do, most severe first.

---

## Fixed during the review

| Commit | What |
|---|---|
| `26867df` | **Listing identity** (fetching core, critical, **checked**): Alpaca's listing check (no listing date, no FIGI) was shared to Massive, blanking Massive's listing date and replacing its reference record and logo link. Shared only when it carries an identity now; a known date or FIGI is never blanked. |
| `26867df` | The daily-first sweep stops on an outage or refusal (it retried up to 2,000 stocks, 5 counted tries each). |
| `26867df` | `delete_before` (weekly, every listed stock) read every bar of the ticker under a write lock to delete nothing: seeks first, deletes per frame. |
| `26867df` | Write-ahead logs cut back to 64 MB once checkpointed (1.3 GB beside `alpaca.db`, 0.76 GB beside `massive-ticks.db`). |
| `26867df` | An opened symbol is "current" only when a feed set up here can serve it; one checked mid-run is stamped with the check's time; big movers come from the session most stocks hold. |
| `ba16c39` | Gov: `GOV_CONTACT` (your name and email) went to every agency: now only to those that ask (the SEC). A refusal (401/403/429) was retried every 10 minutes: now 6 hours or the agency's Retry-After. |
| `ba14fe3` | History takes completed sessions only (see below). |
| `b98286f` | History written oldest first (see below); a feed's own 5-minute and hourly candles no longer fetched. |

---

## Disk and updates

Measured on this machine:

| Where | Size |
|---|---|
| `feeds/massive.db` | 383 GB (no free pages: all of it is live data) |
| `feeds/massive-ticks.db` | 61 GB |
| `feeds/alpaca-ticks.db` | 21 GB |
| `backups/pre-split-*` | 12.2 GB (copies of data now in `feeds/`) |
| everything else | under 15 GB |
| free on the drive | 9.7 TB |

`massive.db` holds about 1,300 backfilled stocks; at that rate all 7,900 would need about 2 TB.

**What was found, and done:**

1. **Pages half empty (done, `b98286f`).** Feeds send history newest first and each page was written
   in that order. A SQLite table keyed like `bars` splits its pages half empty when filled from the
   left: the same 200,000 bars took 8,007 pages written newest first against 4,553 oldest first
   (1.76 times). Every write is now sorted oldest first. The minute backfill still to come (~6,600
   stocks) would have taken about 1 TB more than needed.
2. **A feed's own 5-minute and hourly candles (done, `b98286f`).** Fetched although every frame but
   1 s, 1 min and 1 day is built here: requests, and the same bars stored twice.
3. **Re-fetching the open day all session (done, `ba14fe3`).** On a plan that publishes as it trades,
   every complete stock was "due" on every hourly run during market hours: each run re-fetched the
   open day of ~1,300 stocks (7,900 once backfilled). History now stops at the end of the last
   completed session; the session under way belongs to the live feeds. Symbols opened in the chart or
   on a watchlist still get it.
4. **Updates pause** (earlier tonight): symbols not picked get no updates for 7 days unless more than
   7 days behind.

**What needs your decision:**

| Proposal | Saving | Cost / risk |
|---|---|---|
| **Compact `massive.db`** (`VACUUM INTO` a new file, then swap it in) | ~40% of the file, about 150 GB, and faster reads | Hours of copying; the sync and chart app stopped for the swap. Do *not* use `admin vacuum` on it (storage finding 1). |
| **Stop keeping built versions as revisions** when the feed's own bar arrives, and drop the existing ones | ~14 M rows on the 57 stocks with seconds checked (AAPL alone: 5.2 M), growing with every stock given seconds | Those versions can be rebuilt from the kept trades; the point-in-time read of a built bar *before* the feed's arrived would be lost. |
| **Delete the pre-split backups** | 12.2 GB | They duplicate data in `feeds/`; `prune_backups` never touches them. |
| **Move the tick files** to another drive or a compressed archive | ~82 GB | Source data, so moved, not deleted; the live recorder needs `massive-ticks`/`alpaca-ticks` only if it writes them. |
| **1-second depth** | — | 94 stocks are set to seconds (the rest minutes); seconds cost ~50× the disk of minutes. Fetching seconds as far back as sources have them, with minutes only before that point (your rule), needs a list of which symbols get them. |

---

## Open findings

Severity: **critical** loses or corrupts data or spends without limit; **high** a likely failure with
real cost; **medium** a real defect with bounded cost; **low** an edge case or hardening.

### Critical and high

1. **high, chart server — `gui/window.py:235-254`: `span=0` loops forever (checked).** The auto path
   never validates `(period, span)`; `width = limit * step * factor` is 0 and the paging loop never
   ends, holding one of the two heavy-request slots and the engine lock. Two such requests make every
   chart answer 503 until restart; GETs need no header, so any web page could send one. `span=0` on
   `/api/scheme` or `/api/future` raises `ZeroDivisionError`. *Fix:* reject any `(period, span)` not in
   `window.LEVELS` before dispatching; guard `width > 0`.
2. **high, sources — `sources/massive.py:111-112`: a revoked key (401) is treated as "not on your plan"
   (checked).** `NotEntitled` is exempt from the kill policy everywhere, so every run spends its whole
   cap on 401s symbol by symbol (Massive and Alpaca alike). *Fix:* raise `Unauthorized` for 401 and
   stop the run with a strike.
3. **high, live feeds — `live.py:365,374`: a network drop kills the live streams for 30 minutes
   (checked).** A connection attempt that fails before connecting counts as a "flap"; four in ~7 s
   kill Coinbase, Bitstamp and IEX, escalating to 24 h, and the kill survives restarts. Live trades
   are the only tick source for those exchanges, so they are lost. *Fix:* count a flap only after a
   connection was made; plain backoff otherwise.
4. **high, fetching core — `collector.py:1252-1253`, `:783-784`, `:352-356`: a retired symbol's last
   gap is asked again every run, forever.** Its final session has no bars; the leftover gap is
   treated as the live frontier and never recorded. 2–3 wasted requests per delisted symbol per run,
   growing with every delisting. *Fix:* pass `frontier=False` for retired symbols so the tail is
   recorded as held/none.
5. **high, fetching core — `collector.py:606-609`, `:871-876`, `:1272`: an exchange transfer stops a
   stock for good.** A stock that moves exchange (BCPC: "listed on XNAS, not XNYS as watched; skipped"
   every run) gets no data, costs a reference request per feed per run, and, never finishing its
   minutes, blocks the scheduled 1-second pass for every stock. *Fix:* follow the primary exchange
   when FIGI or name match; stamp `checked_at` on a mismatch; leave such symbols out of
   `_minutes_first`.
6. **high, fetching core — `collector.py:383-388` → `baddata.py:168,183`: `verify_fetched` loads a
   whole changed span into RAM.** A liquid stock's first minute backfill (20 years, 4–5 M rows) is
   read whole: ~1–2 GB peak. *Fix:* chunk by `baddata.CHUNK_MS`.
7. **high, chart server — `gui/window.py:572`, `:769`: a range read has no cap.** `range_stats` and
   `range_profile` read the highlight's whole stretch at the chart's level: a highlight drawn over
   years, then zoomed to seconds, builds years of seconds in RAM under the engine lock. *Fix:* the same
   per-day estimate `scheme()` uses, before reading.
8. **high, storage — `storage/pruned.py:39-44`: a second rebuild drops the version the first kept.**
   The held check is per (ts, rule), so a later `repair-ticks` skips every ts already listed
   "rebuilt" and loses the version used between the two repairs (6.9 M entries exist). *Fix:* for
   "rebuilt", dedupe on (ts, stored_at/derivation).
9. **high, storage — `storage/admin.py:43-59`: `admin vacuum` needs twice each file's size and leaves a
   WAL as big as the database.** On the 383 GB file it would fail or block the live recorder for hours.
   *Fix:* `VACUUM INTO` and swap (see the proposal above); checkpoint after.
10. **high, front end — `app.js:410-413`: a model chart on a live symbol reloads its whole history
    every 2 s.** The stream's `reload` resets the chart on every write, jumping back to now and holding
    a heavy slot. *Fix:* throttle to a closed bar, and not while the user is scrolled back.
11. **high, front end — `app.js:2434-2437`: drawing changes made while a layout save is in flight are
    lost.** The server's copy replaces the local one when the save answers. *Fix:* keep local data, take
    only id/name/updated_at from the reply; clear `dirty` by generation.
12. **high, front end — `app.js:2283-2300`: drawings on indicator panes come back on the price pane.**
    `paneId` is not saved. *Fix:* save the owning indicator and map it back on restore.
13. **high, front end — `replay.js:175`: the replay buttons cannot be clicked while it plays.** The bar
    is rebuilt every 50 ms. *Fix:* rebuild only on state changes; update the clock's text alone.

### Medium

14. **fetching core — `coverage.py:87-90`, `freshness.py:181-182`, `collector.py:297-299`:** on a
    real-time plan the newest minute is recorded as held with no settling margin; a minute not yet
    published, or missing late prints, stays missing for good. *Fix:* trail the frontier by 2–5 min.
15. **fetching core — `collector.py:596-601`:** a retired mark set from one feed's answer (Alpaca's
    coarse status) is never cleared by a later check that finds the symbol active.
16. **fetching core — `cli.py:474,480`:** `DAILY_RESERVE` is computed before the sync spends, so
    directory icons can use the reserve; later runs then send nothing for the rest of the day.
17. **fetching core — `collector.py:387,418`, `storage/audits.py:82-86`:** the audit queue grows to a
    week of backlog (~10⁵ rows), loaded whole on every audit pass and `/api/audits` call.
18. **fetching core — `freshness.py:100`:** the collector's calendar never applies the official
    holiday overrides it fetches; on an unscheduled closure every symbol's frontier is asked all day.
19. **fetching core — `collector.py:611-619`:** cleanup of a reused symbol touches only the checking
    feed; Tiingo and Alpaca keep the previous issuer's bars.
20. **fetching core — `collector.py:329-330`:** empty-head detection never runs on a first (frontier)
    fetch, so a stock whose Massive history starts after its listing never offers the head to Tiingo
    or Alpaca (META's 2012–2016, GOOGL's 2004–2014 are this case).
21. **sources — `ratelimit.py:44-62,113-117`:** the request window and daily cap live in the database
    and are updated without a transaction: two databases on one key (a test copy without
    `MASSIVE_OFFLINE`) double the rate; two writers can under-count.
22. **sources — `datasets.py:266-269`:** a past year's COT file is closed on its first January fetch,
    possibly before the year's last report is out, and never fetched again.
23. **sources — `sources/public.py:303-313`:** the Cboe parser drops rows without OHLC columns and then
    closes the archive as complete (close-only files such as VVIX).
24. **sources — `futures.py:70-79`:** the continuous series rolls forward when the held contract lacks
    one session's bar that a later contract has, and never rolls back (price jump in NQ1!).
25. **sources — `live.py:362-377`:** a database error inside the reconnect handler ends the stream
    thread silently; the status still says connected.
26. **storage — `storage/pruned.py:100-115`:** releasing a pruned bar never rebuilds the frames above
    it or changes the series fingerprint; caches and derived bars keep excluding it.
27. **storage — `storage/feeds.py:544-547`:** where the feed's own bar is pruned, the filtered view has
    a hole a built bar could fill (NVDA's 2024-06-10 daily).
28. **storage — `storage/pruned.py:72`:** `pruned list` loads the whole pruned table (7.3 M rows of
    JSON) into RAM to list tickers.
29. **storage — `storage/admin.py:28-30`:** `status` scans every bar file (hours on 383 GB).
30. **storage — `storage/ledger.py:25-34`:** the coverage ledger's read-modify-write is not in a
    transaction, and its key ignores `origin`, so a 'ticks' span and a 'download' span starting at the
    same ms replace each other (spans then fetched again).
31. **storage — `config.py:169`:** `MASSIVE_API_KEY` would appear in any repr of `Settings` (no current
    caller prints it).
32. **chart server — `gui/server.py:1192-1198`:** no GET route checks where a request comes from;
    `/api/inventory?ticker=<random>` starts a thread and a reader per new ticker, `/api/trading`
    writes fills. *Fix:* require the `X-Massive-Charts` header (or same-origin) on all `/api/*`.
33. **chart server — `gui/server.py:807-814`:** the `_activity` cache never shrinks (free-text keys).
34. **chart server — `trading/paper.py:183-206`:** day orders expire by the wall clock even when the
    session's bars are not stored yet; a fill the bars allowed is missed.
35. **chart server — `gui/server.py:690-707`, `:521-523`, `:465-466`:** the shared lock is held during
    slow work (paper matching, world calendars); layouts, watchlists and the Fetcher wait.
36. **chart server — `gui/bars.py:585-603`:** whole-history loads hold the engine lock; one futures chart
    freezes every other chart and live stream.
37. **chart server — `gui/window.py:695-696`:** the level-2 profile of a past bar is built from a cut-off
    bar (half its volume in a reproduced case).
38. **front end — `app.js:364-367`:** a failed page load tells the chart there is no more history, and a
    stale failure can blank the newer symbol.
39. **front end — `replay.js:60,101,267-270`:** the replay keeps appending price bars into a model or
    Heikin Ashi chart after a switch.
40. **front end — `schemes.js:59-61,376`:** a scheme result for the previous symbol can be drawn as the
    new symbol's.
41. **front end — `app.js:2661-2665`:** Delete on another view deletes the hidden selected drawing.
42. **front end — `gpu.js:118-123`:** the candles vanish for good after a WebGL context loss.
43. **front end — `app.js:236-237`:** the Highlight panel is rebuilt on every crosshair move.
44. **front end — `app.js:1579-1583`:** every symbol pick sends a wasted heavy `/api/bars` for the
    previous symbol.

### Low

Fetching core: futures contracts re-asked every run on Mondays (`collector.py:503`); one ticker's 403
poisons the class entitlement for 7 days (`:1279-1284`); a pruned span's dirty mark can be lost if a
build fails (`derive.py:158,200`); a just-created process lock can be taken over (`proc.py:78-104`);
dead tick code still steers the planner (`collector.py:132-165`, `coverage.py:176-194`).
Sources: class limiters lack the 62 s jitter margin (`sources/__init__.py:41`); Retry-After uncapped
and HTTP dates unread (`sources/massive.py:101-107`); redirects can carry Alpaca's key headers to
another host (`:92`); an IEX correction may delete its own corrected trade (`live.py:201-205`); the
futures day window includes one session too many (`massive.py:220-221`); derivation records keyed by
the flush day (`live.py:406-419`).
Storage: derivation records lose concurrent updates (`provenance.py`); duplicate prune entries from
concurrent scans; a migration race on upgrade day; a revision key that can collide within one batch;
exports start at UTC midnight while ending on ET days; config parsing (`MASSIVE_REFRESH_HOURS` parsed
as int, an empty `MASSIVE_TIER` crashes, `.env` relative to the working directory); stale composite
bars older than 2 days; `COMP` resolves to the Nasdaq Composite, not Compass Inc.
Chart server: crypto/FX 2h/4h bars binned on New York time while their days are UTC; key statistics
for 24/7 assets count 252 sessions a year; schemes treat the forming bar as closed; the range-bar guard
looks at closes only; unexpected exceptions drop the connection instead of answering 500; "All data"
is ignored on the series path.
Front end: the activity poll never backs off; the page-close save fails silently over 64 KiB; projected
axis times refetched on every new bar; filter boxes lose the caret; fast dataset switches can show the
wrong series list; the now-handle drag leaks listeners on pointercancel; a few values reach HTML
unescaped (none from a feed); dataset marks are stored only in the browser; concurrent watchlist saves
can undo each other.

---

## Test gaps worth closing first

- A 401 across many symbols stops the run and strikes the feed (finding 2).
- `connect()` raising does not kill a live stream; the main loop notices a dead stream thread (3, 25).
- Parameter validation on the auto path: `span` 0/negative/huge, very wide `from`/`to` (1, 7).
- Repeated syncs after a symbol retires send no requests (4); an exchange-pin mismatch (5).
- `prune_replaced` / `repair_from_ticks` run twice (8); `release_pruned` rebuilds frames (26).
- Coverage spans of different origins starting at the same ms (30).
- Drawings round-trip with their pane; a save in flight keeps local changes (11, 12).
- Empty head on a first fetch (20); a real-time frontier with an unpublished last minute (14).

---

## Strengths noted by the reviewers

- **Data integrity by design:** the coverage ledger is written page by page and after the bars, so a
  crash re-asks at most a page; upserts rewrite only changed bars, with the replaced version kept by a
  trigger; built bars are kept apart with their derivation; dataset versions are point in time.
- **Request accounting is conservative:** every try is counted before it is sent, against hard per-run
  and per-day caps, with the limiter's window shared across runs and pauses checked after waits.
- **Key hygiene:** keys only in headers (tested), `next_url` followed only on the API host, keys hidden
  from repr (except finding 31).
- **The chart engine:** the window path reproduces the whole-history series bar for bar across levels,
  sessions and merged feeds; DST handled carefully; caches are LRU under a byte budget; schemes are
  causal by construction and tested for prefix invariance.
- **The front end:** escaping is consistent (`esc`, `safeUrl`), stale answers are dropped by tokens,
  requests are shared in flight and paused with backoff, timers are cleared and work coalesced into
  animation frames.

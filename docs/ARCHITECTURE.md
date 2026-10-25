# Architecture and refactor plan

One application. The aim of the refactor is that every result can say what it was computed from,
with what settings, and what was known at the time, so charts, analysis and backtests agree and a
backtest can never see the future.

## Principles

- **What was known when.** Every stored observation carries when it became available (published by
  its source) and when it was stored here; a later correction is a new version, never an overwrite.
  A backtest reads only what was available at its simulated time, including delayed reports (the COT
  report is as of Tuesday, published Friday 15:30 ET) and revisions (the French library revises
  history every month).
- **Raw apart from derived.** Raw observations (feed bars and ticks, dataset values) are kept apart
  from what is computed from them (bars built from ticks, composites, quality scores, audits,
  indicators, forecasts). Each derived result records its inputs (sources and their versions), the
  code version and the settings used.
- **No look-ahead outside viewing.** Calculations used for decisions or tests are causal: a value at
  time t uses data up to t only. Centred calculations (smoother, but they use later data) are for
  viewing charts only and are marked as such.
- **One implementation per calculation.** Calculations live in `calc/`, outside the GUI; the chart
  app, analysis and backtests call the same code.
- **One responsibility at a time.** Each refactor step moves one responsibility, keeps the public
  interface so callers change separately, and is checked by the full test suite before the next.

## Bad data (2026-10-25)

All source data is kept, always: received bars and ticks are never deleted or changed. Every series
has two views:

- **Unfiltered**: all data, everything stored, nothing taken out (the chart's "All data" switch).
- **Filtered** (the target data, what charts and calculations use by default): unfiltered without
  the bad data. The frames built from a base frame (5 minutes to days, weeks to years) are built from
  its filtered bars.

The **pruned list** holds the bad data, one per feed and symbol (the `pruned` table of each feed's
file): each entry is a bar as it was when taken out of the filtered view, the rule below that caught
it, the evidence (the numbers the rule compared, and for a spike the trades inside it priced outside
its neighbours' range), when it was pruned and by which code. A feed's own bar stays where it is,
only left out of filtered reads; a bar built here that a rebuild replaced lives on in the list alone.
An entry can be released (put back) without losing the record that it was pruned, and a released
entry is not pruned again. Pruning never mixes feeds: a feed's series is judged on its own. Reads of
a series as it stood at a past moment (`as_of`) include bad data: it was there. (`baddata.py`;
`python -m massive_collate pruned`; the chart app's Data panel.)

What constitutes bad data (rules 1 to 3 are checked whenever a series is derived: after each
symbol's fetch and in the live recorder, from where it changed; `pruned scan` checks stored history):

1. **Malformed**: a bar whose prices are not positive and finite, whose high is below its low or
   whose open or close lies outside them, whose volume is negative, or whose time is in the future.
   A malformed bar is no other bar's neighbour in rule 2.
2. **Spike without volume**: a bar whose range is at least 10 times the median range of its 20
   nearest neighbours (10 each side) in the same part of the session (US stocks: regular hours
   against regular hours, the rest against the rest: they trade very different volumes), and at
   least 1 % of its price, while its volume is at most 2 times their median volume, and whose open
   and close both lie within the neighbours' range (the price did not move on). A real move has
   volume behind it. Judged on seconds, minutes and days; a bar without 10 neighbours on both sides
   waits for them.
3. **Outside its own finer bars**: a feed's own bar whose high or low lies more than 1 % beyond the
   highest high or lowest low of the same feed's finer bars inside it (a day's minutes, all hours; a
   minute's seconds): an extreme in no trade the feed shows at the finer frame. Judged where the feed
   holds the finer frame for the whole bar and has bars in it, its pruned finer bars supporting
   nothing. (2026-10-25: Massive's daily bar for NVDA on 2024-06-10, the first day after its split,
   had a high of $195.95; its own minutes that day reached $123.10.)
4. **Built from the wrong rules**: a bar built here (from ticks, or finer bars) whose build was
   wrong, found when it is rebuilt: the old version goes to the pruned list, the rebuilt one takes
   its place. (2026-10-25: Massive's condition id 2, Average Price Trade, took the rule of the tape
   letter '2' and set highs and lows; NVDA after hours showed $177 to $251 in a minute at $231.
   `pruned repair-ticks --feed massive` rebuilt every bar built from Massive's ticks.)
5. **Out of line with the other feeds** (specified, not built yet): a feed's bar whose high or low
   lies beyond the other feeds' range for the same time by more than 5 times the composite's
   deviation, where at least two other feeds hold that time.

Not bad data: trades the consolidated tape's rules keep from setting prices (odd lots, average-price
and cash-sale prints, ...). They are kept and counted in volume where the rules say so; they only do
not set the open, high, low or close of bars built from ticks (`ticks.py`), in either view. A bar a
feed corrects later is a revision (`bar_revisions`), not bad data. A spike with volume behind it is a
real move (NVDA pre-market 2025-05-07 04:00:00: 133 trades in one second from $113.92 to $114.93, the
book opening). The thresholds of rules 2, 3 and 5 are constants in `baddata.py`.

**Every fetch is verified.** No program can make a feed's numbers right; what is guaranteed is that
nothing a fetch stores reaches the filtered data unchecked. Right after a symbol's fetch
(`collector.verify_fetched`), the bars it stored are checked by rule 3; each failing bar is asked
for once more (at most 10 requests a symbol, inside the run's caps): a different answer is stored as
a correction (the first kept in `bar_revisions`), the same answer is bad data at the source and the
check that follows prunes it. Every span the fetch updated, and every bar still failing, is queued
for a second feed's audit (`audit.py`: a random hour of minutes, two months of days), so auditors run
against what was just updated, not only the newest session.

## Memory and disk (2026-10-25)

The databases stay on disk; RAM holds what is being drawn or built, in batches, and lets it go.
(2026-10-25: the chart app's live check counted a stock's every bar and trade each time a file
changed, about every two seconds while the sync and the live recorder wrote: 31 GB of
`massive-ticks.db` stayed active in Windows' file cache and the chart app read 76 MB/s at rest. The
live recorder, whose "what changed since" read every bar's `stored_at`, had read 1.7 TB.)

- **Windows, not whole histories.** The auto-ranging chart reads the visible window and what its
  indicators and models need beside it (`gui/window.py`). A whole history is read only at the level
  drawn: a calendar level's bars, or the level a price-based chart builds whole. The page reads the
  next finer level ahead for a smooth change of level. Still on the whole-history series path:
  continuous futures, the composite model, box plots and the intraday integral.
- **The change log.** Whether a series changed, and what changed since a moment, come from each feed
  file's `changes` table (per series and minute written: the earliest and latest bar time, rows
  written), kept by triggers on `bars` and `built_bars`, so every writer keeps it whatever its code
  version. A seek, not a read of the series: the live chart's check, derive's and the bad-data
  scan's "since the last build", the fetch check's "what this fetch stored". It is complete from
  `changes_since` and kept a week (`CHANGES_KEEP_MS`); a reader further behind reads `stored_at`, once.
- **Bars-only mode** (`BARS_ONLY`, on by default). The app works from bars: no process opens the
  feeds' tick files but the live recorder, which records the streams' trades and builds their bars.
  The chart app, the sync, audits and checks never read them; a volume profile is spread from the
  finest bars instead (1-second bars where held, for bars up to a day); commands that would read ticks (exports of trades, tick charts, tick backups,
  `pruned repair-ticks`) say so and stop. The tick files are kept, never touched.
- **Caps.** The chart app's caches share one budget (`CHART_CACHE_MB`, 1 GB: whole histories 40 %,
  styled frames 30 %, bricks and 30-minute bins 15 % each; they had 4 GB between them, and the app
  once grew to 13 GB). At most two requests build chart data at once (`HEAVY_REQUESTS`); the rest wait
  up to 30 s, then are answered busy (503). The live recorder holds at most 64 MB of trades while the
  database refuses writes. numpy loads with one thread: OpenBLAS committed 757 MB per process, per
  CPU thread's buffer, almost none of it used.
- **Measured apart.** `python -m massive_collate admin memory` shows each app process's own memory
  (private bytes, working set) and disk reads, apart from Windows' file cache: active (held) and
  standby (handed back whenever anything needs it, counted as available).

## State (2026-10-24)

Already separated: data sources behind one interface (`sources/`), coverage planning
(`coverage.py`), audits (`audit.py`), the composite model and quality (`composite.py`).

Too broad: `storage.py` (≈2,000 lines: feed files, ticks, coverage ledger, audits, quality,
directory and listings, datasets, news, icons, layouts, runs) and `collector.py` (≈1,100 lines: bars,
ticks, futures, directory and icons, calendars, conditions, news, audit scheduling).

Calculations still in the GUI: resampling and calendar grouping (`gui/bars.py`), chart types such as
Renko (`gui/charttypes.py`), statistics (`gui/stats.py`). `gui/projection.py` (the time axis after
the last bar: where future bars will start; not a price forecast) imports private functions of
`gui/bars.py`.

Look-ahead: the derivative model's centred Savitzky-Golay window uses later bars. Nothing feeds it to
a backtest yet; there is no pattern engine in the repository yet.

Lost history: bar and dataset upserts overwrite values; one-file datasets are replaced whole on
refresh, so the French library's revisions are lost.

## Steps

1. **Derivatives out of the GUI, with a causal mode.** Done: `calc/derivatives.py`; `Model.causal`
   (trailing window, value at t from data up to t; tests prove later data never changes it). The
   chart keeps the centred window for viewing.
2. **Split `storage.py` by responsibility.** Done: `storage/` is a package; the `Store` composes one
   part per responsibility (feeds, watchlist, ledger, audits, markets, directory, datasets, app
   state, maintenance; shared schema and helpers), each moved verbatim in its own commit.
3. **Point-in-time storage.** Datasets done: every version of every value with `available_at`,
   `stored_at` and `origin`; revisions and withdrawals are versions; `as_of` reads; raw downloads
   kept locally by checksum; GitHub-hosted datasets are pinned git submodules whose commit history
   is imported version by version. Feed bars done: `stored_at` per bar; a re-download with the same
   values changes nothing, a correction keeps the replaced version in `bar_revisions` (not for the
   derived composite); `bars_window(as_of=, published=)` gives the series as it stood, gated by the
   plan's publication rule (`freshness.published_at`). Bars stored before tracking have no
   `stored_at` (taken as known since before tracking). Ticks: not versioned yet.
4. **Derived results with provenance.** Done: a `derivations` record (kind, product, code version as
   the git commit with "+dirty" for uncommitted changes, settings, inputs read, span, rows) for bars
   built from ticks (one per day built, with the tick count and a digest of the condition rules),
   composite builds, quality scores, audits and dataset quality priors; derived rows point to it.
   Bars built from ticks live apart from the feed's own in `built_bars` (reads combine them, built
   first); the composite has its own file; indicators (computed on request) return their settings,
   code version and whether they look ahead. `python -m massive_collate provenance [product] -v`.
   Bars built before provenance moved apart under "before-provenance" records.
5. **Calculations out of the GUI.** Resampling, calendar grouping, chart-type construction and
   statistics move to `calc/`; `gui/bars.py` becomes a thin adapter; the projection uses public
   `calc/` functions only.
6. **Split `collector.py` by job**: bars, ticks, futures, directory and icons, calendars and
   conditions, news; `sync` orchestrates.
7. **Backtest harness.** A simulated clock; data only through `as_of`; causal calculations only;
   each result records its inputs, code version and settings.
8. **Dataset prices in symbol composites** (Shiller S&P composite behind the S&P 500 index, IMF
   commodity prices behind their futures): built on 3–5, so the composite records which source and
   resolution supplies each epoch.

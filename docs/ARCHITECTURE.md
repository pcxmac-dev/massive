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
only left out of filtered reads, and a bar built here from the feed's finer data takes its place there
(unfiltered and `as_of` reads still give the feed's own); a bar built here that a rebuild replaced
lives on in the list alone, each rebuild's version kept. Pruning and releasing are changes like any
write: the frames built above the bar are rebuilt and the chart's caches see it.
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

## Schemes and replay (2026-10-25)

**Schemes** are trading algorithms run over a stretch of time on the Analysis view: the highlight
when one is drawn, otherwise what is in view, at the chart's level (1-second bars to years), in the
view the chart shows (filtered or all data). Built in, each with its settings; long and short. They
are backtests only: nothing places orders anywhere. (`calc/schemes.py`; `/api/scheme`; the Analysis
panel's Schemes section.)

Every scheme is causal: what it decides at a bar uses that bar and the bars before it, never a later
one. A bar's signal is known when the bar closes (the bar still forming is left out) and filled at
the next bar's open; a stop or target
fills inside a bar at its level (at the open when the bar opens beyond it; when one bar reaches both,
the stop is taken: the worse case). A position is a fixed quantity from its entry (equity at entry
over the entry price), so a trade's return is its side times its exit over its entry, less costs.
Costs: a fee and slippage in basis points of what is traded, charged at every entry and exit (a
reversal pays both). The run starts flat at the stretch's first bar; the bars before it are warm-up
only (each scheme's indicators and state, read from the store as far back as the scheme needs and no
further). A position still open at the stretch's end is marked at its last close and shown as open.
The test suite proves the causality: a run over any prefix of the bars gives the signals the full run
gave up to that point.

1. **ATR swing reversal** (stop and reverse). In an up-swing the highest high since the swing began
   is tracked; when a close falls more than `mult` × ATR(`atr`) below it, the swing has turned: short,
   and the lowest low is tracked from then on; the mirror turns it long. ATR is Wilder's average true
   range. Defaults: ATR 14, mult 3. Drawn: the reversal level each bar, and the swing highs and lows
   (each known only at the bar that confirmed it).
2. **Failed breakout reclaim.** The range is the highest high and lowest low of the `lookback` bars
   before a bar. A breakout is a close beyond the range by more than `depth` × ATR (or, with
   `breakout` = wick, a high or low beyond it); it fails when, within `window` bars, a close comes
   back inside the level it broke: long after a failed breakdown, short after a failed break up. The
   stop is beyond the failed breakout's extreme by `stop` × ATR; the target the range's far side as
   it was at the breakout. Exits: the stop, the target, or the opposite signal (a reversal).
   Defaults: lookback 20, window 3, depth 0, stop 0.25, ATR 14.
3. **CUSUM change detection** (Page's two-sided cumulative sum). Each bar's log return is scaled by
   the volatility of the returns before it (an exponentially weighted average of their squares, span
   `vol`); S+ adds up how far the scaled returns run above `k`, S− how far they run below −k, each
   floored at zero. When S+ passes `h` a shift up is declared (long), when S− does a shift down
   (short), and both start again from zero. Defaults: k 0.5, h 5, vol 50. Drawn: S+ and S− with h.

Results: the trades (side, entry and exit with times, prices and reason, return, bars held), the
equity curve and position per bar, and statistics (net return, buy and hold over the same bars, the
deepest drawdown, trades, share won, average win and loss, profit factor, time in a position).

**Replay** plays a symbol's bars from a start point as if they were arriving live. The start is a
bar picked on the chart; the chart shows only what was known then. Speeds 1×, 2×, 4×, 8×, 16× and
32× real market time; a gap longer than 15 minutes (nights, weekends, holidays, halts) passes in an
instant. The forming bar builds up from the finest bars held (1-second bars where the feed has them,
else minutes; each tagged with the chart bar it belongs to, binned by the server exactly as the
chart's bars are), so a candle grows as it did; each finer bar shows once its own time has passed.
Pause, step one finer bar, step one chart bar, or stop (back to the chart as it is now). Replay reads
a page of finer bars ahead (`/api/replay`), never the rest of the history. Schemes run during replay:
signals, positions, trades and the equity curve appear as the replay reaches them, each when the bar
that decided it closes: the scheme runs over the bars replayed and complete, so it sees no more than
it would have live.

## Trading (2026-10-25)

The Trading view (the rail) holds accounts at brokers, each a plugin behind one interface
(`trading/brokers.py`): its account (cash, equity, profit and loss), positions, orders and fills,
placing and cancelling an order. **Paper** is the first: a simulated account here, filled from the
stored bars by the backtests' rules. **Interactive Brokers** is the first real broker, planned and
not built; live trading stays off (`LIVE_TRADING`, off by default) until it is decided otherwise,
and then every order is placed by the user in this app, never by anything automatic without the
user's say-so.

Paper (`trading/paper.py`; its own tables in the main file): one account at a time, with a starting
cash and a fee in basis points; a reset starts a new account and keeps the old one's record. Orders:
buy or sell a quantity (selling more than is held goes short) at market, at a limit or on a stop,
for the day or until cancelled. Each working order is filled from the finest bars stored after it was
placed (1-second bars where held, else minutes), the first that reach it: a market order at the next
bar's open; a buy limit when a bar's low reaches it, at the limit or the open if the bar opened below
it (a sell limit the mirror); a buy stop when a bar's high reaches it, at the stop or the open if it
opened above (a sell stop the mirror). A day order not filled by its session's close expires, once
the stored bars reach that close (bars stored late still count); bars only a partial feed (IEX) holds
are looked at again for up to 2 days, until the whole-market feeds' bars replace them. Orders
are matched whenever the Trading view asks (and so after the fact, from the stored bars, when it was
closed: fills are what the bars allowed, whenever they are looked at). Positions are kept at their
average cost; realized profit and loss when a position is reduced, unrealized at the last stored price.

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

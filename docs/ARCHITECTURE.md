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

## Bad data (draft for review, 2026-10-25)

All source data is kept, always: received bars and ticks are never deleted or changed. Every series
has two views:

- **Unfiltered**: all data, everything stored, nothing taken out.
- **Filtered** (the target data, what charts and calculations use by default): unfiltered without
  the bad data.

The **pruned list** holds the bad data, one per feed and symbol: each entry is what was taken out of
the filtered view (a bar or a trade, as it was), the rule below that caught it, the evidence (the
numbers the rule compared), when it was pruned and by which code. An entry can be released (put
back) without losing the record that it was pruned. Pruning never mixes feeds: a feed's series is
judged on its own, other feeds' data serving only as evidence.

What constitutes bad data:

1. **Malformed**: a bar whose prices are not positive and finite, whose high is below its low or
   whose open or close lies outside them, whose volume is negative; a time in the future, or outside
   every session of the market (a stock print on a weekend or holiday).
2. **Spike without volume**: a bar whose range is at least 10 times the median range of its 20
   nearest neighbours (10 each side) in the same part of the session (regular hours against regular
   hours, after hours against after hours: they trade very different volumes), while its volume is at
   most 2 times their median volume, and whose open and close both lie within the neighbours' range
   (the price did not move on). A real move has volume behind it. The bar is pruned; the trades
   inside it are named in the evidence.
3. **Built from the wrong rules**: a bar built here (from ticks, or finer bars) whose build was
   wrong, found when it is rebuilt: the old version goes to the pruned list, the rebuilt one takes
   its place. (2026-10-25: Massive's condition id 2, Average Price Trade, took the rule of the tape
   letter '2' and set highs and lows; NVDA after hours showed $177 to $251 in a minute at $231.)
4. **Out of line with the other feeds**: a feed's bar whose high or low lies beyond the other feeds'
   range for the same time by more than 5 times the composite's deviation, where at least two other
   feeds hold that time.

Not bad data: trades the consolidated tape's rules keep from setting prices (odd lots, average-price
and cash-sale prints, ...). They are kept and counted in volume where the rules say so; they only do
not set the open, high, low or close of filtered bars (`ticks.py`). A bar a feed corrects later is a
revision (`bar_revisions`), not bad data. The thresholds in 2 and 4 are settings.

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

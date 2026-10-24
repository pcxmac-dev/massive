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
2. **Split `storage.py` by responsibility**, the `Store` class composing the parts and keeping its
   interface: feed files (bars, ticks), coverage ledger, audits and quality, directory and listings
   (with icons and reference details), datasets, app state (layouts, runs, kv). Pure moves.
3. **Point-in-time storage.** `available_at` (publication time by each source's rule) and
   `stored_at` on raw observations; corrections stored as new versions; an `as_of(t)` read path.
   Datasets first (revisions are routine there: undo the replace-on-refresh), then feed bars.
   Raw dataset files kept locally with checksums; GitHub-hosted datasets as pinned git submodules.
4. **Derived results with provenance.** A `derivations` record (inputs and their versions, code
   version, settings) for bars built from ticks, composites, quality scores, audits and indicators;
   derived values stored apart from raw ones.
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

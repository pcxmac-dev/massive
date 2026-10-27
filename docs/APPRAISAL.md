# Appraisal: modularity, scale, style (2026-10-24)

A read-only review of the whole codebase (494 tests passing at the time), spot-checked by hand:
the duplicated `clear_denied`, the change check that reads whole series (`bars_changed_since`),
the unbounded series cache in `BarEngine`, the Fetcher's state order disagreeing between page and
server, and the missing SQLite busy-timeout and sync settings were all confirmed. ★ marks the
five to do first in each part. Paths are relative to `src/massive_collate/`.

**Done since (2026-10-24, evening):** 1.★5's first step (the series cache is bounded); 1.8's
`proc.py`; 1.9's busy wait (30 s, `storage/common.py` `BUSY_TIMEOUT_S`; Python's sqlite3 already
waited 5 s, so this lengthens it rather than adding one); 2.★1 (`Store.kv_json`, and
`kv_update` under `BEGIN IMMEDIATE` for the priority requests, the open charts and the monthly
symbol places; `RECORDER_KEY`, `STATUS_KEY` and `WATCHLISTS_KEY` defined once, in `live.py`);
2.★3's `clear_denied` duplicate and the lock files (all through `proc.py`); 2.★4's state order
(the page's now matches the server's, with a test); 2.6's `start_scan` docstring, unused `tier =`
and `ranked.order` (now a parameter).
**Not done, on purpose:** `synchronous=NORMAL`. Bars and the coverage ledger are in different
files, so after a power cut NORMAL could keep the ledger's "held" while losing the bars it
describes: a hole that would never be fetched again. FULL stays until the ledger moves into the
feed files or a start-up check reconciles them.

## 1. Modularity and scale

**★1. A series catalog table instead of `stored_series()` scans** (medium). The list of stored
series is rebuilt by loose index scans of every feed file (`storage/feeds.py` `_stored_series`),
cached on a version every write changes; `coverage()` seeds from it for every call (`ledger.py`
`_seed_coverage`), and `coverage.plan` calls `coverage()` several times per symbol. During a sync
every stored page clears the cache. Change: a `series(source, ticker, kind, first_ts, last_ts,
dirty_from, updated_at)` table indexed by ticker, kept by the upserts; seed once, drop
`_seed_coverage`. Every per-symbol lookup becomes one indexed row.

**★2. Caches keyed on their own inputs; fast-changing state out of the main file** (small to
medium). The main DB's `data_version` changes every ~2 s (recorder heartbeat and status,
`KvLedger` on every API request, derive progress), so `daily_snapshot`, `company_profiles`,
`stored_series` and the trading calendar are effectively never cached. Change: heartbeats,
progress, waits and rate ledgers in a `runtime.db`; each cache keyed on what it reads.

**★3. Change detection that costs the change** (small). `bars_changed_since` filters
`stored_at > ?` inside the primary key, reading the whole series, after every sync (`derive.py`),
every minute in the recorder, and on each live tail refresh; `_fingerprint` counts a ticker's
ticks after each recorder flush, checked every 0.5 s per chart stream. Change: an index on
`(ticker, timespan, multiplier, stored_at)` or the catalog's `dirty_from`.

**★4. No per-symbol work on polled endpoints; no single lock** (medium). `symbol_rows` runs
several queries per watched symbol per poll; everything holds one `RLock` over one shared
connection, so chart requests queue behind Fetcher polls. Change: one SQL join over the
watchlist and the catalog; a read-only connection per server thread (WAL allows it).

**★5. Bound the series cache; stop loading whole histories** (small, then large).
`BarEngine._series` is a plain dict never emptied, each entry a full history (Alpaca to 2016 is
~2.4 M rows a stock). First bound it (`_BoundedCache` exists); then serve pages through
`bars_window` and read coarse frames from what `derive.py` stores.

6. **Split the collector by job; feeds in parallel** (large): `collector.sync` mixes reference,
archive, futures, stocks, audits and directory; `cli._sync` runs feeds one after another under one
run lock. Limits are per feed already, so per-feed locks are safe; pick due symbols in SQL.

7. **One registry per feed** (medium; done 2026-10-27: providers are plugins, `sources/registry.py`,
docs/ARCHITECTURE.md; the live streams still apart): a feed's facts live in eight places (`sources/__init__.py`
`rate_window`, `BUILDERS`, `ARCHIVAL`, `SUPPORTED_ASSETS`, `supports`, `feed_profiles`, `markets`;
`composite.py`; `config.py`; `live.py`; `gui/bars.py`). One `FeedSpec` each.

8. **Layering and import cycles** (small): `collector`/`deps` import `gui.world`; `gui/activity`
and `live` import `cli.pid_alive`; `audit`↔`collector` and `coverage`↔`collector`; 152 imports
inside functions hide these. Add `timeutil.py`, `plans.py`, `proc.py` (pids and locks).

9. **Storage settings, tick files, migrations** (small; tick split large): no `busy_timeout` or
`synchronous=NORMAL`; ticks one file per feed (~390 GB a year); migrations scattered across
opens and one-off kv flags. Numbered steps on `PRAGMA user_version`.

## 2. Style and code organization

**★1. One helper for JSON in kv, one list of keys** (small): 13 copies of
`json.loads(store.kv_get(K) or "{}")` with their own try/except; `RECORDER_KEY` and
`WATCHLISTS_KEY` defined twice; read-modify-write of kv (`request_priority`, `note_open`) not
atomic across processes. `kv_json(key, default)` and `kv_update(key, fn)` under `BEGIN IMMEDIATE`.

**★2. Shared time helpers** (small): `ET` defined 11 times (twice as a string); ms/date
conversions in five modules; `_iso` three times; `_ns` three times with opposite meanings.
One `timeutil.py`.

**★3. Duplicated logic** (small to medium): span subtraction twice (`coverage.subtract`,
`gui/profile_gaps.uncovered`); OHLCV aggregation four times (`derive.aggregate`,
`bars._aggregate`, `lod_aggregate`, `ticks.build_bars`); `clear_denied` = `clear_denied_days`;
cache insert-and-evict pasted five times in `bars.py`; three lock-file implementations
(`live.take_locks` not atomic, unlike `cli.run_lock`).

**★4. Front-end modules** (medium): `app.js` declares 179 globals that ten other scripts use
implicitly, so script order matters; 19 raw `fetch()` calls bypass `api()`; lists copied from the
server by hand (`LOD_TYPES`, `GPU_TYPES`, `TRANSFORMED`; the Fetcher's state order already
disagrees). ES modules without a build step, starting with `api.js` and `state.js`.

**★5. Tests** (small to medium): the `store` fixture redefined in four files; fakes in four
files; `sync_future` untested; `test_collector.py` 97 lines for a 1,337-line module; no JS tests;
no scale test. A `tests/fakes.py` and a query-counting fixture with 10k synthetic symbols.

6. **Stale or misleading code** (small): `start_scan`'s docstring after an `if` (a no-op string);
unused `tier =` in `sources/__init__.py`; `ranked.order` as hidden state on a function attribute;
`_Archive` as the base of live exchanges, `PublicClient` subclassing `MassiveClient`.

7. **Long functions** (small to medium): `make_handler` 186 lines with a 30-branch `do_GET` (a
route table); `deps.checks` 164; `build_parser` 150; chart types picked by an if/elif chain.

8. **Docs and lint** (small): README and ARCHITECTURE.md mention `storage.py`; PROGRESS.md's old
sections cite 169 tests and `gui/models.py`; no linter (165 lines over 120 characters): ruff in CI.
Strength: the plain-English docstrings that say why are good; keep them (393 of 701 public
definitions have one).

"""The main file's tables (what the feeds share) and the columns added to older databases."""

from __future__ import annotations


SCHEMA = """
-- SIP trade conditions and which bar fields each may update (consolidated rules), from the
-- source's reference data: bars built from ticks follow the same rules as the feeds' own bars.
CREATE TABLE IF NOT EXISTS conditions (
    source    TEXT    NOT NULL,
    id        INTEGER NOT NULL,
    name      TEXT,
    cta       TEXT,                -- the condition's letter on tapes A/B (CTA), if any
    utp       TEXT,                -- its letter on tape C (UTP), if any
    high_low  INTEGER,             -- updates the bar's high and low
    open_close INTEGER,            -- updates open and close
    volume    INTEGER,             -- counts in volume
    PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS exchanges (
    source      TEXT    NOT NULL,
    id          INTEGER NOT NULL,
    name        TEXT,
    acronym     TEXT,
    mic         TEXT,
    type        TEXT,
    country     TEXT,                -- ISO country code
    PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS watchlist (
    source        TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    resolution    TEXT NOT NULL DEFAULT 'auto',  -- auto | trades | second | minute
    backfilled_to TEXT,                          -- oldest date fully backfilled, NULL until done
    added_at      TEXT NOT NULL,
    exchange      TEXT,                          -- expected primary exchange MIC, NULL = any
    listed_from   TEXT,                          -- listing date of the current instrument
    figi          TEXT,                          -- composite FIGI of the current instrument
    checked_at    TEXT,                          -- date the listing was last checked
    PRIMARY KEY (source, ticker)
);

CREATE TABLE IF NOT EXISTS grouped_days (
    source     TEXT NOT NULL,
    day        TEXT NOT NULL,        -- YYYY-MM-DD
    status     TEXT NOT NULL,        -- ok | empty | denied
    bar_count  INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, day)
);

CREATE TABLE IF NOT EXISTS futures_contracts (
    source      TEXT NOT NULL,
    product     TEXT NOT NULL,       -- e.g. NQ
    ticker      TEXT NOT NULL,       -- e.g. NQZ6
    first_trade TEXT,
    last_trade  TEXT,
    PRIMARY KEY (source, ticker)
);

CREATE TABLE IF NOT EXISTS futures_rolls (
    source   TEXT NOT NULL,
    product  TEXT NOT NULL,
    rank     INTEGER NOT NULL,       -- 1 = most active
    session  TEXT NOT NULL,          -- session (settlement) date
    ticker   TEXT NOT NULL,          -- contract held for that session
    PRIMARY KEY (source, product, rank, session)
);

CREATE TABLE IF NOT EXISTS entitlements (
    source     TEXT NOT NULL,
    asset      TEXT NOT NULL,        -- stock | index | fx | future
    resolution TEXT NOT NULL,        -- trades | second | minute
    allowed    INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (source, asset, resolution)
);

CREATE TABLE IF NOT EXISTS instrument_details (
    source     TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    data       TEXT NOT NULL,        -- JSON reference record from the source
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, ticker)
);

CREATE TABLE IF NOT EXISTS layouts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    data       TEXT NOT NULL,        -- JSON: symbol, interval, chart type, indicators, drawings per ticker
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS icons (
    source     TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    url        TEXT,
    mime       TEXT,
    data       BLOB,                            -- NULL = the source had none (retried after 30 days)
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, ticker)
);

-- Official calendars from a source (override the rule-based calendar in sessions.py).
CREATE TABLE IF NOT EXISTS market_holidays (
    source   TEXT NOT NULL,
    day      TEXT NOT NULL,
    exchange TEXT NOT NULL,
    status   TEXT NOT NULL,                     -- closed | early-close
    open_ms  INTEGER,
    close_ms INTEGER,
    name     TEXT,
    PRIMARY KEY (source, day, exchange)
);

CREATE TABLE IF NOT EXISTS futures_schedule (
    source  TEXT NOT NULL,
    product TEXT NOT NULL,
    day     TEXT NOT NULL,                      -- session end (trading) date
    event   TEXT NOT NULL,                      -- open | close | pre_open | ...
    ts_ms   INTEGER NOT NULL,
    PRIMARY KEY (source, product, day, event, ts_ms)
);

-- News headlines (announcements ticker on the Markets globe).
CREATE TABLE IF NOT EXISTS news (
    source       TEXT NOT NULL,
    id           TEXT NOT NULL,
    published_ms INTEGER NOT NULL,
    title        TEXT NOT NULL,
    publisher    TEXT,
    url          TEXT,
    tickers      TEXT,                      -- JSON list
    PRIMARY KEY (source, id)
);
CREATE INDEX IF NOT EXISTS news_time ON news (source, published_ms);

-- Daily closes of the market proxy funds (from the whole-market daily file), for the globe's colours.
CREATE TABLE IF NOT EXISTS proxy_daily (
    source TEXT NOT NULL,
    day    TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close  REAL NOT NULL,
    PRIMARY KEY (source, day, ticker)
);

-- Every listing on the exchanges we cover (placeholders: no market data is collected for them
-- unless they are added to the watchlist), refreshed weekly.
CREATE TABLE IF NOT EXISTS directory (
    source   TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    kind     TEXT NOT NULL,                     -- stock | future
    name     TEXT,
    exchange TEXT,                              -- MIC (XNAS, XNYS) or futures venue
    type     TEXT,                              -- CS, ADRC, ETF... / futures sector
    figi     TEXT,
    seen     TEXT NOT NULL,                     -- date of the listing that included it
    PRIMARY KEY (source, ticker)
);

-- Changes to the listings, as each refresh finds them: listed (new), retired (gone from the active
-- listings), relisted (back again), reassigned (the symbol now names another instrument: new FIGI).
CREATE TABLE IF NOT EXISTS listing_events (
    source   TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    kind     TEXT NOT NULL,                     -- stock | future
    day      TEXT NOT NULL,
    event    TEXT NOT NULL,                     -- listed | retired | relisted | reassigned
    detail   TEXT,                              -- name, venue, old and new FIGI
    PRIMARY KEY (source, ticker, day, event)
);

-- The whole market's latest daily bar (close, volume, VWAP) per ticker, from the market-wide
-- daily file: ranks the directory and fills placeholder tiles. Only the newest days are kept.
CREATE TABLE IF NOT EXISTS market_snapshot (
    source TEXT NOT NULL,
    day    TEXT NOT NULL,
    ticker TEXT NOT NULL,
    close  REAL,
    volume REAL,
    vwap   REAL,
    PRIMARY KEY (source, day, ticker)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Which feed holds which span of a series. Each span [start, end) of a ticker's series was asked
-- for by exactly one feed, which stored whatever exists in it: other feeds never download it again
-- (only an audit may, without storing). Spans of one feed are kept merged.
CREATE TABLE IF NOT EXISTS coverage (
    source    TEXT    NOT NULL,
    ticker    TEXT    NOT NULL,
    timespan  TEXT    NOT NULL,   -- trades | second | minute | day
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    origin    TEXT    NOT NULL DEFAULT 'download',  -- download | ticks (built here from its ticks) | none (asked: this feed has nothing there)
    PRIMARY KEY (source, ticker, timespan, start_ms)
);
CREATE INDEX IF NOT EXISTS coverage_series ON coverage (ticker, timespan);
CREATE INDEX IF NOT EXISTS watchlist_ticker ON watchlist (ticker);

-- Audits: a second feed's copy of a stored span, compared in memory (see audit.py). Only the
-- comparison is kept, never the second copy.
CREATE TABLE IF NOT EXISTS audits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT    NOT NULL,
    ticker    TEXT    NOT NULL,
    asset     TEXT,
    timespan  TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    owner     TEXT    NOT NULL,   -- the feed whose stored data was checked
    auditor   TEXT    NOT NULL,   -- the feed asked for a second copy
    reason    TEXT,               -- after-download | random | manual
    verdict   TEXT    NOT NULL,   -- pass | warn | fail | no-data
    matched   INTEGER, only_owner INTEGER, only_auditor INTEGER,
    report    TEXT                -- JSON: box stats and pass rates per field, offsets, worst bars
);

-- How far a source can be trusted for display and reporting (0-1), per asset class: measured for
-- exchanges (leave-one-out against the others), else a prior by provenance.
CREATE TABLE IF NOT EXISTS quality (
    subject   TEXT    NOT NULL,  -- a feed, an exchange or a dataset
    asset     TEXT    NOT NULL,
    score     REAL    NOT NULL,
    details   TEXT,              -- JSON: what the score was measured from
    updated   TEXT    NOT NULL,
    PRIMARY KEY (subject, asset)
);

-- How each derived result was made (provenance.py): bars built from ticks, composites, quality
-- scores, audits. The results point here by id; raw observations have no derivation.
CREATE TABLE IF NOT EXISTS derivations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL,   -- bars-from-ticks | composite | quality | quality-prior | audit
    product    TEXT    NOT NULL,   -- what was made, e.g. alpaca:BTCUSD:minute
    created_at TEXT    NOT NULL,
    code       TEXT    NOT NULL,   -- code version: git commit (+dirty), or "before-provenance"
    settings   TEXT    NOT NULL,   -- JSON: the parameters used
    inputs     TEXT    NOT NULL,   -- JSON: what was read (sources, spans, counts, versions)
    span_lo    INTEGER,            -- the period it covers, ms
    span_hi    INTEGER,
    rows       INTEGER             -- results it made
);
CREATE INDEX IF NOT EXISTS derivations_product ON derivations (product, id);

-- Spans waiting to be audited by another feed (queued after a download, done in that feed's run).
CREATE TABLE IF NOT EXISTS audit_queue (
    ticker    TEXT    NOT NULL,
    timespan  TEXT    NOT NULL,
    start_ms  INTEGER NOT NULL,
    end_ms    INTEGER NOT NULL,
    owner     TEXT    NOT NULL,
    queued_at TEXT    NOT NULL,
    PRIMARY KEY (ticker, timespan, start_ms, owner)
);

-- File versions of the research datasets (datasets.py): when each was fetched, whether it is closed
-- (final: never fetched again), its checksum, the time its source published it, where the copy is
-- kept, and what it is (a git commit, or the sha256 of a download). Values: datasets/<provider>.db.
CREATE TABLE IF NOT EXISTS dataset_files (
    dataset    TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    bytes      INTEGER,
    values_n   INTEGER,
    fetched_at TEXT    NOT NULL,
    closed     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset, url)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    requests    INTEGER NOT NULL DEFAULT 0,
    rows        INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    message     TEXT
);
"""

# Columns added after the first release: (table, column, type). Applied to older databases on open.
MIGRATIONS = [
    ("coverage", "origin", "TEXT NOT NULL DEFAULT 'download'"),
    # A closed feed's symbol: everything it should hold is in; it is not asked again.
    ("watchlist", "closed_at", "TEXT"),
    ("watchlist", "exchange", "TEXT"),
    ("watchlist", "listed_from", "TEXT"),
    ("watchlist", "figi", "TEXT"),
    ("watchlist", "checked_at", "TEXT"),
    ("watchlist", "name", "TEXT"),
    ("watchlist", "kind", "TEXT"),
    ("watchlist", "asset", "TEXT NOT NULL DEFAULT 'stock'"),
    ("watchlist", "source_ticker", "TEXT"),
    ("watchlist", "invert", "INTEGER NOT NULL DEFAULT 0"),
    ("watchlist", "rank", "INTEGER NOT NULL DEFAULT 1"),
    # Save order: clocks can tie (Windows), a counter cannot.
    ("layouts", "touched", "INTEGER NOT NULL DEFAULT 0"),
    # The symbol a running sync is working on, for the on-screen activity view.
    ("runs", "current", "TEXT"),
    # Last scan: when the source was asked for its newest bar, and what it had (ms).
    ("watchlist", "probed_at", "TEXT"),
    ("watchlist", "source_through", "INTEGER"),
    # Futures contracts: live | ended | missing (no such contract month); checked = last probe date
    ("futures_contracts", "state", "TEXT"),
    ("futures_contracts", "checked", "TEXT"),
    # When a symbol last finished syncing completely (for the per-symbol refresh interval).
    ("watchlist", "synced_at", "TEXT"),
    # Directory listings: first seen, and the day a listing left the active list (kept, not deleted).
    ("directory", "first_seen", "TEXT"),
    ("directory", "retired_on", "TEXT"),
    # A watched symbol no longer listed: its history is still filled, no new data is expected.
    ("watchlist", "retired_on", "TEXT"),
    # Dataset file versions: checksum, publication time, origin (commit or sha256), local copy.
    ("dataset_files", "sha256", "TEXT"),
    ("dataset_files", "version_at", "TEXT"),
    ("dataset_files", "origin", "TEXT"),
    ("dataset_files", "local_path", "TEXT"),
    # Derived results point to the derivation record that made them.
    ("quality", "derivation", "INTEGER"),
    ("audits", "derivation", "INTEGER"),
]

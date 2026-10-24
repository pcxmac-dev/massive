"""Local SQLite store for ticks, bars, exchanges, the watchlist and an audit log of runs.

Each feed's market data has database files of its own (feeds/<feed>.db for its bars,
feeds/<feed>-ticks.db for its trades), and so has each research dataset provider
(datasets/<provider>.db), so one provider's data can be backed up, checked or removed apart from
the others. The main file keeps what they share: the watchlist, the coverage ledger, the ledger of
dataset files, audits, quality scores, the directory and the chart app's state.

The Store is one object over all of them; its methods come from one part per responsibility, a
module each in this package (feed files and market data, the watchlist, the coverage ledger,
audits and quality, markets reference data, the directory, datasets, app state, maintenance).
"""
from __future__ import annotations

from .store import (
    BAR_COLUMNS, COMPOSITE, COMPOSITE_SCHEMA, DATASET_SCHEMA, FEED_SCHEMA, LayoutLimit, OldLayout, Store,
    TICKS_SCHEMA, datasets_backup_dir, feeds_backup_dir, old_layout)

__all__ = [
    'BAR_COLUMNS',
    'COMPOSITE',
    'COMPOSITE_SCHEMA',
    'DATASET_SCHEMA',
    'FEED_SCHEMA',
    'LayoutLimit',
    'OldLayout',
    'Store',
    'TICKS_SCHEMA',
    'datasets_backup_dir',
    'feeds_backup_dir',
    'old_layout',
]

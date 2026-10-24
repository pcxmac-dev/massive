"""Provenance of derived results: what each was computed from, with which code and settings.

Raw observations (a feed's bars and ticks, dataset values) are kept as their sources published
them. Everything computed here from them (bars built from ticks, the composite model, quality
scores, audits, indicators) is a derived result, kept apart from the raw data and pointing to a
derivation record: its kind, the product, the code version (git commit; "+dirty" when the source
had uncommitted changes), the settings and the inputs read (sources, spans, counts, versions).
"""
from __future__ import annotations

import functools
import hashlib
import json
import subprocess
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parents[2]


@functools.cache
def code_version() -> str:
    """The code that computes: the repository's commit, "+dirty" if src/ has uncommitted changes;
    the package version where there is no repository."""
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=20, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no", "--", "src"],
                               capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return f"massive-collate {__version__}"
    return sha[:12] + ("+dirty" if dirty else "") if sha else f"massive-collate {__version__}"


def digest(value) -> str:
    """A short, stable fingerprint of settings too long to record whole (e.g. condition rules)."""
    blob = json.dumps(value, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def record(store, kind: str, product: str, settings: dict, inputs: dict, span: tuple[int, int] | None = None,
           rows: int | None = None) -> int:
    """Record one derivation (with the running code's version); returns its id for the results."""
    return store.record_derivation(kind, product, code_version(), settings, inputs, span, rows)

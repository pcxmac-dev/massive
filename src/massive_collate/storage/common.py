"""Small helpers shared by the storage modules: clocks, dates and key scans."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


_ET = ZoneInfo("America/New_York")

# How long a connection waits for another process's write before failing with "database is
# locked": the hourly sync, the live recorder and the chart app write the same files.
BUSY_TIMEOUT_S = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_precise() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _date_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


FEED_NAME = re.compile(r"[A-Za-z0-9_]+(-[A-Za-z0-9_]+)*")


def _feed_name(name: str) -> bool:
    return bool(FEED_NAME.fullmatch(name)) and not name.endswith("-ticks")


def _distinct(conn: sqlite3.Connection, table: str, cols: tuple[str, ...], rest: tuple[str, ...]) -> list[tuple]:
    """Distinct values of a primary-key prefix `cols`. Each step seeks past the whole key
    (`rest` = the remaining integer key columns, set to their maximum), so rows sharing a
    prefix are skipped in the index rather than read."""
    names = ", ".join(cols)
    key = ", ".join(cols + rest)
    top = (2**63 - 1,) * len(rest)
    out: list[tuple] = []
    row = conn.execute(f"SELECT {names} FROM {table} ORDER BY {names} LIMIT 1").fetchone()
    while row is not None:
        out.append(tuple(row))
        row = conn.execute(
            f"SELECT {names} FROM {table} WHERE ({key}) > ({', '.join('?' * (len(cols) + len(rest)))}) "
            f"ORDER BY {key} LIMIT 1", tuple(row) + top).fetchone()
    return out

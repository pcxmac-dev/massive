"""The chart app's saved layouts, the log of runs and the small key-value table."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .common import _now, _now_precise


class LayoutLimit(Exception):
    """The maximum number of saved layouts has been reached."""


def _layout_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("layout name must be 1-60 characters")
    return name


def _profile_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name or len(name) > 40:
        raise ValueError("a profile's name must be 1-40 characters")
    return name


class AppStateStore:
    """Store methods: the chart app's saved layouts, the log of runs and the small key-value table."""

    def kv_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self._wrote()

    def kv_json(self, key: str, default: Any = None) -> Any:
        """A kv value stored as JSON; `default` when it is missing, empty, unreadable or of
        another type than `default` (a list where a dict is kept ...). A None default takes any."""
        raw = self.kv_get(key)
        if not raw:
            return default
        try:
            value = json.loads(raw)
        except ValueError:
            return default
        return value if default is None or isinstance(value, type(default)) else default

    def kv_update(self, key: str, fn: Callable[[Any], Any], default: Any = None) -> Any:
        """Read, change and write a JSON kv value in one write transaction (BEGIN IMMEDIATE): a
        writer in another process waits, so neither loses the other's change. `fn` gets the
        value (as kv_json reads it) and returns the new one; returns that."""
        with self.conn:
            if not self.conn.in_transaction:
                self.conn.execute("BEGIN IMMEDIATE")
            value = fn(self.kv_json(key, default))
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
        self._wrote()
        return value

    # -- profiles (no passwords) -------------------------------------------------
    # Each keeps its own watchlists (live.watchlists_key), layouts, preferences (the page's, under a
    # prefix of its own) and register of the stocks it opened (visits). The first, "Default", holds
    # what came before profiles; a new one starts empty. The fetcher follows the signed-in one's
    # watchlists (the one signed in last: ACTIVE_PROFILE_KEY).

    FIRST_PROFILE = 1
    ACTIVE_PROFILE_KEY = "profiles:active"

    def profiles(self) -> list[dict]:
        """Every profile, the most recently signed in first."""
        self._first_profile()
        cur = self.conn.execute("SELECT id, name, created_at, seen_at FROM profiles "
                                "ORDER BY seen_at IS NULL, seen_at DESC, id")
        return [{"id": i, "name": n, "created_at": c, "seen_at": s} for i, n, c, s in cur]

    def profile(self, profile_id: int) -> dict | None:
        self._first_profile()
        row = self.conn.execute("SELECT id, name, created_at, seen_at FROM profiles WHERE id=?",
                                (profile_id,)).fetchone()
        return dict(zip(("id", "name", "created_at", "seen_at"), row)) if row else None

    def _first_profile(self) -> None:
        if self.conn.execute("SELECT 1 FROM profiles LIMIT 1").fetchone() is None:
            with self.conn:
                self.conn.execute("INSERT OR IGNORE INTO profiles (id, name, created_at) VALUES (?, ?, ?)",
                                  (self.FIRST_PROFILE, "Default", _now()))

    def create_profile(self, name: str) -> dict:
        name = _profile_name(name)
        self._first_profile()
        with self.conn:
            if self.conn.execute("SELECT 1 FROM profiles WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                raise ValueError(f"a profile named {name} already exists")
            cur = self.conn.execute("INSERT INTO profiles (name, created_at) VALUES (?, ?)", (name, _now()))
        return self.profile(cur.lastrowid)

    def rename_profile(self, profile_id: int, name: str) -> dict | None:
        name = _profile_name(name)
        with self.conn:
            if self.conn.execute("SELECT 1 FROM profiles WHERE name=? COLLATE NOCASE AND id<>?",
                                 (name, profile_id)).fetchone():
                raise ValueError(f"a profile named {name} already exists")
            n = self.conn.execute("UPDATE profiles SET name=? WHERE id=?", (name, profile_id)).rowcount
        return self.profile(profile_id) if n else None

    def sign_in(self, profile_id: int) -> dict | None:
        """The profile using the app now: its seen time, and the fetcher follows its watchlists."""
        found = self.profile(profile_id)
        if found is None:
            return None
        with self.conn:
            self.conn.execute("UPDATE profiles SET seen_at=? WHERE id=?", (_now(), profile_id))
        self.kv_set(self.ACTIVE_PROFILE_KEY, json.dumps(profile_id))
        return self.profile(profile_id)

    def active_profile(self) -> int:
        """The profile signed in last (the first when none has been)."""
        pid = self.kv_json(self.ACTIVE_PROFILE_KEY, None)
        return pid if isinstance(pid, int) and self.profile(pid) else self.FIRST_PROFILE

    def visit(self, profile_id: int, ticker: str, now: datetime | None = None) -> None:
        """Note a stock opened in the chart in the profile's register."""
        at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "INSERT INTO visits (profile, ticker, first_at, last_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (profile, ticker) DO UPDATE SET last_at=excluded.last_at, count=count + 1",
                (profile_id, ticker.upper(), at, at))

    def visits(self, profile_id: int, limit: int = 1000) -> list[dict]:
        """A profile's register, the most recently opened first."""
        cur = self.conn.execute("SELECT ticker, first_at, last_at, count FROM visits WHERE profile=? "
                                "ORDER BY last_at DESC, ticker LIMIT ?", (profile_id, limit))
        return [{"ticker": t, "first_at": f, "last_at": la, "count": c} for t, f, la, c in cur]

    # -- chart layouts (saved markups): each profile's own ----------------------

    MAX_LAYOUTS = 5

    def list_layouts(self, profile: int = 1) -> list[dict]:
        cur = self.conn.execute("SELECT id, name, updated_at FROM layouts WHERE profile=? "
                                "ORDER BY touched DESC, id DESC", (profile,))
        return [{"id": i, "name": n, "updated_at": u} for i, n, u in cur]

    def get_layout(self, layout_id: int, profile: int = 1) -> dict | None:
        row = self.conn.execute("SELECT id, name, data, updated_at FROM layouts WHERE id=? AND profile=?",
                                (layout_id, profile)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "data": json.loads(row[2]), "updated_at": row[3]}

    def create_layout(self, name: str, data: dict, profile: int = 1) -> dict:
        name = _layout_name(name)
        with self.conn:
            count = self.conn.execute("SELECT COUNT(*) FROM layouts WHERE profile=?", (profile,)).fetchone()[0]
            if count >= self.MAX_LAYOUTS:
                raise LayoutLimit(f"at most {self.MAX_LAYOUTS} layouts; delete one first")
            cur = self.conn.execute(
                "INSERT INTO layouts (name, data, updated_at, touched, profile) "
                "VALUES (?, ?, ?, (SELECT COALESCE(MAX(touched), 0) + 1 FROM layouts), ?)",
                (name, json.dumps(data), _now_precise(), profile))
        return self.get_layout(cur.lastrowid, profile)

    def update_layout(self, layout_id: int, name: str | None = None, data: dict | None = None,
                      profile: int = 1) -> dict | None:
        sets = ["updated_at=?", "touched=(SELECT COALESCE(MAX(touched), 0) + 1 FROM layouts)"]
        params: list = [_now_precise()]
        if name is not None:
            sets.append("name=?")
            params.append(_layout_name(name))
        if data is not None:
            sets.append("data=?")
            params.append(json.dumps(data))
        with self.conn:
            n = self.conn.execute(f"UPDATE layouts SET {', '.join(sets)} WHERE id=? AND profile=?",
                                  (*params, layout_id, profile)).rowcount
        return self.get_layout(layout_id, profile) if n else None

    def delete_layout(self, layout_id: int, profile: int = 1) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM layouts WHERE id=? AND profile=?", (layout_id, profile)).rowcount > 0

    # -- runs / audit ------------------------------------------------------

    def start_run(self, source: str | None, command: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (source, command, started_at) VALUES (?, ?, ?)", (source, command, _now())
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, requests: int, rows: int, status: str, message: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, requests=?, rows=?, status=?, message=? WHERE id=?",
                (_now(), requests, rows, status, message, run_id),
            )

    def set_run_requests(self, run_id: int, requests: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET requests=? WHERE id=?", (requests, run_id))

    def set_run_current(self, run_id: int, ticker: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET current=? WHERE id=?", (ticker, run_id))

    def close_abandoned_runs(self, source: str | None = None) -> int:
        """Runs still marked 'running' (crashed or killed): `source`'s only (the feeds run side
        by side, each closing its own when it holds its lock). Their recorded requests are kept;
        nothing is lowered, so the daily cap stays conservative."""
        where, args = ("status='running'", (_now(),)) if source is None else \
            ("status='running' AND source=?", (_now(), source))
        with self.conn:
            return self.conn.execute(
                "UPDATE runs SET status='abandoned', finished_at=COALESCE(finished_at, ?), "
                f"message=COALESCE(message, '') || ' (run did not finish)' WHERE {where}", args
            ).rowcount

    EVENT_DAYS = 30  # feed_events older than this are dropped as new ones come

    def feed_event(self, feed: str, kind: str, outcome: str, seconds: float | None = None, detail: str = "",
                   now: datetime | None = None) -> None:
        """Record one outcome of a feed's live connection or fetch run (the evidence the kill
        policy and the `feeds` report work from)."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (now - timedelta(days=self.EVENT_DAYS)).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute("INSERT INTO feed_events VALUES (?, ?, ?, ?, ?, ?)",
                              (now.isoformat(timespec="seconds"), feed, kind, outcome, seconds, detail[:300]))
            self.conn.execute("DELETE FROM feed_events WHERE feed=? AND kind=? AND at < ?", (feed, kind, cutoff))

    def feed_events(self, feed: str | None = None, since: datetime | None = None, limit: int = 200) -> list[dict]:
        """Recorded outcomes, newest first."""
        sql, args = "SELECT at, feed, kind, outcome, seconds, detail FROM feed_events WHERE 1=1", []
        if feed:
            sql += " AND feed=?"
            args.append(feed)
        if since:
            sql += " AND at >= ?"
            args.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
        rows = self.conn.execute(sql + " ORDER BY at DESC, rowid DESC LIMIT ?", [*args, limit]).fetchall()
        return [dict(zip(("at", "feed", "kind", "outcome", "seconds", "detail"), r)) for r in rows]

    def feed_scores(self, since: datetime) -> list[dict]:
        """Per feed and kind since `since`: how many of each outcome, and how long live
        connections held (median, of those that ended)."""
        out: dict[tuple[str, str], dict] = {}
        for feed, kind, outcome, n in self.conn.execute(
                "SELECT feed, kind, outcome, COUNT(*) FROM feed_events WHERE at >= ? GROUP BY 1, 2, 3",
                (since.astimezone(timezone.utc).isoformat(timespec="seconds"),)):
            out.setdefault((feed, kind), {"feed": feed, "kind": kind, "outcomes": {}})["outcomes"][outcome] = n
        for (feed, kind), row in out.items():
            held = [s for (s,) in self.conn.execute(
                "SELECT seconds FROM feed_events WHERE feed=? AND kind=? AND outcome IN ('dropped', 'refused') "
                "AND seconds IS NOT NULL AND at >= ? ORDER BY seconds",
                (feed, kind, since.astimezone(timezone.utc).isoformat(timespec="seconds")))]
            row["medianHeld"] = held[len(held) // 2] if held else None
        return sorted(out.values(), key=lambda r: (r["feed"], r["kind"]))

    def count_request(self, source: str, n: int = 1, now: datetime | None = None) -> None:
        """Count requests on the day (UTC) they are made, not the day their run began."""
        day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        with self.conn:
            self.conn.execute("INSERT INTO request_days (source, day, n) VALUES (?, ?, ?) "
                              "ON CONFLICT (source, day) DO UPDATE SET n = n + excluded.n", (source, day, n))

    def requests_today(self, source: str, now: datetime | None = None) -> int:
        """API requests made today (UTC) for a source: counted as each is made (a run that began
        before midnight books what it asks after midnight to the new day), and never less than
        what runs that began today recorded."""
        today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        by_run = self.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) FROM runs WHERE source=? AND substr(started_at, 1, 10)=?",
            (source, today),
        ).fetchone()[0]
        made = self.conn.execute("SELECT n FROM request_days WHERE source=? AND day=?", (source, today)).fetchone()
        return max(int(by_run), int(made[0]) if made else 0)

    def runs(self, limit: int = 25) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message, current "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur]

    def runs_going(self) -> list[dict]:
        """The runs still marked 'running' (one a feed at most while their processes live), newest first."""
        cur = self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message, current "
            "FROM runs WHERE status='running' ORDER BY id DESC LIMIT 50")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur]

    def last_runs(self) -> dict[str, dict]:
        """Each source's latest finished run (any age): {source: run}."""
        cur = self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message, current FROM runs "
            "WHERE id IN (SELECT MAX(id) FROM runs WHERE status != 'running' GROUP BY source)")
        cols = [c[0] for c in cur.description]
        return {row[1]: dict(zip(cols, row)) for row in cur}

    def recent_runs(self, limit: int = 10) -> list[tuple]:
        return self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()

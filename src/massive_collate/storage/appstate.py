"""The chart app's saved layouts, the log of runs and the small key-value table."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .common import _now, _now_precise


class LayoutLimit(Exception):
    """The maximum number of saved layouts has been reached."""


def _layout_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("layout name must be 1-60 characters")
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

    # -- chart layouts (saved markups) --------------------------------------

    MAX_LAYOUTS = 5

    def list_layouts(self) -> list[dict]:
        cur = self.conn.execute("SELECT id, name, updated_at FROM layouts ORDER BY touched DESC, id DESC")
        return [{"id": i, "name": n, "updated_at": u} for i, n, u in cur]

    def get_layout(self, layout_id: int) -> dict | None:
        row = self.conn.execute("SELECT id, name, data, updated_at FROM layouts WHERE id=?", (layout_id,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "data": json.loads(row[2]), "updated_at": row[3]}

    def create_layout(self, name: str, data: dict) -> dict:
        name = _layout_name(name)
        with self.conn:
            count = self.conn.execute("SELECT COUNT(*) FROM layouts").fetchone()[0]
            if count >= self.MAX_LAYOUTS:
                raise LayoutLimit(f"at most {self.MAX_LAYOUTS} layouts; delete one first")
            cur = self.conn.execute(
                "INSERT INTO layouts (name, data, updated_at, touched) "
                "VALUES (?, ?, ?, (SELECT COALESCE(MAX(touched), 0) + 1 FROM layouts))",
                (name, json.dumps(data), _now_precise()))
        return self.get_layout(cur.lastrowid)

    def update_layout(self, layout_id: int, name: str | None = None, data: dict | None = None) -> dict | None:
        sets = ["updated_at=?", "touched=(SELECT COALESCE(MAX(touched), 0) + 1 FROM layouts)"]
        params: list = [_now_precise()]
        if name is not None:
            sets.append("name=?")
            params.append(_layout_name(name))
        if data is not None:
            sets.append("data=?")
            params.append(json.dumps(data))
        with self.conn:
            n = self.conn.execute(f"UPDATE layouts SET {', '.join(sets)} WHERE id=?", (*params, layout_id)).rowcount
        return self.get_layout(layout_id) if n else None

    def delete_layout(self, layout_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM layouts WHERE id=?", (layout_id,)).rowcount > 0

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

    def close_abandoned_runs(self) -> int:
        """Runs still marked 'running' (crashed or killed). Their recorded requests are kept;
        nothing is lowered, so the daily cap stays conservative."""
        with self.conn:
            return self.conn.execute(
                "UPDATE runs SET status='abandoned', finished_at=COALESCE(finished_at, ?), "
                "message=COALESCE(message, '') || ' (run did not finish)' WHERE status='running'", (_now(),)
            ).rowcount

    def requests_today(self, source: str) -> int:
        """API requests recorded today (UTC) for a source, across all runs."""
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) FROM runs WHERE source=? AND substr(started_at, 1, 10)=?",
            (source, today),
        ).fetchone()
        return int(row[0])

    def runs(self, limit: int = 25) -> list[dict]:
        cur = self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message, current "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur]

    def recent_runs(self, limit: int = 10) -> list[tuple]:
        return self.conn.execute(
            "SELECT id, source, command, started_at, finished_at, requests, rows, status, message "
            "FROM runs ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()

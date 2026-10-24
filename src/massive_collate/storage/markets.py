"""Reference data about markets: exchanges, trade conditions, holidays and futures schedules, news, market proxies."""

from __future__ import annotations

import json
from datetime import date
from typing import Iterable, TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from ..models import Exchange


class MarketsStore:
    """Store methods: reference data about markets: exchanges, trade conditions, holidays and futures schedules, news, market proxies."""

    # -- exchanges ---------------------------------------------------------

    def upsert_exchanges(self, exchanges: Iterable["Exchange"]) -> int:
        data = [
            (e.source, e.id, e.name, e.acronym, e.mic, e.type, e.country.code if e.country else None)
            for e in exchanges
        ]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def exchanges(self, source: str | None = None) -> pd.DataFrame:
        sql = "SELECT source, id, name, acronym, mic, type, country FROM exchanges"
        params: list = []
        if source:
            sql += " WHERE source=?"
            params.append(source)
        return pd.read_sql_query(sql + " ORDER BY source, id", self.conn, params=params)

    # -- trade condition rules ------------------------------------------------

    def save_conditions(self, source: str, rows: list[dict]) -> int:
        with self.conn:
            self.conn.execute("DELETE FROM conditions WHERE source=?", (source,))
            self.conn.executemany("INSERT INTO conditions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                (source, r["id"], r.get("name"), r.get("cta"), r.get("utp"), int(bool(r.get("high_low"))),
                 int(bool(r.get("open_close"))), int(bool(r.get("volume")))) for r in rows])
        return len(rows)

    def condition_rules(self) -> dict[tuple[str, str], tuple[bool, bool, bool]]:
        """{(tape family 'cta'|'utp', letter): (updates high/low, open/close, volume)} from any source."""
        out = {}
        for cta, utp, hl, oc, vol in self.conn.execute(
                "SELECT cta, utp, high_low, open_close, volume FROM conditions"):
            for fam, letter in (("cta", cta), ("utp", utp)):
                if letter:
                    out[(fam, letter)] = (bool(hl), bool(oc), bool(vol))
        return out

    # -- official calendars --------------------------------------------------

    def save_market_holidays(self, source: str, rows: Iterable[dict]) -> int:
        data = [(source, r["date"], r["exchange"], r["status"], r.get("open_ms"), r.get("close_ms"), r.get("name"))
                for r in rows]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO market_holidays VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def market_holidays(self, source: str) -> list[tuple[date, str, int | None, int | None]]:
        """NYSE closures and early closes (Nasdaq where NYSE is not listed for a day)."""
        rows = self.conn.execute(
            "SELECT day, exchange, status, open_ms, close_ms FROM market_holidays WHERE source=? "
            "AND exchange IN ('NYSE', 'NASDAQ') ORDER BY day, exchange DESC", (source,)).fetchall()
        out: dict[str, tuple] = {}
        for day, exchange, status, o, c in rows:  # NYSE sorts after NASDAQ descending: NYSE wins
            if day not in out or exchange == "NYSE":
                out[day] = (date.fromisoformat(day), status, o, c)
        return list(out.values())

    def save_futures_schedule(self, source: str, product: str, rows: Iterable[tuple[str, str, int]]) -> int:
        """rows: (session date, event, ms). Replaces what was stored for those dates."""
        rows = list(rows)
        days = sorted({d for d, _, _ in rows})
        with self.conn:
            self.conn.executemany("DELETE FROM futures_schedule WHERE source=? AND product=? AND day=?",
                                  [(source, product.upper(), d) for d in days])
            self.conn.executemany("INSERT OR REPLACE INTO futures_schedule VALUES (?, ?, ?, ?, ?)",
                                  [(source, product.upper(), d, e, ms) for d, e, ms in rows])
        return len(rows)

    def futures_schedules(self, source: str) -> list[tuple[str, date, list[tuple[str, int]]]]:
        out: dict[tuple, list] = {}
        for product, day, event, ms in self.conn.execute(
                "SELECT product, day, event, ts_ms FROM futures_schedule WHERE source=? ORDER BY product, day, ts_ms",
                (source,)):
            out.setdefault((product, day), []).append((event, ms))
        return [(p, date.fromisoformat(d), ev) for (p, d), ev in out.items()]

    def save_news(self, source: str, items: Iterable[dict]) -> int:
        rows = [(source, str(i["id"]), int(i["published_ms"]), i["title"], i.get("publisher"), i.get("url"),
                 json.dumps(i.get("tickers") or [])) for i in items if i.get("id") and i.get("title")]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO news VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    def news(self, source: str | None = None, since_ms: int = 0, limit: int = 60) -> list[dict]:
        sql = "SELECT source, id, published_ms, title, publisher, url, tickers FROM news WHERE published_ms >= ?"
        params: list = [since_ms]
        if source:
            sql += " AND source=?"
            params.append(source)
        rows = self.conn.execute(sql + " ORDER BY published_ms DESC LIMIT ?", params + [limit]).fetchall()
        return [{"source": s, "id": i, "published_ms": t, "title": ti, "publisher": p, "url": u,
                 "tickers": json.loads(tk or "[]")} for s, i, t, ti, p, u, tk in rows]

    def latest_news_ms(self, source: str) -> int | None:
        return self.conn.execute("SELECT MAX(published_ms) FROM news WHERE source=?", (source,)).fetchone()[0]

    def save_proxy_closes(self, source: str, day: date, closes: dict[str, float]) -> int:
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO proxy_daily VALUES (?, ?, ?, ?)",
                                  [(source, day.isoformat(), t, c) for t, c in closes.items()])
        return len(closes)

    def proxy_closes(self, source: str, day: date) -> dict[str, float]:
        return dict(self.conn.execute("SELECT ticker, close FROM proxy_daily WHERE source=? AND day=?",
                                      (source, day.isoformat())).fetchall())

    def snapshot_closes(self, source: str, day: date, tickers: list[str]) -> dict[str, float]:
        marks = ",".join("?" * len(tickers))
        return dict(self.conn.execute(
            f"SELECT ticker, close FROM market_snapshot WHERE source=? AND day=? AND ticker IN ({marks}) AND close IS NOT NULL",
            (source, day.isoformat(), *tickers)).fetchall())

    def proxy_days(self, source: str) -> list[date]:
        return [date.fromisoformat(d) for (d,) in self.conn.execute(
            "SELECT DISTINCT day FROM proxy_daily WHERE source=? ORDER BY day", (source,))]

    def proxy_moves(self, source: str = "massive") -> dict[str, dict]:
        """Latest daily change of each proxy: {ticker: {day, close, changePct}}."""
        days = self.proxy_days(source)[-2:]
        if len(days) < 2:
            return {}
        prev, last = (dict(self.conn.execute("SELECT ticker, close FROM proxy_daily WHERE source=? AND day=?",
                                             (source, d.isoformat())).fetchall()) for d in days)
        return {t: {"day": days[1].isoformat(), "close": c, "changePct": (c / prev[t] - 1) * 100}
                for t, c in last.items() if prev.get(t)}

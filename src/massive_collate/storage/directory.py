"""The directory of listings (new, retired, relisted, reassigned), the market snapshot, company icons and reference details."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from .common import _now


class DirectoryStore:
    """Store methods: the directory of listings (new, retired, relisted, reassigned), the market snapshot, company icons and reference details."""

    def save_icon(self, source: str, ticker: str, url: str | None, mime: str | None, data: bytes | None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO icons VALUES (?, ?, ?, ?, ?, ?)",
                              (source, ticker.upper(), url, mime, data, _now()))

    def icon(self, ticker: str, source: str | None = None) -> tuple[str, bytes] | None:
        """(content type, bytes) of a stored company icon, from any source unless one is given."""
        sql = "SELECT mime, data FROM icons WHERE ticker=? AND data IS NOT NULL"
        params: list = [ticker.upper()]
        if source:
            sql += " AND source=?"
            params.append(source)
        row = self.conn.execute(sql + " ORDER BY fetched_at DESC LIMIT 1", params).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def icon_checked(self, source: str, ticker: str, max_age_days: int = 30, url: str | None = None) -> bool:
        """Whether an icon was fetched, or found missing recently enough not to ask again. With
        `url` (the logo link in the company's current details), a stored icon from a different
        link does not count: the company has a new logo."""
        row = self.conn.execute("SELECT data IS NOT NULL, fetched_at, url FROM icons WHERE source=? AND ticker=?",
                                (source, ticker.upper())).fetchone()
        if row is None:
            return False
        if row[0]:
            return url is None or row[2] == url
        return (datetime.now(timezone.utc) - datetime.fromisoformat(row[1])).days < max_age_days

    def forget_unasked_icons(self, source: str) -> int:
        """Drop "no icon" records of tickers whose details were never read (nothing was asked).
        Returns how many."""
        with self.conn:
            return self.conn.execute(
                "DELETE FROM icons WHERE source=? AND data IS NULL AND url IS NULL AND ticker NOT IN "
                "(SELECT ticker FROM instrument_details WHERE source=?)", (source, source)).rowcount

    def stale_details(self, source: str, tickers_types: tuple[str, ...], max_age_days: int, limit: int) -> list[str]:
        """Directory companies whose stored details (and so logo link) are older than max_age_days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(tickers_types))
        return [r[0] for r in self.conn.execute(
            f"""SELECT d.ticker FROM directory d JOIN instrument_details x ON x.source=d.source AND x.ticker=d.ticker
                WHERE d.source=? AND d.kind='stock' AND d.type IN ({marks}) AND x.updated_at < ?
                ORDER BY x.updated_at LIMIT ?""", (source, *tickers_types, cutoff, limit))]

    def icon_link_changes(self, source: str) -> list[str]:
        """Tickers whose stored icon came from a different logo link than their details now give."""
        out = []
        for ticker, url, data in self.conn.execute(
                "SELECT i.ticker, i.url, x.data FROM icons i JOIN instrument_details x "
                "ON x.source=i.source AND x.ticker=i.ticker WHERE i.source=? AND i.data IS NOT NULL", (source,)):
            current = ((json.loads(data).get("branding") or {}).get("icon_url"))
            if current and current != url:
                out.append(ticker)
        return out

    def icon_tickers(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT ticker FROM icons WHERE data IS NOT NULL")}

    # -- directory ------------------------------------------------------------

    # A refresh that would retire more than this share of a kind's active listings is taken for a
    # bad answer (a venue missing from the reply), not a wave of delistings: nothing is retired.
    MAX_RETIRED_SHARE = 0.05

    def update_directory(self, source: str, kind: str, rows: Iterable[dict], today: date) -> dict[str, list]:
        """The complete active listing of one kind (stock | future): listings are added or updated,
        those missing are marked retired (kept, with the day), and every change is logged in
        listing_events. Returns {listed, retired, relisted, reassigned, suspect}: tickers, with
        `suspect` true when retiring was skipped because too much would have gone at once."""
        day = today.isoformat()
        rows = {r["ticker"].upper(): r for r in rows if r.get("ticker")}
        before = {t: (figi, retired, name) for t, figi, retired, name in self.conn.execute(
            "SELECT ticker, figi, retired_on, name FROM directory WHERE source=? AND kind=?", (source, kind))}
        venues = {v for (v,) in self.conn.execute("SELECT DISTINCT exchange FROM directory WHERE source=? AND kind=?",
                                                  (source, kind))}
        active = {t for t, (_, retired, _) in before.items() if not retired}
        out = {"listed": [], "retired": [], "relisted": [], "reassigned": [], "suspect": False}
        events, start = [], set()  # start: a venue's first listing here, a starting point, not new listings
        for t, r in rows.items():
            if t not in before:
                if r.get("exchange") not in venues:
                    start.add(t)
                    continue
                out["listed"].append(t)
                events.append((t, "listed", f"{r.get('name') or ''} ({r.get('exchange') or ''})"))
                continue
            figi, retired, name = before[t]
            if retired:
                out["relisted"].append(t)
                events.append((t, "relisted", f"{r.get('name') or ''}, retired {retired}"))
            if figi and r.get("figi") and r["figi"] != figi:
                out["reassigned"].append(t)
                events.append((t, "reassigned", f"{name or ''} {figi} -> {r.get('name') or ''} {r['figi']}"))
        gone = sorted(active - set(rows))
        if active and len(gone) > self.MAX_RETIRED_SHARE * len(active):
            out["suspect"], gone = True, []
        out["retired"] = gone
        events += [(t, "retired", before[t][2] or "") for t in gone]
        with self.conn:
            self.conn.executemany(
                "INSERT INTO directory (source, ticker, kind, name, exchange, type, figi, seen, first_seen, retired_on) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) ON CONFLICT (source, ticker) DO UPDATE SET kind=excluded.kind, "
                "name=excluded.name, exchange=excluded.exchange, type=excluded.type, figi=COALESCE(excluded.figi, figi), "
                "seen=excluded.seen, retired_on=NULL",
                [(source, t, kind, r.get("name"), r.get("exchange"), r.get("type"), r.get("figi"), day,
                  None if t in start else day) for t, r in rows.items()])
            self.conn.executemany("UPDATE directory SET retired_on=? WHERE source=? AND ticker=?",
                                  [(day, source, t) for t in gone])
            self.conn.executemany("INSERT OR REPLACE INTO listing_events VALUES (?, ?, ?, ?, ?, ?)",
                                  [(source, t, kind, day, e, d) for t, e, d in events])
        self._wrote()
        return out

    def listing_events(self, source: str | None = None, since: date | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT source, ticker, kind, day, event, detail FROM listing_events WHERE 1=1", []
        if source:
            sql += " AND source=?"
            args.append(source)
        if since:
            sql += " AND day >= ?"
            args.append(since.isoformat())
        cols = ["source", "ticker", "kind", "day", "event", "detail"]
        return [dict(zip(cols, r)) for r in self.conn.execute(sql + " ORDER BY day DESC, ticker LIMIT ?", [*args, limit])]

    def directory(self, source: str = "massive", kind: str | None = None) -> list[dict]:
        sql = """SELECT d.ticker, d.kind, d.name, d.exchange, d.type, s.close, s.volume, s.vwap,
                        i.data IS NOT NULL AS has_icon
                 FROM directory d
                 LEFT JOIN market_snapshot s ON s.source=d.source AND s.ticker=d.ticker
                      AND s.day=(SELECT MAX(day) FROM market_snapshot WHERE source=d.source)
                 LEFT JOIN icons i ON i.source=d.source AND i.ticker=d.ticker
                 WHERE d.source=? AND d.retired_on IS NULL""" + (" AND d.kind=?" if kind else "") + " ORDER BY d.ticker"
        cols = ["ticker", "kind", "name", "exchange", "type", "close", "volume", "vwap", "icon"]
        return [dict(zip(cols, r)) for r in self.conn.execute(sql, [source] + ([kind] if kind else []))]

    def company_profiles(self, source: str = "massive") -> dict[str, dict]:
        """{ticker: {sic_code, sic_description, market_cap}} from stored reference records (one
        pass, cached until the database changes): the market map's sectors and sizes."""
        def compute():
            out = {}
            for ticker, data in self.conn.execute("SELECT ticker, data FROM instrument_details WHERE source=?", (source,)):
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                if d.get("sic_code") or d.get("market_cap"):
                    out[ticker] = {"sic_code": d.get("sic_code"), "sic_description": d.get("sic_description"),
                                   "market_cap": d.get("market_cap"),
                                   "class_shares": d.get("share_class_shares_outstanding")}
            return out
        return self._cached(("company_profiles", source), compute)

    def snapshot_changes(self, source: str = "massive") -> dict[str, float]:
        """Each ticker's change on the newest day of the whole-market file, in percent."""
        days = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT day FROM market_snapshot WHERE source=? ORDER BY day DESC LIMIT 2", (source,))]
        if len(days) < 2:
            return {}
        rows = self.conn.execute(
            "SELECT a.ticker, a.close, b.close FROM market_snapshot a JOIN market_snapshot b "
            "ON b.source=a.source AND b.ticker=a.ticker AND b.day=? WHERE a.source=? AND a.day=?",
            (days[1], source, days[0]))
        return {t: (c / p - 1) * 100 for t, c, p in rows if c and p}

    def directory_icon_queue(self, source: str, types: tuple[str, ...], limit: int) -> list[str]:
        """Companies still without an icon decision, biggest daily dollar volume first."""
        marks = ",".join("?" * len(types))
        sql = f"""SELECT d.ticker FROM directory d
                  LEFT JOIN icons i ON i.source=d.source AND i.ticker=d.ticker
                  LEFT JOIN market_snapshot s ON s.source=d.source AND s.ticker=d.ticker
                       AND s.day=(SELECT MAX(day) FROM market_snapshot WHERE source=d.source)
                  WHERE d.source=? AND d.kind='stock' AND d.type IN ({marks}) AND i.ticker IS NULL AND d.retired_on IS NULL
                  ORDER BY COALESCE(s.close * s.volume, 0) DESC, d.ticker LIMIT ?"""
        return [r[0] for r in self.conn.execute(sql, [source, *types, limit])]

    def directory_stats(self, source: str, types: tuple[str, ...]) -> dict:
        marks = ",".join("?" * len(types))
        q = self.conn.execute
        listed = q("SELECT kind, COUNT(*) FROM directory WHERE source=? AND retired_on IS NULL GROUP BY kind",
                   (source,)).fetchall()
        venues = q("SELECT exchange, COUNT(*) FROM directory WHERE source=? AND retired_on IS NULL AND kind='stock' "
                   "GROUP BY exchange", (source,)).fetchall()
        retired = q("SELECT COUNT(*) FROM directory WHERE source=? AND retired_on IS NOT NULL", (source,)).fetchone()[0]
        companies = q(f"SELECT COUNT(*) FROM directory WHERE source=? AND kind='stock' AND type IN ({marks}) "
                      "AND retired_on IS NULL", (source, *types)).fetchone()[0]
        decided = q(f"""SELECT COUNT(*), SUM(i.data IS NOT NULL) FROM directory d JOIN icons i
                        ON i.source=d.source AND i.ticker=d.ticker
                        WHERE d.source=? AND d.kind='stock' AND d.type IN ({marks}) AND d.retired_on IS NULL""",
                    (source, *types)).fetchone()
        return {"listed": dict(listed), "venues": dict(venues), "retired": retired, "companies": companies,
                "iconsChecked": decided[0] or 0, "icons": int(decided[1] or 0)}

    def listings_by_volume(self, source: str, exchanges: Sequence[str], by: str = "value",
                           skip_types: Sequence[str] = ()) -> list[dict]:
        """Active stock listings on `exchanges`, most traded first: average daily traded value
        (shares x VWAP) over the market snapshot's sessions, or shares with by="shares". Listings
        without a snapshot come last, by ticker. [{ticker, exchange, type, name, measure}]"""
        measure = "AVG(s.volume * COALESCE(s.vwap, s.close))" if by == "value" else "AVG(s.volume)"
        skip = f"AND COALESCE(d.type, '') NOT IN ({','.join('?' * len(skip_types))})" if skip_types else ""
        rows = self.conn.execute(
            f"""SELECT d.ticker, d.exchange, d.type, d.name, {measure} AS m FROM directory d
                LEFT JOIN market_snapshot s ON s.source=d.source AND s.ticker=d.ticker
                WHERE d.source=? AND d.kind='stock' AND d.retired_on IS NULL
                  AND d.exchange IN ({','.join('?' * len(exchanges))}) {skip}
                GROUP BY d.ticker ORDER BY m IS NULL, m DESC, d.ticker""",
            (source, *exchanges, *skip_types))
        return [{"ticker": t, "exchange": e, "type": ty, "name": n, "measure": m} for t, e, ty, n, m in rows]

    def futures_products(self, source: str, exchanges: Sequence[str]) -> list[dict]:
        """Futures products listed on `exchanges` (CME Group's): [{ticker (the product code), exchange,
        type (its sector), name}], by code. (No volume: the market snapshot is the stocks'; a stock
        sharing a product's code, CL, is Colgate-Palmolive.)"""
        rows = self.conn.execute(
            f"""SELECT ticker, exchange, type, name FROM directory
                WHERE source=? AND kind='future' AND retired_on IS NULL
                  AND exchange IN ({','.join('?' * len(exchanges))}) ORDER BY ticker""", (source, *exchanges))
        return [{"ticker": t, "exchange": e, "type": ty, "name": n} for t, e, ty, n in rows]

    def save_market_snapshot(self, source: str, day: date, bars: list[dict]) -> int:
        rows = [(source, day.isoformat(), b["T"].upper(), b.get("c"), b.get("v"), b.get("vw")) for b in bars if b.get("T")]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO market_snapshot VALUES (?, ?, ?, ?, ?, ?)", rows)
            keep = [d for (d,) in self.conn.execute(
                "SELECT DISTINCT day FROM market_snapshot WHERE source=? ORDER BY day DESC LIMIT 3", (source,))]
            if keep:
                self.conn.execute(f"DELETE FROM market_snapshot WHERE source=? AND day NOT IN ({','.join('?' * len(keep))})",
                                  (source, *keep))
        return len(rows)

    # -- reference details -------------------------------------------------

    def set_details(self, source: str, ticker: str, data: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO instrument_details VALUES (?, ?, ?, ?)",
                              (source, ticker.upper(), json.dumps(data), _now()))

    def details(self, source: str, ticker: str) -> dict | None:
        row = self.conn.execute("SELECT data, updated_at FROM instrument_details WHERE source=? AND ticker=?",
                                (source, ticker.upper())).fetchone()
        return {**json.loads(row[0]), "_updated_at": row[1]} if row else None

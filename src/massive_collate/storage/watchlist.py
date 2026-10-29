"""What is collected: the watchlist, listing checks, backfill and sync state, futures rolls, plan entitlements and probes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable

from .common import _now


class WatchlistStore:
    """Store methods: what is collected: the watchlist, listing checks, backfill and sync state, futures rolls, plan entitlements and probes."""

    # -- watchlist ---------------------------------------------------------

    def watch(
        self, source: str, tickers: Iterable[str], resolution: str = "auto", exchange: str | None = None,
        asset: str | None = None, source_ticker: str | None = None, invert: bool = False, rank: int = 1,
    ) -> None:
        """Add or update watchlist entries. `exchange` (a MIC) pins the expected primary exchange;
        `asset`/`source_ticker`/`invert`/`rank` describe non-stock symbols (see symbols.resolve)."""
        with self.conn:
            for t in tickers:
                self.conn.execute(
                    "INSERT INTO watchlist (source, ticker, resolution, added_at, exchange, asset, source_ticker, "
                    "invert, rank) VALUES (?, ?, ?, ?, ?, COALESCE(?, 'stock'), ?, ?, ?) "
                    "ON CONFLICT (source, ticker) DO UPDATE SET resolution=excluded.resolution, "
                    "exchange=COALESCE(excluded.exchange, watchlist.exchange), "
                    "asset=CASE WHEN ? IS NULL THEN watchlist.asset ELSE excluded.asset END, "
                    "source_ticker=COALESCE(excluded.source_ticker, watchlist.source_ticker), "
                    "invert=CASE WHEN ? IS NULL THEN watchlist.invert ELSE excluded.invert END, "
                    "rank=CASE WHEN ? IS NULL THEN watchlist.rank ELSE excluded.rank END, "
                    "backfilled_to=CASE WHEN watchlist.resolution=excluded.resolution "
                    "THEN watchlist.backfilled_to END",
                    (source, t.upper(), resolution, _now(), exchange, asset, source_ticker, int(invert), rank,
                     asset, asset, asset),
                )

    def watch_spec(self, source: str, spec, resolution: str = "auto") -> None:
        """Watch a symbols.SymbolSpec."""
        self.watch(source, [spec.ticker], resolution, exchange=spec.exchange, asset=spec.asset,
                   source_ticker=spec.source_ticker, invert=spec.invert, rank=spec.rank)

    # -- futures -----------------------------------------------------------

    def set_contract(
        self, source: str, product: str, ticker: str, month: date, state: str,
        last_trade: date | None, checked: date,
    ) -> None:
        """Record what is known about a contract: `month` is its contract month (stored in
        first_trade), `state` live | ended | missing, `last_trade` its last session once ended."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO futures_contracts (source, product, ticker, first_trade, last_trade, state, checked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, product, ticker, month.isoformat(), last_trade.isoformat() if last_trade else None,
                 state, checked.isoformat()))

    def contracts(self, source: str, product: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT ticker, first_trade, last_trade, state, checked FROM futures_contracts "
            "WHERE source=? AND product=? ORDER BY first_trade", (source, product))
        d = lambda v: date.fromisoformat(v) if v else None  # noqa: E731
        return [{"ticker": t, "month": d(f), "last_trade": d(l), "state": s, "checked": d(c)}
                for t, f, l, s, c in cur]

    def set_rolls(self, source: str, product: str, rank: int, schedule: list[tuple[date, str]]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM futures_rolls WHERE source=? AND product=? AND rank=?", (source, product, rank))
            self.conn.executemany(
                "INSERT INTO futures_rolls VALUES (?, ?, ?, ?, ?)",
                [(source, product, rank, d.isoformat(), t) for d, t in schedule])

    def rolls(self, source: str, product: str, rank: int = 1) -> list[tuple[date, str]]:
        cur = self.conn.execute(
            "SELECT session, ticker FROM futures_rolls WHERE source=? AND product=? AND rank=? ORDER BY session",
            (source, product, rank))
        return [(date.fromisoformat(d), t) for d, t in cur]

    def set_listing(
        self, source: str, ticker: str, listed_from: date | None, figi: str | None, checked: date,
        name: str | None = None, kind: str | None = None, exchange: str | None = None,
    ) -> None:
        """Record a listing check. `exchange` pins the primary exchange if none was set yet."""
        with self.conn:
            self.conn.execute(
                "UPDATE watchlist SET listed_from=?, figi=?, checked_at=?, name=COALESCE(?, name), "
                "kind=COALESCE(?, kind), exchange=COALESCE(exchange, ?) WHERE source=? AND ticker=?",
                (listed_from.isoformat() if listed_from else None, figi, checked.isoformat() if checked else None, name, kind,
                 exchange, source, ticker.upper()),
            )

    def set_exchange(self, ticker: str, mic: str) -> int:
        """Pin a stock's primary exchange anew on every feed watching it (the same instrument moved:
        a listing check found it on `mic`). Returns rows changed."""
        with self.conn:
            return self.conn.execute("UPDATE watchlist SET exchange=? WHERE ticker=? AND asset='stock' "
                                     "AND exchange IS NOT NULL AND exchange != ?", (mic, ticker.upper(), mic)).rowcount

    def directory_identity(self, ticker: str) -> list[tuple[str | None, str | None]]:
        """(FIGI, name) of a stock in the directory of listings (where a bulk-watched stock's exchange
        pin came from): what a listing check compares with when no feed has checked it yet."""
        return [tuple(r) for r in self.conn.execute(
            "SELECT figi, name FROM directory WHERE ticker=? AND kind='stock'", (ticker.upper(),))]

    def unwatch(self, source: str, tickers: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany(
                "DELETE FROM watchlist WHERE source=? AND ticker=?", [(source, t.upper()) for t in tickers]
            )

    def watch_counts(self) -> dict[tuple[str, str], int]:
        """{(feed, asset class): symbols watched}, open ones only (a closed archive's are filled)."""
        return {(s, a or "stock"): n for s, a, n in self.conn.execute(
            "SELECT source, asset, COUNT(*) FROM watchlist WHERE closed_at IS NULL GROUP BY source, asset")}

    def watchlist(self, source: str | None = None, ticker: str | None = None) -> list[dict]:
        """Watchlist entries, of one feed and/or one ticker (`ticker` reads by index: with thousands
        of symbols, per-symbol code asks for its own rows rather than scanning them all)."""
        sql = (
            "SELECT source, ticker, resolution, backfilled_to, exchange, listed_from, figi, checked_at, "
            "name, kind, asset, source_ticker, invert, rank, synced_at, closed_at, retired_on, seconds_from FROM watchlist"
        )
        where, params = [], []
        if source:
            where.append("source=?")
            params.append(source)
        if ticker:
            where.append("ticker=?")
            params.append(ticker.upper())
        if where:
            sql += " WHERE " + " AND ".join(where)

        def d(v: str | None) -> date | None:
            return date.fromisoformat(v) if v else None

        return [
            {"source": s, "ticker": t, "resolution": r, "backfilled_to": d(b), "exchange": e,
             "listed_from": d(lf), "figi": f, "checked_at": d(c), "name": n, "kind": k,
             "asset": a or "stock", "source_ticker": st or t, "invert": bool(inv), "rank": rk or 1,
             "synced_at": datetime.fromisoformat(sy) if sy else None,
             "closed_at": datetime.fromisoformat(cl) if cl else None, "retired_on": d(ret), "seconds_from": d(sf)}
            for s, t, r, b, e, lf, f, c, n, k, a, st, inv, rk, sy, cl, ret, sf
            in self.conn.execute(sql + " ORDER BY source, ticker", params)
        ]

    def set_resolution(self, source: str, ticker: str, resolution: str) -> None:
        self.watch(source, [ticker], resolution)

    def set_closed(self, source: str, ticker: str | None, when: datetime | None) -> int:
        """Close a feed's symbol (or, with ticker None, reopen/close all of the feed's symbols)."""
        sql = "UPDATE watchlist SET closed_at=? WHERE source=?"
        args = [when.isoformat() if when else None, source]
        if ticker:
            sql += " AND ticker=?"
            args.append(ticker.upper())
        with self.conn:
            return self.conn.execute(sql, args).rowcount

    def set_retired(self, ticker: str, day: date | None) -> int:
        """Mark a watched symbol retired (no longer listed) on every feed, or clear the mark (None)."""
        with self.conn:
            return self.conn.execute("UPDATE watchlist SET retired_on=? WHERE ticker=? AND asset IN ('stock', 'future')",
                                     (day.isoformat() if day else None, ticker.upper())).rowcount

    def move_resolution(self, source: str, tickers: Iterable[str], old: str, new: str, asset: str = "stock") -> int:
        """Move a feed's symbols of an asset class at `old` resolution to `new`, their backfill kept
        (set_resolution starts it again: minute to second, the minute history is still theirs).
        Returns the symbols moved."""
        with self.conn:
            return self.conn.executemany(
                "UPDATE watchlist SET resolution=? WHERE source=? AND ticker=? AND resolution=? AND asset=?",
                [(new, source, t.upper(), old, asset) for t in tickers]).rowcount

    def reset_backfill(self, source: str, ticker: str) -> None:
        self.set_backfilled(source, ticker, None)

    def set_seconds_from(self, source: str, ticker: str, day: date | None) -> None:
        """The day a symbol's window of 1-second bars was filled from (None: to fill again)."""
        with self.conn:
            self.conn.execute("UPDATE watchlist SET seconds_from=? WHERE source=? AND ticker=?",
                              (day.isoformat() if day else None, source, ticker.upper()))

    def set_backfilled(self, source: str, ticker: str, day: date | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE watchlist SET backfilled_to=? WHERE source=? AND ticker=?",
                (day.isoformat() if day else None, source, ticker.upper()),
            )

    # -- plan entitlements (per asset class) -------------------------------

    def set_entitlement(self, source: str, asset: str, resolution: str, allowed: bool) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO entitlements VALUES (?, ?, ?, ?, ?)",
                              (source, asset, resolution, int(allowed), _now()))

    def entitlement(self, source: str, asset: str, resolution: str, max_age_days: int = 7) -> bool | None:
        """Known answer for this plan, or None if never checked or older than max_age_days."""
        row = self.conn.execute(
            "SELECT allowed, checked_at FROM entitlements WHERE source=? AND asset=? AND resolution=?",
            (source, asset, resolution)).fetchone()
        if row is None:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row[1])
        return bool(row[0]) if age.days < max_age_days else None

    def clear_entitlements(self, source: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM entitlements WHERE source=?", (source,))

    def set_probe(self, source: str, ticker: str, when: datetime, newest_ms: int | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE watchlist SET probed_at=?, source_through=? WHERE source=? AND ticker=?",
                              (when.astimezone(timezone.utc).isoformat(timespec="seconds"), newest_ms,
                               source, ticker.upper()))

    def probe(self, source: str, ticker: str) -> tuple[datetime, int | None] | None:
        row = self.conn.execute("SELECT probed_at, source_through FROM watchlist WHERE source=? AND ticker=?",
                                (source, ticker.upper())).fetchone()
        return (datetime.fromisoformat(row[0]), row[1]) if row and row[0] else None

    def set_synced(self, source: str, ticker: str, when: datetime) -> None:
        with self.conn:
            self.conn.execute("UPDATE watchlist SET synced_at=? WHERE source=? AND ticker=?",
                              (when.astimezone(timezone.utc).isoformat(timespec="seconds"), source, ticker.upper()))

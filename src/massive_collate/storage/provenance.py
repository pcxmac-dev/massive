"""Derivation records: how each derived result was made (kind, product, code version, settings,
inputs, span, rows). Derived rows point to theirs by id (see provenance.py)."""

from __future__ import annotations

import json

from .common import _now, write_transaction


def _widens(record: tuple, span: tuple[int, int] | None, rows: int | None) -> bool:
    """Whether a derivation record (id, span_lo, span_hi, rows) would change: a span reaching
    beyond its own, or a larger row count."""
    _, lo, hi, held = record
    return bool(span and (lo is None or hi is None or span[0] < lo or span[1] > hi)) or \
        (rows is not None and (held is None or rows > held))


class ProvenanceStore:
    """Store methods: derivation records of derived results."""

    def record_derivation(self, kind: str, product: str, code: str, settings: dict, inputs: dict,
                          span: tuple[int, int] | None = None, rows: int | None = None) -> int:
        """The record of a derivation: the same kind, product, code, settings and inputs reuse the
        newest record, its span widened (the live recorder and derive.py rebuild every minute: a row
        each time grew the table by tens of thousands a day)."""
        settings_json = json.dumps(settings, sort_keys=True, default=str)
        inputs_json = json.dumps(inputs, sort_keys=True, default=str)
        find = ("SELECT id, span_lo, span_hi, rows FROM derivations WHERE product=? AND kind=? AND code=? "
                "AND settings=? AND inputs=? ORDER BY id DESC LIMIT 1", (product, kind, code, settings_json, inputs_json))
        same = self.conn.execute(*find).fetchone()
        if same is not None and not _widens(same, span, rows):
            return same[0]  # the usual case: nothing to write, no lock taken
        # Found and written in one write transaction: two processes recording the same derivation
        # made two records, and two widening one span each kept only their own end.
        with write_transaction(self.conn):
            same = self.conn.execute(*find).fetchone()
            if same is not None:
                if _widens(same, span, rows):  # widened in SQL, from the record as it is now (NULL: no bound)
                    self.conn.execute(
                        "UPDATE derivations SET span_lo = COALESCE(MIN(span_lo, ?1), ?1, span_lo), "
                        "span_hi = COALESCE(MAX(span_hi, ?2), ?2, span_hi), rows = COALESCE(MAX(rows, ?3), ?3, rows) "
                        "WHERE id=?4", (span[0] if span else None, span[1] if span else None, rows, same[0]))
                return same[0]
            cur = self.conn.execute(
                "INSERT INTO derivations (kind, product, created_at, code, settings, inputs, span_lo, span_hi, rows) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, product, _now(), code, settings_json, inputs_json, span[0] if span else None,
                 span[1] if span else None, rows))
            return cur.lastrowid

    def set_derivation_rows(self, derivation: int, rows: int) -> None:
        """The rows a derivation made. A record is reused by every build with the same code,
        settings and inputs (derive.py rebuilds a series' recent bins every few minutes), and each
        build reports the bars it stored itself: a later, smaller count does not replace a larger
        one (the record said "12 rows" for a series' whole history, the last build's)."""
        with self.conn:
            self.conn.execute("UPDATE derivations SET rows = COALESCE(MAX(rows, ?1), ?1, rows) WHERE id=?2",
                              (rows, derivation))

    def derivation(self, derivation: int) -> dict | None:
        rows = self.derivations(ids=[derivation])
        return rows[0] if rows else None

    def derivations(self, kind: str | None = None, product: str | None = None, limit: int = 50,
                    ids: list[int] | None = None) -> list[dict]:
        """Derivation records, newest first; `product` matches its start (e.g. "alpaca:BTCUSD")."""
        sql, args = ("SELECT id, kind, product, created_at, code, settings, inputs, span_lo, span_hi, rows "
                     "FROM derivations WHERE 1=1"), []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if product:
            sql += " AND product LIKE ?"
            args.append(product.replace("%", r"\%") + "%")
        if ids:
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            args += list(ids)
        cols = ["id", "kind", "product", "created_at", "code", "settings", "inputs", "span_lo", "span_hi", "rows"]
        out = []
        for r in self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]):
            d = dict(zip(cols, r))
            d["settings"], d["inputs"] = json.loads(d["settings"]), json.loads(d["inputs"])
            out.append(d)
        return out

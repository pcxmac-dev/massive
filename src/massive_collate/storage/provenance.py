"""Derivation records: how each derived result was made (kind, product, code version, settings,
inputs, span, rows). Derived rows point to theirs by id (see provenance.py)."""

from __future__ import annotations

import json

from .common import _now


class ProvenanceStore:
    """Store methods: derivation records of derived results."""

    def record_derivation(self, kind: str, product: str, code: str, settings: dict, inputs: dict,
                          span: tuple[int, int] | None = None, rows: int | None = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO derivations (kind, product, created_at, code, settings, inputs, span_lo, span_hi, rows) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (kind, product, _now(), code, json.dumps(settings, sort_keys=True, default=str),
                 json.dumps(inputs, sort_keys=True, default=str), span[0] if span else None,
                 span[1] if span else None, rows))
            return cur.lastrowid

    def set_derivation_rows(self, derivation: int, rows: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE derivations SET rows=? WHERE id=?", (rows, derivation))

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

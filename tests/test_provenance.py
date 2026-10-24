"""Derived results apart from raw data, each with a record of how it was made."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pandas as pd

from massive_collate import collector, provenance
from massive_collate.storage import Store


def _ticks(store, source="alpaca", ticker="BTCUSD"):
    base = 1_761_192_000_000  # a UTC midnight, ms
    trades = [{"sip_timestamp": (base + i * 15_000) * 1_000_000, "sequence_number": i, "price": 100 + i,
               "size": 1, "exchange": 1, "conditions": []} for i in range(8)]  # two minutes of ticks
    store.upsert_trades(source, ticker, trades)
    store.add_coverage(source, ticker, "trades", base, base + 120_000)
    return base


def test_code_version_names_the_commit():
    assert re.fullmatch(r"[0-9a-f]{12}(\+dirty)?|massive-collate .+", provenance.code_version())


def test_bars_built_from_ticks_live_apart_with_their_derivation(store):
    base = _ticks(store)
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, [{"t": base - 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    built = collector.build_from_ticks(store, "alpaca", "BTCUSD", base, base + 120_000, rules={}, log=lambda *_: None)
    assert built == 8 + 2  # eight one-second bars and two minute bars
    conn = store._bars("alpaca")
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 1  # the feed's own bar only
    assert conn.execute("SELECT COUNT(*) FROM built_bars").fetchone()[0] == 10
    minutes = store.bars_window(["alpaca"], "BTCUSD", "minute", base - 60_000, base + 120_000)
    assert minutes["ts"].tolist() == [base - 60_000, base, base + 60_000]  # reads combine both
    assert store.bar_edges("alpaca", "BTCUSD", "second") == (base, base + 105_000)
    (d,) = store.derivations(kind="bars-from-ticks")
    assert d["product"] == "alpaca:BTCUSD" and d["rows"] == 10 and d["inputs"]["count"] == 8
    assert d["settings"]["steps"] == ["second", "minute"] and len(d["settings"]["rules"]) == 16
    assert d["code"] == provenance.code_version() and (d["span_lo"], d["span_hi"]) == (base, base + 120_000)
    did = conn.execute("SELECT DISTINCT derivation FROM built_bars").fetchall()
    assert did == [(d["id"],)]


def test_bars_built_before_provenance_are_moved_apart(store):
    base = 1_761_192_000_000
    store.upsert_bars("alpaca", "BTCUSD", "second", 1, [{"t": base + i * 1000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
                                                        for i in range(5)])
    store.upsert_bars("alpaca", "BTCUSD", "minute", 1, [{"t": base - 60_000, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
                                                        {"t": base, "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}])
    store.add_coverage("alpaca", "BTCUSD", "second", base, base + 60_000, origin="ticks")
    store.add_coverage("alpaca", "BTCUSD", "minute", base, base + 60_000, origin="ticks")
    assert store.separate_built_bars(log=lambda *_: None) == 6
    conn = store._bars("alpaca")
    assert conn.execute("SELECT timespan, COUNT(*) FROM bars GROUP BY timespan").fetchall() == [("minute", 1)]
    assert conn.execute("SELECT COUNT(*) FROM built_bars").fetchone()[0] == 6
    assert {d["code"] for d in store.derivations(kind="bars-from-ticks")} == {"before-provenance"}
    assert store.bars_window(["alpaca"], "BTCUSD", "minute", 0, 10**15)["close"].tolist() == [1, 2]  # nothing lost
    assert store.separate_built_bars(log=lambda *_: None) == 0  # once only


def test_composite_and_quality_record_how_they_were_made(store):
    from massive_collate import composite
    from massive_collate.symbols import resolve

    from .test_composite import T0, _feeds

    for feed in ("coinbase", "alpaca", "bitfinex"):
        store.watch_spec(feed, resolve("BTCUSD"))
    mk = lambda c: [{"t": T0 + i * 60_000, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 2.0} for i in range(30)]  # noqa: E731
    for feed, c in (("coinbase", 84000.0), ("alpaca", 84003.0), ("bitfinex", 84080.0)):
        store.upsert_bars(feed, "BTCUSD", "minute", 1, mk(c))
    composite.refresh(store, _feeds(), datetime.fromtimestamp((T0 + 3_600_000) / 1000, timezone.utc), log=lambda m: None)
    (comp,) = store.derivations(kind="composite")
    assert comp["product"] == "composite:BTCUSD:minute" and comp["rows"] == 30
    assert comp["settings"]["outlier_bps"] == composite.OUTLIER_BPS and set(comp["inputs"]["weights"]) == {
        "coinbase", "kraken", "bitfinex"}
    conn = store._bars("composite")
    assert conn.execute("SELECT DISTINCT derivation FROM bars").fetchall() == [(comp["id"],)]
    assert conn.execute("SELECT DISTINCT derivation FROM composite_stats").fetchall() == [(comp["id"],)]
    (q,) = store.derivations(kind="quality")
    assert q["settings"]["days"] == composite.QUALITY_DAYS and store.quality("coinbase", "crypto")["derivation"] == q["id"]


def test_an_audit_records_its_tolerances_and_inputs(store):
    from datetime import date

    from massive_collate import audit
    from massive_collate.symbols import resolve

    from .test_feeds import Other, _log, _minutes

    store.watch_spec("fake", resolve("SPCX"))
    store.watch_spec("other", resolve("SPCX"))
    bars = _minutes(date(2026, 10, 20), count=30)
    store.upsert_bars("fake", "SPCX", "minute", 1, bars)
    auditor = Other(entitled=("minute",), bars={"minute": bars})
    r = audit.audit_span(auditor, store, "SPCX", "minute", bars[0]["t"], bars[-1]["t"] + 60_000, "fake", "manual", log=_log)
    (d,) = store.derivations(kind="audit")
    assert d["settings"]["tolerance"] == audit.TOLERANCE and d["inputs"]["auditor_bars"] == 30
    assert store.audits(1)[0]["id"] == r["id"]
    row = store.conn.execute("SELECT derivation FROM audits WHERE id=?", (r["id"],)).fetchone()
    assert row == (d["id"],)


def test_indicators_say_how_they_were_derived_and_whether_they_look_ahead(tmp_path):
    from massive_collate.calc.derivatives import Model
    from massive_collate.gui.bars import BarEngine

    from .test_gui import _store_with_bars

    eng = BarEngine(_store_with_bars(tmp_path))
    page = eng.bars("massive", "SPCX", "minute", 1, model=Model("d1", per="bar", causal=True))
    d = page["model"]["derivation"]
    assert d["kind"] == "indicator" and d["settings"]["window"] == 9 and d["lookAhead"] is False
    assert d["code"] == provenance.code_version()

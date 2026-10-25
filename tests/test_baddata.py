"""Bad data (docs/ARCHITECTURE.md, "Bad data"; baddata.py): what the rules catch, the pruned list
it goes on, the filtered and unfiltered views, and a rebuild that keeps what it replaces."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from massive_collate import baddata, derive
from massive_collate.collector import build_from_ticks
from massive_collate.storage import Store

ET = "America/New_York"


def _ms(t: str) -> int:
    return int(pd.Timestamp(t, tz=ET).timestamp() * 1000)


def _minutes(day: str, start: str, end: str, price: float, volume: float) -> list[dict]:
    out = []
    for i, t in enumerate(pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min", tz=ET, inclusive="left")):
        p = price + (i % 5) * 0.01
        out.append({"t": int(t.timestamp() * 1000), "o": p, "h": p + 0.02, "l": p - 0.02, "c": p + 0.01,
                    "v": volume, "vw": p, "n": 10})
    return out


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.watch("massive", ["SPCX"], "minute")
        for day in ("2026-10-19", "2026-10-20"):
            s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, "04:00", "09:30", 50.0, 100))  # pre-market: thin
            s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, "09:30", "16:00", 50.0, 10_000))  # regular: busy
            s.upsert_bars("massive", "SPCX", "minute", 1, _minutes(day, "16:00", "20:00", 50.0, 100))  # after hours
        yield s


def _set(store, t: str, **bar):
    row = next(b for b in store.bars_window(["massive"], "SPCX", "minute", _ms(t), _ms(t) + 60_000,
                                            filtered=False).to_dict("records"))
    new = {"t": _ms(t), "o": row["open"], "h": row["high"], "l": row["low"], "c": row["close"], "v": row["volume"],
           "vw": row["vwap"], "n": row["transactions"]}
    store.upsert_bars("massive", "SPCX", "minute", 1, [{**new, **bar}])


def test_a_spike_without_volume_is_pruned_and_one_with_volume_is_not(store):
    _set(store, "2026-10-19 17:08", h=75.0, l=30.0, v=120)  # after hours: 22 dollars either way on its usual volume
    _set(store, "2026-10-19 18:30", h=60.0, v=1_500)  # 15 times the after-hours volume behind it: a move
    _set(store, "2026-10-20 11:00", h=60.0, v=12_000)  # regular hours: little volume beside its neighbours'
    added = baddata.scan_series(store, "massive", "SPCX", "minute", "stock")
    entries = store.pruned("massive", "SPCX")
    assert added == 2 and sorted(entries["ts"]) == [_ms("2026-10-19 17:08"), _ms("2026-10-20 11:00")]
    ev = json.loads(entries[entries["ts"] == _ms("2026-10-19 17:08")]["evidence"].iloc[0])
    assert ev["part"] == "extended" and ev["neighbours_volume_median"] == 100 and ev["range_x"] > 100
    assert (entries["rule"] == "spike").all() and entries["excludes"].all()
    assert baddata.scan_series(store, "massive", "SPCX", "minute", "stock") == 0  # once


def test_filtered_reads_leave_bad_data_out_and_unfiltered_ones_keep_it(store):
    _set(store, "2026-10-19 17:08", h=75.0, l=30.0, v=120)
    baddata.scan_series(store, "massive", "SPCX", "minute", "stock")
    lo, hi = _ms("2026-10-19 17:00"), _ms("2026-10-19 17:30")
    filtered = store.bars_window(["massive"], "SPCX", "minute", lo, hi)
    every = store.bars_window(["massive"], "SPCX", "minute", lo, hi, filtered=False)
    assert len(every) == 30 and len(filtered) == 29 and every["high"].max() == 75.0 and filtered["high"].max() < 51
    assert len(store.load_bars(["SPCX"], "minute", source="massive", filtered=False)) - \
        len(store.load_bars(["SPCX"], "minute", source="massive")) == 1
    raw = store._bars("massive").execute("SELECT high FROM bars WHERE ts=?", (_ms("2026-10-19 17:08"),)).fetchone()
    assert raw == (75.0,)  # the feed's own bar is kept as it came
    assert store.release_pruned("massive", "SPCX", "minute", 1, _ms("2026-10-19 17:08")) == 1
    assert len(store.bars_window(["massive"], "SPCX", "minute", lo, hi)) == 30  # back, the record kept
    assert store.pruned("massive", "SPCX")["released_at"].notna().all()
    assert baddata.scan_series(store, "massive", "SPCX", "minute", "stock") == 0  # a release is a decision


def test_the_frames_above_are_built_again_without_what_was_pruned(store):
    derive.run(store, log=lambda *_: None)
    _set(store, "2026-10-19 17:08", h=75.0, l=30.0, v=120)
    derive.run(store, log=lambda *_: None)  # the check runs first, then the frames above
    five = store._bars("massive").execute("SELECT high, low FROM built_bars WHERE ticker='SPCX' AND timespan='minute' "
                                          "AND multiplier=5 AND ts=?", (_ms("2026-10-19 17:05"),)).fetchone()
    assert five[0] < 51 and five[1] > 49 and len(store.pruned("massive", "SPCX")) == 1


def test_malformed_bars_are_pruned_and_are_no_ones_neighbour(store):
    _set(store, "2026-10-20 10:00", h=49.0, l=51.0)  # high below low
    added = baddata.scan_series(store, "massive", "SPCX", "minute", "stock")
    entry = store.pruned("massive", "SPCX")
    assert added == 1 and entry["rule"].iloc[0] == "malformed"
    assert "high below low" in json.loads(entry["evidence"].iloc[0])["failed"]


def test_the_scan_follows_what_changed(store):
    assert baddata.scan_symbol(store, "massive", "SPCX", "stock") == 0
    _set(store, "2026-10-20 17:08", h=75.0, l=30.0, v=120)  # stored later: judged by the next scan
    assert baddata.scan_symbol(store, "massive", "SPCX", "stock") == 1
    assert baddata.scan_symbol(store, "massive", "SPCX", "stock") == 0


def _trade(t: str, i: int, price: float, conditions, size=100):
    return {"sip_timestamp": _ms(t) * 1_000_000 + i * 1_000_000_000, "sequence_number": i, "price": price, "size": size,
            "exchange": 4, "conditions": conditions, "tape": 3}


def test_a_rebuild_keeps_every_bar_it_changes_on_the_pruned_list(tmp_path):
    with Store(tmp_path / "r.db") as store:
        store.save_conditions("massive", [
            {"id": 2, "name": "Average Price Trade", "cta": "B", "utp": "W", "high_low": False, "open_close": False,
             "volume": True},
            {"id": 12, "name": "Form T/Extended Hours", "cta": "T", "utp": "T", "high_low": False, "open_close": False,
             "volume": True}])
        trades = [_trade("2026-10-01 17:07", i, 231.15, [12]) for i in range(30)]
        trades += [_trade("2026-10-01 17:08", 0, 231.15, [12]), _trade("2026-10-01 17:08", 1, 251.77, [12, 2], 160),
                   _trade("2026-10-01 17:08", 2, 231.14, [12])]
        store.upsert_trades("massive", "SPCX", trades)
        lo, hi = _ms("2026-10-01 17:07"), _ms("2026-10-01 17:09")
        store.add_coverage("massive", "SPCX", "trades", lo, hi)
        wrong = {("utp", "12"): (True, True, True), ("utp", "2"): (True, True, True)}  # as the mixed-up rules had it
        build_from_ticks(store, "massive", "SPCX", lo, hi, wrong, lambda *_: None)
        assert store.bars_window(["massive"], "SPCX", "minute", lo, hi)["high"].max() == 251.77
        out = baddata.repair_from_ticks(store, "massive", log=lambda *_: None)
        minute = store.bars_window(["massive"], "SPCX", "minute", lo, hi)
        assert minute["high"].max() == 231.15 and out == {"SPCX": len(store.pruned("massive", "SPCX"))}
        kept = store.pruned("massive", "SPCX", timespan="minute")
        assert list(kept["ts"]) == [_ms("2026-10-01 17:08")] and kept["high"].iloc[0] == 251.77  # 17:07 was right
        assert kept["rule"].iloc[0] == "rebuilt" and not kept["excludes"].iloc[0]
        assert json.loads(kept["evidence"].iloc[0])["now"]["high"] == 231.15
        second = store.pruned("massive", "SPCX", timespan="second")
        assert list(second["ts"]) == [_ms("2026-10-01 17:08") + 1000]  # the one second the print was in
        assert len(store.trades_between("massive", "SPCX", lo * 1_000_000, hi * 1_000_000)) == 33  # trades untouched


def test_the_chart_shows_filtered_data_unless_asked_for_all_of_it(store):
    from massive_collate.gui.server import ChartApp

    _set(store, "2026-10-19 17:08", h=75.0, l=30.0, v=120)
    baddata.scan_series(store, "massive", "SPCX", "minute", "stock")
    app = ChartApp(store)
    q = {"source": "massive", "ticker": "SPCX", "period": "minute", "span": "1", "session": "extended", "auto": "1",
         "to": str(_ms("2026-10-19 17:30")), "limit": "30"}
    highs = {}
    for view in ("0", "1"):
        with app._view({**q, "unfiltered": view}):
            highs[view] = max(b["high"] for b in app.bars({**q, "unfiltered": view})["bars"])
    assert highs["0"] < 51 and highs["1"] == 75.0
    listed = app.pruned("SPCX")
    assert listed["counts"] == {"massive": {"spike": 1}} and listed["entries"][0]["evidence"]["part"] == "extended"


# -- rule 3: a feed's own bar against its own finer bars; the fetch's second answer; audits ----
from massive_collate.collector import verify_fetched  # noqa: E402
from massive_collate.config import TIERS  # noqa: E402
from massive_collate.coverage import FeedProfile  # noqa: E402


def _day(store, day: str, **bar):
    t = _ms(f"{day} 00:00")
    base = {"t": t, "o": 50.0, "h": 50.06, "l": 49.98, "c": 50.03, "v": 1e6, "vw": 50.0, "n": 900}
    store.upsert_bars("massive", "SPCX", "day", 1, [{**base, **bar}])


def test_a_daily_bar_outside_its_own_minutes_is_pruned(store):
    store.add_coverage("massive", "SPCX", "minute", _ms("2026-10-19 00:00"), _ms("2026-10-21 00:00"))
    _day(store, "2026-10-19", h=78.95)  # the feed's day: a high no minute of its own reached (NVDA 2024-06-10)
    _day(store, "2026-10-20", h=50.30)  # within 1 % of its minutes' high (50.06): feeds filter prints a little differently
    _day(store, "2026-10-16", h=99.0)  # no minutes held that day: nothing to judge it by
    bad, evidence = baddata.inconsistent(store, "massive", "SPCX", "day", _ms("2026-10-15 00:00"), _ms("2026-10-21 00:00"))
    assert list(bad["ts"]) == [_ms("2026-10-19 00:00")]
    assert evidence[0]["finer"] == "minute" and evidence[0]["finer_high"] == pytest.approx(50.06)
    assert evidence[0]["finer_bars"] == 960
    baddata.scan_series(store, "massive", "SPCX", "day", "stock")
    entry = store.pruned("massive", "SPCX", timespan="day")
    assert list(entry["rule"]) == ["inconsistent"] and entry["high"].iloc[0] == 78.95
    assert store.bars_window(["massive"], "SPCX", "day", _ms("2026-10-19 00:00"), _ms("2026-10-20 00:00")).empty
    assert len(store.bars_window(["massive"], "SPCX", "day", _ms("2026-10-19 00:00"), _ms("2026-10-20 00:00"),
                                 filtered=False)) == 1  # kept: all data shows it


class _Asked:
    """A feed that answers a re-asked day with `answer` (or nothing)."""
    name = "massive"

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
        self.asked.append((ticker, start, end, timespan))
        yield [self.answer] if self.answer else []


def _feeds():
    return {"massive": FeedProfile("massive", TIERS["advanced"], frozenset({"stock"}), 0),
            "other": FeedProfile("other", TIERS["free"], frozenset({"stock"}), 1)}


@pytest.mark.parametrize("second_answer, corrected", [(78.95, 0), (50.06, 1)])
def test_a_fetched_bar_that_fails_is_asked_for_again_and_audited(store, second_answer, corrected):
    store.add_coverage("massive", "SPCX", "minute", _ms("2026-10-19 00:00"), _ms("2026-10-21 00:00"))
    since = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)  # the fetch began
    _day(store, "2026-10-19", h=78.95)
    feeds = _feeds()
    feed = _Asked({"t": _ms("2026-10-19 00:00"), "o": 50.0, "h": second_answer, "l": 49.98, "c": 50.03, "v": 1e6,
                   "vw": 50.0, "n": 900})
    lines = []
    out = verify_fetched(feed, store, "SPCX", "stock", "SPCX", False, since - 1000, feeds["massive"], feeds, lines.append)
    assert out == {"failed": 1, "asked": 1, "corrected": corrected} and len(feed.asked) == 1
    stored = store.bars_window(["massive"], "SPCX", "day", _ms("2026-10-19 00:00"), _ms("2026-10-20 00:00"),
                               filtered=False)["high"].iloc[0]
    assert stored == second_answer  # a different second answer is a correction; the first is kept as a revision
    if corrected:
        assert store._bars("massive").execute("SELECT high FROM bar_revisions WHERE timespan='day'").fetchone() == (78.95,)
    queued = {(q["timespan"], q["start_ms"]) for q in store.audit_queue()}
    assert ("day", _ms("2026-10-19 00:00")) in queued  # a second feed's audit of the failing day
    derive.run(store, log=lambda *_: None)  # the check derive runs: a bar still failing is pruned
    assert len(store.pruned("massive", "SPCX", timespan="day", rule="inconsistent")) == (0 if corrected else 1)

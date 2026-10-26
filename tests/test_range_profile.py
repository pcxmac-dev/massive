"""The highlight's profile (gui/window.py range_profile, /api/profile with from and to): volume by
price across a stretch of the chart, spread from the finest bars a stretch that long allows, with
its major prices (calc/profile.py major_prices)."""
from __future__ import annotations

import pandas as pd
import pytest

from massive_collate import derive
from massive_collate.calc.profile import major_prices
from massive_collate.gui.server import ChartApp
from massive_collate.gui.window import WindowReader
from massive_collate.storage import Store

ET = "America/New_York"


def _ms(t: str) -> int:
    return int(pd.Timestamp(t, tz=ET).timestamp() * 1000)


def test_major_prices_are_the_point_of_control_its_value_area_and_the_other_peaks():
    rows = pd.DataFrame({"price": [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5],
                         "volume": [5, 60, 10, 20, 100, 30, 8, 2], "trades": 1})
    m = major_prices(rows.iloc[::-1])  # highest first, as the ladder lists them
    assert m["poc"] == 12.0
    # grown from 12.0 toward the busier neighbour until 70% of 235 is held: 12.5 (30), 11.5 (20),
    # 11.0 (10) then 10.5 (60) rather than 13.0 (8)
    assert (m["val"], m["vah"]) == (10.5, 12.5) and m["held"] == pytest.approx(220 / 235)
    assert m["nodes"] == [10.5]  # a peak with at least half the point of control's; 12.5 is not a peak
    assert major_prices(rows.iloc[0:0]) == {}


def test_a_flat_tops_point_of_control_is_its_middle():
    rows = pd.DataFrame({"price": [1.0, 1.1, 1.2, 1.3, 1.4], "volume": [5, 50, 50, 50, 5], "trades": 1})
    m = major_prices(rows)
    assert m["poc"] == 1.2 and m["nodes"] == []  # the flat top's other segments are not peaks of their own


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        for d, base in (("2026-10-14", 10.0), ("2026-10-15", 10.4), ("2026-10-16", 10.8)):
            t0 = pd.Timestamp(f"{d} 09:30", tz=ET)
            rows = []
            for i in range(390):
                p = base + (0.5 if 150 <= i < 250 else 0.0)  # an hour and a half held up at +0.50: the busiest price
                rows.append({"t": int((t0 + pd.Timedelta(minutes=i)).timestamp() * 1000), "o": p, "h": p + 0.02,
                             "l": p - 0.02, "c": p, "v": 1000.0 if 150 <= i < 250 else 100.0, "n": 5})
            s.upsert_bars("massive", "SPCX", "minute", 1, rows)
        s.watch("massive", ["SPCX"], "minute")
        derive.run(s, log=lambda *_: None)
        yield s


def test_a_highlights_profile_spreads_its_minutes_and_finds_its_busiest_price(store):
    app = ChartApp(store)
    out = app.profile({"source": "*", "ticker": "SPCX", "period": "minute", "span": "30", "session": "regular",
                       "from": str(_ms("2026-10-14 09:30")), "to": str(_ms("2026-10-14 15:30"))})
    r = out["range"]
    assert r["bars"] == 13 and r["from"] == _ms("2026-10-14 09:30") and r["to"] == _ms("2026-10-14 16:00")
    assert out["basis"] == "390 1-minute bars" and out["rows"]
    assert out["marks"]["poc"] == pytest.approx(10.5, abs=1.5 * out["step"])  # the price it was held at
    assert sum(x["volume"] for x in out["rows"]) == pytest.approx(r["volume"])  # every share, nowhere twice
    assert out["derived"].startswith("estimated from 1-minute bars")


def test_a_long_highlight_reads_coarser_bars_within_the_cap(store, monkeypatch):
    monkeypatch.setattr(WindowReader, "RANGE_PROFILE_BARS", 200)  # three sessions of minutes is over it
    out = ChartApp(store).profile({"source": "*", "ticker": "SPCX", "period": "hour", "span": "1", "session": "regular",
                                   "from": str(_ms("2026-10-14 09:30")), "to": str(_ms("2026-10-16 15:30"))})
    assert out["basis"].endswith("30-minute bars") and out["range"]["bars"] == 21
    assert {"poc", "vah", "val", "nodes"} <= set(out["marks"])

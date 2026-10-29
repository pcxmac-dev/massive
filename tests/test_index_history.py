"""A symbol's history before its feeds, from a dataset of the same index (gui/window.py HISTORY): the
user, 2026-10-28: "also the S&P back to 1800s if possible"; "Yes, draw Shiller's monthly S&P from 1871
on the S&P chart before the daily history, marked as monthly"; "make sure the source and dataset work
together". Shiller's months fill the month and year levels before the month of the feed's first daily
bar, the feed's own bars from there; where both have a whole month they are checked against each other."""
from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from massive_collate import datasets
from massive_collate.gui import server
from massive_collate.gui.bars import BarEngine, _regular_close, to_records
from massive_collate.gui.window import HISTORY_TOLERANCE_PCT, WindowReader
from massive_collate.storage import Store

ET = "America/New_York"
SHILLER = "shiller-us-market-monthly"


def _month_ms(y: int, m: int) -> int:
    return int(pd.Timestamp(year=y, month=m, day=1, tz=ET).timestamp() * 1000)


def _trading_days(start: str, end: str) -> list[pd.Timestamp]:
    return [d for d in pd.date_range(start, end, freq="D") if _regular_close(d.date()) is not None]


# Shiller's monthly averages: two months of 1871, then 2023-01 to 2024-12 (made up, a steady rise)
VALUES = {(1871, 1): 4.44, (1871, 2): 4.5, **{(2023 + i // 12, i % 12 + 1): 4000.0 + 50 * i for i in range(24)}}


def _shiller(store, values=VALUES):
    parsed = datasets.Parsed(values=[("SP500", _month_ms(y, m), v) for (y, m), v in values.items()],
                             series={"SP500": ("SP500", "index", "month")})
    store.save_dataset_version("shiller", SHILLER, parsed, _month_ms(2025, 1), "t")


def _days(store, start: str, end: str, close_for):
    """The feed's daily bars, each close from `close_for(day)`."""
    bars = []
    for d in _trading_days(start, end):
        c = close_for(d)
        bars.append({"t": int(d.tz_localize(ET).timestamp() * 1000), "o": c - 1, "h": c + 5, "l": c - 5, "c": c,
                     "v": 2e9, "vw": c, "n": 0})
    store.upsert_bars("massive", "SPX", "day", 1, bars)


def _monthly(target):
    """Closes whose mean over each month is `target[(y, m)]` exactly: alternately 10 above and below,
    the last of an odd month at the target itself."""
    def close_for(d):
        days = _trading_days(d.replace(day=1), d + pd.offsets.MonthEnd(0))
        i = [x.day for x in days].index(d.day)
        if len(days) % 2 and i == len(days) - 1:
            return target[(d.year, d.month)]
        return target[(d.year, d.month)] + (10 if i % 2 == 0 else -10)
    return close_for


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.watch("massive", ["SPX"], "minute", asset="index", source_ticker="I:SPX")
        _shiller(s)
        # the feed from 2024-10-23 (its first month part-way through), its months' means Shiller's values,
        # December's 0.2 % above
        target = {**VALUES, (2024, 12): VALUES[(2024, 12)] * 1.002}
        _days(s, "2024-10-23", "2024-12-31", _monthly(target))
        yield s


def _bars(reader, period, ticker="SPX"):
    return reader.page("massive", ticker, period, 1, "regular", limit=100_000)


def test_the_months_before_the_feed_come_from_shiller_flat_and_marked(store):
    page = _bars(WindowReader(BarEngine(store)), "month")
    by_ms = {b["timestamp"]: b for b in page["bars"]}
    jan71 = by_ms[_month_ms(1871, 1)]
    assert (jan71["open"], jan71["high"], jan71["low"], jan71["close"], jan71["volume"]) == (4.44, 4.44, 4.44, 4.44, 0)
    assert jan71["hist"] is True and jan71["derived"] is True
    sep = by_ms[_month_ms(2024, 9)]
    assert sep["hist"] is True and sep["close"] == VALUES[(2024, 9)]
    # from the month of the feed's first daily bar (October, part-way through) the feed's own
    oct_ = by_ms[_month_ms(2024, 10)]
    days = store.load_bars(["SPX"], "day", 1, source="massive")
    assert oct_["hist"] is False and oct_["volume"] == 2e9 * 7  # its seven days from the 23rd
    assert oct_["high"] == VALUES[(2024, 10)] + 15 and oct_["open"] == days["open"].iloc[0]
    assert all(b["hist"] is False for b in page["bars"] if b["timestamp"] >= _month_ms(2024, 10))
    assert [b["timestamp"] for b in page["bars"]] == sorted(by_ms)  # one bar a month, in order


def test_a_year_mixing_shiller_months_and_feed_days_is_built_from_both(store):
    years = {b["timestamp"]: b for b in _bars(WindowReader(BarEngine(store)), "year")["bars"]}
    y2024 = years[_month_ms(2024, 1)]
    assert y2024["open"] == VALUES[(2024, 1)] and y2024["hist"] is True  # January from Shiller ...
    last_close = store.load_bars(["SPX"], "day", 1, source="massive")["close"].iloc[-1]
    assert y2024["close"] == pytest.approx(last_close)  # ... December's last day from the feed
    assert years[_month_ms(1871, 1)]["close"] == 4.5


def test_days_and_weeks_keep_to_the_feed(store):
    reader = WindowReader(BarEngine(store))
    for period in ("day", "week"):
        bars = _bars(reader, period)["bars"]
        assert bars and all(not b.get("hist") for b in bars)
        assert bars[0]["timestamp"] >= _month_ms(2024, 10)


def test_month_and_year_levels_reach_back_to_1871_finer_ones_stop_at_the_feed(store):
    levels = {lv["type"]: lv for lv in WindowReader(BarEngine(store)).levels("massive", "SPX")}
    assert levels["month"]["first"] == levels["year"]["first"] == _month_ms(1871, 1)
    first_day = int(pd.Timestamp("2024-10-23", tz=ET).timestamp() * 1000)
    assert levels["day"]["first"] == levels["week"]["first"] == first_day  # the auto zoom holds at months before it
    assert levels["month"]["last"] == levels["day"]["last"]


def test_the_page_says_where_the_feed_takes_over_and_that_they_agree(store):
    info = _bars(WindowReader(BarEngine(store)), "month")["history"]
    assert info["dataset"] == SHILLER and info["series"] == "SP500" and "ODC PDDL" in info["licence"]
    assert info["seam"] == _month_ms(2024, 10)
    check = info["check"]
    # October is left out (the feed starts on the 23rd): November exactly, December 0.2 % apart
    assert check["months"] == 2 and check["agrees"] is True and check["flagged"] == []
    assert check["max"] == pytest.approx(100 * (1 / 1.002 - 1) * -1, abs=1e-3) and check["worst"] == _month_ms(2024, 12)
    assert check["tolerance"] == HISTORY_TOLERANCE_PCT


def test_a_month_apart_by_more_than_the_tolerance_is_flagged(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.watch("massive", ["SPX"], "minute", asset="index")
        _shiller(s)
        _days(s, "2024-11-01", "2024-12-31", _monthly({**VALUES, (2024, 11): VALUES[(2024, 11)] * 1.02}))
        check = _bars(WindowReader(BarEngine(s)), "month")["history"]["check"]
        assert check["agrees"] is False and [m for m, _ in check["flagged"]] == [_month_ms(2024, 11)]
        assert check["flagged"][0][1] == pytest.approx((1 / 1.02 - 1) * 100, abs=1e-3)


def test_a_month_the_feed_misses_a_day_of_is_not_checked(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.watch("massive", ["SPX"], "minute", asset="index")
        _shiller(s)
        _days(s, "2024-11-01", "2024-12-31", _monthly(VALUES))
        with s.conn:  # a hole on 2024-12-10
            s._bars("massive").execute("DELETE FROM bars WHERE ticker='SPX' AND ts=?",
                                       (int(pd.Timestamp("2024-12-10", tz=ET).timestamp() * 1000),))
        check = _bars(WindowReader(BarEngine(s)), "month")["history"]["check"]
        assert check["months"] == 1 and check["worst"] == _month_ms(2024, 11)


def test_the_feed_backfilling_earlier_takes_those_months_from_shiller(store):
    _days(store, "2024-06-03", "2024-10-22", _monthly(VALUES))
    reader = WindowReader(BarEngine(store))
    page = _bars(reader, "month")
    assert page["history"]["seam"] == _month_ms(2024, 6)
    assert {b["timestamp"]: b["hist"] for b in page["bars"]}[_month_ms(2024, 5)] is True
    assert all(b["hist"] is False for b in page["bars"] if b["timestamp"] >= _month_ms(2024, 6))
    assert page["history"]["check"]["months"] == 7  # June to November whole and exact; December 0.2 % apart
    assert page["history"]["check"]["agrees"] is True


def test_with_no_daily_bars_yet_the_months_and_years_are_shiller_alone(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.watch("massive", ["SPX"], "minute", asset="index")
        _shiller(s, {**VALUES, (2024, 12): 2996.1136363636365})  # the mirror's recent months are unrounded
        reader = WindowReader(BarEngine(s))
        assert [lv["type"] for lv in reader.levels("massive", "SPX")] == ["month", "year"]
        page = _bars(reader, "month")
        assert len(page["bars"]) == len(VALUES) and all(b["hist"] for b in page["bars"])
        assert page["history"]["seam"] is None and page["history"]["check"]["months"] == 0
        meta = reader.meta("massive", "SPX")
        assert meta["levels"][0]["type"] == "month"
        assert meta["pricePrecision"] == 2  # Shiller's unrounded averages (2996.1136...) set no decimals


def test_symbols_without_a_history_and_a_dataset_not_held_are_as_before(tmp_path):
    with Store(tmp_path / "m.db", check_same_thread=False) as s:
        s.watch("massive", ["SPX", "IXIC"], "minute", asset="index")
        _days(s, "2024-11-01", "2024-11-30", lambda d: 5000.0)
        s.upsert_bars("massive", "IXIC", "day", 1, [{"t": _month_ms(2024, 11) + 86_400_000 * 3, "o": 1, "h": 1, "l": 1,
                                                     "c": 1, "v": 1, "vw": 1, "n": 1}])
        reader = WindowReader(BarEngine(s))
        for ticker in ("SPX", "IXIC"):  # Shiller not fetched; IXIC has none
            page = _bars(reader, "month", ticker)
            assert "history" not in page and all("hist" not in b for b in page["bars"])
            assert {lv["type"]: lv["first"] for lv in reader.levels("massive", ticker)}["month"] >= _month_ms(2024, 11)


def test_records_carry_the_flag_as_a_boolean():
    idx = pd.DatetimeIndex([pd.Timestamp("1871-01-01", tz=ET)])
    frame = pd.DataFrame({"open": [4.44], "high": [4.44], "low": [4.44], "close": [4.44], "volume": [0.0],
                          "turnover": [0.0], "trades": [0], "hist": [True]}, index=idx)
    assert to_records(frame)[0]["hist"] is True


# -- the chart page (no JavaScript runtime in the suite: these read the sources) -----------------------
STATIC = pathlib.Path(server.__file__).parent / "static"


def _function(src: str, name: str) -> str:
    return src.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]


def test_the_chart_draws_the_months_as_a_gold_line_marked_monthly_and_says_how_they_agree():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "state.history = res.history || null;" in app  # each page's note: what it is, the seam, the check
    assert "if (type === 'backward' && res.history) state.history = res.history;" in app
    marks = app.split("kc.registerIndicator({\n  name: 'DERIVED_MARKS',", 1)[1].split("kc.registerIndicator(", 1)[0]
    assert "else if (data[i]?.hist) hist++;" in marks and "const total = to - from - slots - hist;" in marks
    assert "if (hist) drawHistory(ctx, xAxis, yAxis, data, from, to);" in marks
    assert "if (!d?.derived || d.interp || d.wide || d.hist) continue;" in marks  # no rings on the feed's months
    draw = _function(app, "drawHistory")
    assert "ctx.setLineDash([]);" in draw  # a dash left on the shared canvas made it look dashed
    assert "ctx.strokeStyle = HIST_COLOR;" in draw and "const x = px(xAxis, i), y = yAxis.convertToPixel(d.close);" in draw
    assert "if (pen && d) ctx.lineTo(px(xAxis, i), yAxis.convertToPixel(d.open));" in draw  # joined at the seam
    assert "const HIST_COLOR = '#d4a72c';" in app
    # a symbol whose history spans 150 years opens at years: the model window's span read "NaNd" there
    assert "month: 2592000, year: 31536000 }[p.type] * p.span;" in _function(app, "windowSpan")
    # nor are sessions shaded inside year candles (their days drew as a dotted band over the last years)
    timeaxis = (STATIC / "timeaxis.js").read_text(encoding="utf-8")
    assert "if (['day', 'week', 'month', 'year'].includes(state.period.type) || !sess.list.length) return true;" in timeaxis
    chip = _function(app, "renderHistoryChip")
    assert "chip.classList.toggle('warn', off);" in chip and "chip.textContent = off ? 'monthly* ⚠' : 'monthly*';" in chip
    title = _function(app, "historyTitle")
    assert "All within ${c.tolerance}%: they agree." in title and "Not checked yet" in title
    panels = (STATIC / "panels.js").read_text(encoding="utf-8")
    assert "b.hist ? ' <span class=\"derived-tag hist\"" in panels  # the cursor's bar says so too
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".derived-tag.hist, .history-chip { color: #d4a72c; border-color: #d4a72c; }" in css
    assert ".history-chip.warn {" in css

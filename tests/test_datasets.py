"""Research datasets: parsers, one file per provider, closed files, refresh cadence."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from massive_collate import datasets
from massive_collate.datasets import DATASETS, due_files, fetch, parse_cot, parse_french, status
from massive_collate.storage import Store

TODAY = date(2026, 10, 23)

FRENCH_DAILY = """This file was created by CMPT_ME_BEME_RETS_DAILY using the 202608 CRSP database.
The Tbill return is the simple daily rate.

,Mkt-RF,SMB,HML,RF
19260701,    0.10,   -0.25,   -0.27,    0.01
19260702,    0.45,   -0.33,   -0.06,    0.01
19260706,  -99.99,    0.12,   -0.11,    0.01

Copyright 2026 Kenneth R. French
"""

FRENCH_MONTHLY = """This file was created by CMPT_ME_BEME_RETS using the 202608 CRSP database.

,Mkt-RF,SMB,HML,RF
192607,    2.89,   -2.55,   -2.39,    0.22
192608,    2.64,   -1.14,    3.81,    0.25

 Annual Factors: January-December
,Mkt-RF,SMB,HML,RF
1927,   29.47,   -2.04,   -3.81,    3.12
"""

INDUSTRIES = """  Average Equal Weighted Returns -- Daily
,Agric,Food ,Soda
19260701,    9.99,    9.99,    9.99

  Average Value Weighted Returns -- Daily
,Agric,Food ,Soda
19260701,    0.56,   -0.12,  -99.99
19260702,    1.20,    0.34,    0.10
"""

COT_HEADER = ("Market and Exchange Names,As of Date in Form YYMMDD,As of Date in Form YYYY-MM-DD,"
              "CFTC Contract Market Code,Open Interest (All),Noncommercial Positions-Long (All),"
              "Noncommercial Positions-Short (All),Noncommercial Positions-Spreading (All),"
              "Commercial Positions-Long (All),Commercial Positions-Short (All),"
              "Nonreportable Positions-Long (All),Nonreportable Positions-Short (All)\n")


def _cot(day: str) -> str:
    return COT_HEADER + (f'"WHEAT-SRW - CHICAGO BOARD OF TRADE",{day[2:].replace("-", "")},{day},001602,'
                         "400000,100000,90000,20000,150000,200000,30000,20000\n")


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


class _Resp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class _Session:
    """Answers each URL from `route(url)`: a response, or an exception to raise."""

    def __init__(self, route):
        self.route, self.calls = route, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        r = self.route(url)
        if isinstance(r, Exception):
            raise r
        return r


def _web(url: str):
    if "Factors_daily" in url:
        return _Resp(200, _zip("F-F_Research_Data_Factors_daily.CSV", FRENCH_DAILY))
    if "deacot2026" in url:
        return _Resp(404)  # this year's file not published yet
    if "deacot" in url:
        year = "2010" if "1986_2016" in url else url[-8:-4]
        return _Resp(200, _zip("annual.txt", _cot(f"{year}-03-02")))
    if "constituents" in url:
        return _Resp(200, b"Symbol,Security,GICS Sector,GICS Sub-Industry,Headquarters Location,Date added,CIK,Founded\n"
                          b"AAPL,Apple Inc.,Information Technology,Technology Hardware,Cupertino,1982-11-30,0000320193,1977\n"
                          b"JPM,JPMorgan Chase,Financials,Diversified Banks,New York,1975-06-30,0000019617,2000\n")
    return requests.ConnectionError("unreachable")


def test_french_tables():
    daily = parse_french("x.CSV", FRENCH_DAILY.encode())
    assert daily.series["Mkt-RF"] == ("Mkt-RF", "%", "day")
    mkt = [(ts, v) for s, ts, v in daily.values if s == "Mkt-RF"]
    assert [v for _, v in mkt] == [0.10, 0.45]  # -99.99 is the library's missing value
    assert mkt[0][0] == int(datetime(1926, 7, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)  # ET midnight
    monthly = parse_french("x.CSV", FRENCH_MONTHLY.encode())
    assert {s: f for s, (_, _, f) in monthly.series.items()} == {"Mkt-RF": "month", "SMB": "month", "HML": "month", "RF": "month"}
    assert len(monthly.values) == 8  # the annual table after it is left out
    vw = parse_french("x.CSV", INDUSTRIES.encode(), section="Average Value Weighted Returns")
    assert sorted(vw.series) == ["Agric", "Food", "Soda"]
    assert ("Agric", vw.values[0][1], 0.56) in vw.values and not any(v == 9.99 for *_, v in vw.values)


def test_cot_series_per_market_and_field():
    parsed = parse_cot("annual.txt", _cot("2026-03-03").encode())
    assert parsed.series["001602:open_interest"] == ("WHEAT-SRW - CHICAGO BOARD OF TRADE: open interest", "contracts", "week")
    assert ("001602:commercial_short", parsed.values[0][1], 200000.0) in parsed.values
    assert len(parsed.values) == 8


def test_fetch_keeps_each_provider_in_its_own_file(tmp_path):
    session = _Session(_web)
    lines = []
    with Store(tmp_path / "m.db") as store:
        done = fetch(store, ["french-3-factors-daily", "cftc-cot-legacy", "sp500-constituents"], today=TODAY,
                     session=session, log=lines.append)
        assert done == {"french-3-factors-daily": 1, "cftc-cot-legacy": 10, "sp500-constituents": 1}
        assert any("deacot2026.zip not published yet" in l for l in lines)
        files = store.dataset_files("cftc-cot-legacy")
        assert len(files) == 10 and all(f["closed"] for f in files.values())  # past years and the archive: final
        assert not store.dataset_files("french-3-factors-daily")[datasets.FRENCH + "F-F_Research_Data_Factors_daily_CSV.zip"]["closed"]
        series = {r["series"]: r for r in store.dataset_series("cftc", "cftc-cot-legacy")}
        assert series["001602:open_interest"]["n"] == 10  # one value per year file
        assert store.dataset_values("french", "french-3-factors-daily", "SMB")["value"].tolist() == [-0.25, -0.33, 0.12]
        assert store.constituents("sp500", "S&P 500")["AAPL"]["sector"] == "Technology"  # GICS mapped to the map's names
        assert store.quality("cftc-cot-legacy", "dataset")["score"] == 1.0  # official
        assert store.quality("french-3-factors-daily", "dataset")["score"] == 0.85  # the researcher's own library
        assert store.quality("sp500-constituents", "dataset")["score"] == 0.8  # a mirror
        rows = {d["name"]: d for d in status(store, TODAY)}
        assert rows["cftc-cot-legacy"]["closed"] == 10 and rows["sp500-constituents"]["members"] == 2
        assert rows["shiller-us-market-monthly"]["files"] == 0
    assert sorted(f.name for f in (tmp_path / "datasets").glob("*.db")) == ["cftc.db", "french.db", "sp500.db"]

    with Store(tmp_path / "m.db") as store:  # again: closed files never asked, the open ones not yet due
        again = _Session(_web)
        fetch(store, ["cftc-cot-legacy", "french-3-factors-daily"], today=TODAY + timedelta(days=3), session=again,
              log=lines.append)
        assert again.calls == [datasets.CFTC + "deacot2026.zip"]  # the one never fetched
        fetch(store, ["french-3-factors-daily"], today=TODAY + timedelta(days=31), session=again, log=lines.append)
        assert again.calls[-1].endswith("F-F_Research_Data_Factors_daily_CSV.zip")  # due after 30 days


def test_a_failing_file_leaves_the_others(store):
    lines = []
    done = fetch(store, ["shiller-us-market-monthly", "sp500-constituents"], today=TODAY, session=_Session(_web),
                 log=lines.append)
    assert done == {"shiller-us-market-monthly": 0, "sp500-constituents": 1}
    assert any("not fetched (unreachable); tried again next time" in l for l in lines)
    assert store.dataset_files("shiller-us-market-monthly") == {}  # not marked: asked again next time


def test_scheduled_refresh_never_starts_a_dataset(store):
    session = _Session(_web)
    fetch(store, today=TODAY, session=session, log=lambda *_: None, only_started=True)
    assert session.calls == []
    fetch(store, ["sp500-constituents"], today=TODAY, session=session, log=lambda *_: None)
    fetch(store, today=TODAY + timedelta(days=8), session=session, log=lambda *_: None, only_started=True)
    assert len(session.calls) == 2 and all("constituents" in c for c in session.calls)


def test_due_files_force_skips_closed(store):
    ds = DATASETS["cftc-cot-legacy"]
    fetch(store, ["cftc-cot-legacy"], today=TODAY, session=_Session(_web), log=lambda *_: None)
    assert due_files(store, ds, TODAY, force=True) == [(datasets.CFTC + "deacot2026.zip", False)]
    with pytest.raises(ValueError):
        store.save_dataset("../x", "x", datasets.Parsed())


def test_commodities_skip_missing_cells():
    parsed = datasets.parse_commodities("c.csv", b"Date,All Commodity Price Index,Gold\n1980-02-01,nan,35.2\n"
                                                 b"1980-03-01,101.5,\n")
    assert sorted((s, v) for s, _, v in parsed.values) == [("All Commodity Price Index", 101.5), ("Gold", 35.2)]
    assert parsed.series["Gold"][1] == "USD (nominal)" and parsed.series["All Commodity Price Index"][1] == "index (2005 = 100)"


def test_shiller_zeros_are_missing_and_a_one_file_dataset_is_replaced(store):
    first = (b"Date,SP500,Earnings,PE10\n1871-01-01,4.44,0.4,0\n2026-08-01,7711.32,0,0\n")
    second = (b"Date,SP500,Earnings,PE10\n1871-01-01,4.44,0.4,0\n2026-08-01,7711.32,0,0\n2026-09-01,7669.41,0,0\n")
    files = iter([first, second])
    session = _Session(lambda url: _Resp(200, next(files)))
    fetch(store, ["shiller-us-market-monthly"], today=TODAY, session=session, log=lambda *_: None)
    series = {r["series"]: r["n"] for r in store.dataset_series("shiller", "shiller-us-market-monthly")}
    assert series == {"SP500": 2, "Earnings": 1}  # PE10 had only zeros: no values, no series
    store.save_dataset("shiller", "shiller-us-market-monthly", datasets.Parsed(values=[("Old", 0, 1.0)],
                                                                               series={"Old": ("Old", "", "month")}))
    fetch(store, ["shiller-us-market-monthly"], today=TODAY, session=session, log=lambda *_: None, force=True)
    series = {r["series"]: r["n"] for r in store.dataset_series("shiller", "shiller-us-market-monthly")}
    assert series == {"SP500": 3, "Earnings": 1}  # replaced whole: the withdrawn series is gone


def test_chart_app_lists_datasets_and_maps_members_by_gics(tmp_path):
    import json

    from massive_collate.gui.server import ChartApp

    s = Store(tmp_path / "m.db", check_same_thread=False)
    fetch(s, ["sp500-constituents"], today=TODAY, session=_Session(_web), log=lambda *_: None)
    s.watch("massive", ["JPM"], "minute")
    app = ChartApp(s)
    rows = {d["name"]: d for d in json.loads(json.dumps(app.datasets()))["datasets"]}
    assert rows["sp500-constituents"]["members"] == 2 and rows["cftc-cot-legacy"]["files"] == 0
    jpm = next(x for x in app.symbols() if x["ticker"] == "JPM")
    assert (jpm["sector"], jpm["sectorBasis"]) == ("Financial", "GICS (S&P 500 list)")
    s.close()


def test_viewer_endpoints(tmp_path):
    from massive_collate.gui.server import ChartApp

    s = Store(tmp_path / "m.db", check_same_thread=False)
    fetch(s, ["cftc-cot-legacy", "french-3-factors-daily"], today=TODAY, session=_Session(_web), log=lambda *_: None)
    app = ChartApp(s)
    markets = app.dataset_series("cftc-cot-legacy")["markets"]
    assert [(m["code"], m["name"], m["openInterest"]) for m in markets] == [
        ("001602", "WHEAT-SRW - CHICAGO BOARD OF TRADE", 400000.0)]  # the name without the field
    assert [x["series"] for x in app.dataset_series("french-3-factors-daily")["series"]] == ["HML", "Mkt-RF", "RF", "SMB"]
    got = app.dataset_values("french-3-factors-daily", "Mkt-RF,SMB")["series"]
    assert got["SMB"]["v"] == [-0.25, -0.33, 0.12] and got["SMB"]["unit"] == "%" and len(got["SMB"]["t"]) == 3
    cot = app.dataset_values("cftc-cot-legacy", "001602:noncommercial_long")["series"]["001602:noncommercial_long"]
    assert cot["v"] == [100000.0] * 10 and cot["freq"] == "week"
    with pytest.raises(KeyError):
        app.dataset_values("french-3-factors-daily", "Nope")
    with pytest.raises(LookupError):
        app.dataset_series("no-such-dataset")
    s.close()

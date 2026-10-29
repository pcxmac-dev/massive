"""Heat maps for every exchange, divided into sectors, for the futures exchanges and for the watchlists
(the user, 2026-10-28: "i want heat maps for futures all the different exchanges"; "so every stock on the
heat map needs to be updated daily"; "also a heatmap for the watchlist"; "or watchlists"; "lets get heat
maps for all the exchanges (and relevent sectors divided in) as well as watchlist(s) so when i open the
symbol picker or watchlist picker, its easier to figure")."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from massive_collate.gui import server
from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store

STATIC = Path(server.__file__).parent / "static"
ET_MIDNIGHT = lambda d: int(datetime(d.year, d.month, d.day, 4).timestamp() * 1000)  # noqa: E731 (EDT)


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    return src.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "m.db", check_same_thread=False)
    s.update_directory("massive", "future", [
        {"ticker": "ES", "name": "E-mini S&P 500", "exchange": "XCME", "type": "us_index"},
        {"ticker": "BZ", "name": "Brent Crude Oil Last Day Financial Futures", "exchange": "XNYM", "type": "crude_oil"},
        {"ticker": "NG", "name": "Henry Hub Natural Gas", "exchange": "XNYM", "type": "nat_gas"}], date(2026, 10, 20))
    s.watch("massive", ["ES1!"], "minute", asset="future", source_ticker="ES", rank=1)
    s.watch("massive", ["NG1!"], "minute", asset="future", source_ticker="NG", rank=1)
    # ES held September's contract, then December's: the move is December's
    s.set_rolls("massive", "ES", 1, [(date(2026, 9, 1), "ESU6"), (date(2026, 9, 18), "ESZ6"), (date(2026, 10, 27), "ESZ6")])
    s.set_rolls("massive", "BZ", 1, [(date(2026, 10, 27), "BZF7")])
    for contract, closes in (("ESZ6", (6000.0, 6060.0)), ("ESU6", (5000.0, 4000.0)), ("BZF7", (60.0, 63.0))):
        s.upsert_bars("massive", contract, "day", 1, [
            {"t": ET_MIDNIGHT(date(2026, 10, 26)), "o": closes[0], "h": closes[0], "l": closes[0], "c": closes[0], "v": 1, "vw": closes[0], "n": 1},
            {"t": ET_MIDNIGHT(date(2026, 10, 27)), "o": closes[1], "h": closes[1], "l": closes[1], "c": closes[1], "v": 1, "vw": closes[1], "n": 1}])
    # the stock BZ (Kanzhun) moved -10 % in the stocks' whole-market file
    s.save_market_snapshot("massive", date(2026, 10, 26), [{"T": "BZ", "c": 20.0, "v": 1e6, "vw": 20.0}])
    s.save_market_snapshot("massive", date(2026, 10, 27), [{"T": "BZ", "c": 18.0, "v": 1e6, "vw": 18.0}])
    yield s
    s.close()


def test_each_continuous_future_reads_its_move_from_the_contract_it_holds_now(store):
    assert store.front_contracts()[("massive", "ES", 1)] == "ESZ6"
    syms = {x["ticker"]: x for x in json.loads(json.dumps(ChartApp(store).symbols()))}
    es = syms["ES1!"]
    assert (es["exchange"], es["futuresType"], es["front"]) == ("XCME", "us_index", "ESZ6")
    assert es["close"] == 6060.0 and es["changePct"] == pytest.approx(1.0)  # December's +1 %, not September's -20 %
    ng = syms["NG1!"]  # no contract fetched yet: its exchange and type, no move (grey on the map)
    assert (ng["exchange"], ng["futuresType"], ng["changePct"], ng["close"]) == ("XNYM", "nat_gas", None, None)


def test_a_futures_root_sharing_a_stocks_ticker_is_not_that_stock(store):
    brent = next(x for x in ChartApp(store).directory()["items"] if x["ticker"] == "BZ")
    assert brent["kind"] == "future"
    assert brent["changePct"] == pytest.approx(5.0) and brent["close"] == 63.0  # its front contract's move, not -10 %
    assert brent["dollarVolume"] is None  # the stock's volume is not Brent's
    assert store.directory(kind="future")[0]["close"] is None  # the stocks' file is joined to stocks only


def test_the_map_divides_into_any_levels_and_carries_each_groups_move():
    tm = _js("treemap.js")
    render = tm.split("function render(", 1)[1].split("\n  // A strip", 1)[0]
    assert "const levels = opts.levels || [(s) => s.sector, (s) => s.industry];" in render  # the old map's levels by default
    assert "const size = opts.size || ((s) => s.marketCap);" in render
    assert "if (i < levels.length - 1) node = node.get(name) || node.set(name, new Map()).get(name);" in render
    assert "const top = depth === 0 && levels.length > 2;  // the exchanges round their sectors" in render
    assert "draw(g.child, depth + 1, r.x + 1, r.y + head, r.w - 3, r.h - head - 3);" in render
    # a group's heading: its name and its tiles' size-weighted move
    assert "w += s.value; m += s.value * s.changePct;" in render and '<span class="map-move">' in render
    assert "XNAS: 'Nasdaq', XNYS: 'NYSE', XASE: 'NYSE American', ARCX: 'NYSE Arca'" in tm
    assert "XCME: 'CME', XCBT: 'CBOT', XNYM: 'NYMEX', XCEC: 'COMEX'" in tm
    assert "Energy: ['crude_oil', 'refined_products', 'nat_gas'" in tm and "Rates: ['long_term_gov'" in tm
    assert "window.MarketMap = { render, renderStrip, squarify, changeColor, exchangeName, futuresFamily, futuresTypeName };" in tm


def test_the_symbol_picker_maps_every_exchange_by_sector_the_watchlists_and_the_futures():
    app = _js("app.js")
    bar = _function(app, "mapBar")
    assert "chip('all', 'All exchanges', items.length, 'Every exchange, each divided into its sectors')" in bar
    assert "data-map-size=" in bar and 'id="mapList"' in bar and "data-map-priced" in bar
    levels = _function(app, "mapLevels")
    assert "const levels = chosen === 'all' && held.size > 1 ? [(s) => s.exchange || '', ...inner] : inner;" in levels
    maps = _function(app, "renderMapList")
    assert "const sized = byVolume ? (s) => s.dollarVolume > 0 : (s) => s.marketCap > 0 && s.sector;" in maps
    assert "mapLevels(candidates, 'mapExchange', [(s) => s.sector, (s) => s.industry])" in maps
    assert "const named = !dir && state.mapList ? (window.Watchlist?.lists?.() || []).find((l) => l.name === state.mapList) : null;" in maps
    assert "size: byVolume ? (s) => s.dollarVolume : (s) => s.marketCap" in maps
    assert "sector: s.sector || (byVolume ? UNCLASSIFIED : null)" in maps  # every stock that traded is on the map
    futures = _function(app, "renderFuturesMap")
    assert "sector: M.futuresFamily(s.futuresType), industry: M.futuresTypeName(s.futuresType)" in futures
    assert "mapLevels(base, 'futuresExchange', [(s) => s.sector, (s) => s.industry])" in futures
    assert "M.render(map, mapped, { current: state.ticker, levels, label, size: () => 1 });" in futures
    lists = _function(app, "renderSymbolList")
    assert "if (state.switcherScope === 'futures') {\n      renderFuturesMap(query);" in lists
    assert "const choice = e.target.closest('[data-map-exchange], [data-map-size], [data-map-priced]');" in app
    assert "if (e.target.id !== 'mapList') return;" in app
    for key in ("mapExchange", "futuresExchange", "mapSize", "mapList", "futuresPriced"):
        assert f"prefs.get('{key}'" in app  # each choice kept


def test_the_watchlist_panel_shows_its_list_as_a_heat_map():
    wl = _js("watchlist.js")
    assert "let view = prefs.get('watchView', 'list');" in wl
    assert "if (view === 'map') {" in wl and "renderMap(box, head, syms.filter(match), empty);" in wl
    group = wl.split("function mapGroup(", 1)[1].split("\n  }\n", 1)[0]
    assert "if (sym.asset === 'stock') return [sym.sector || 'Not classified yet', sym.industry || ''];" in group
    assert "`Futures · ${M.futuresFamily(sym.futuresType)}`" in group
    draw = wl.split("function renderMap(", 1)[1].split("\n  }\n", 1)[0]
    assert "changePct: q.pct, close: q.last" in draw  # live where it streams, as the table
    assert "const size = items.length <= MAP_EQUAL ? () => 1" in draw
    # the activity polls draw it again at most every MAP_REDRAW_MS (thousands of tiles)
    assert "if (view === 'map' && Date.now() - mapDrawn < MAP_REDRAW_MS && document.activeElement?.id !== 'watchFilter') return;" in wl
    assert "const tile = e.target.closest('.map-tile[data-ticker]');" in wl
    assert "window.Watchlist = { render, load, lists: () => lists };" in wl
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".market-map.watch-map {" in css and ".map-chip.active {" in css and ".map-sector.map-top {" in css


def test_every_future_is_coloured_by_the_same_session_and_one_behind_is_grey_with_its_date(store):
    # the user's map, 2026-10-29: CL's newest bar was from Oct 23, ES's from Oct 27, GC's from Oct 28
    store.watch("massive", ["CL1!"], "minute", asset="future", source_ticker="CL", rank=1)
    store.watch("massive", ["SR31!"], "minute", asset="future", source_ticker="SR3", rank=1)
    store.set_rolls("massive", "CL", 1, [(date(2026, 10, 23), "CLZ6")])
    store.set_rolls("massive", "SR3", 1, [(date(2026, 10, 28), "SR3Z6")])
    for contract, days in (("CLZ6", ((22, 99.1), (23, 103.44))), ("SR3Z6", ((26, 95.0), (27, 95.95), (28, 99.0)))):
        store.upsert_bars("massive", contract, "day", 1, [
            {"t": ET_MIDNIGHT(date(2026, 10, d)), "o": c, "h": c, "l": c, "c": c, "v": 1, "vw": c, "n": 1} for d, c in days])
    syms = {x["ticker"]: x for x in ChartApp(store).symbols()}
    # Oct 27: the session most fronts' newest bar is from (ES and BZ), against Oct 26
    assert (syms["ES1!"]["session"], syms["ES1!"]["changePct"]) == ("2026-10-27", pytest.approx(1.0))
    # a front a session ahead is read on that session too (Oct 27 against Oct 26), not on its newest
    sr3 = syms["SR31!"]
    assert sr3["changePct"] == pytest.approx(1.0) and sr3["close"] == 95.95 and sr3["day"] == "2026-10-28"
    # a front behind: no change (its +4.38 % was Oct 23's), its newest bar's day to show
    cl = syms["CL1!"]
    assert (cl["changePct"], cl["session"], cl["day"], cl["close"]) == (None, None, "2026-10-23", 103.44)


def test_the_futures_map_dates_a_front_behind_and_an_empty_watchlist_says_so():
    app = _js("app.js")
    futures = _function(app, "renderFuturesMap")
    assert "const session = state.symbols.find((s) => s.asset === 'future' && s.session)?.session || null;" in futures
    assert "asOf: behind(s) ? dayText(s.day) : null" in futures
    assert "const priced = all.filter((s) => s.close != null);" in futures  # a front behind stays on the priced map, grey
    assert "'s session, each front contract (grey with a date: its newest bar is older)" in futures
    tm = _js("treemap.js")
    assert "const sub = pct == null && s.asOf ? s.asOf : pctText(pct);" in tm and "${s.note ? `\\n${s.note}` : ''}" in tm
    wl = _js("watchlist.js")
    # the user, 2026-10-29: "even empty watch list": the map says why it is empty, as the table does
    assert "renderMap(box, head, syms.filter(match), empty);" in wl
    assert "mine ? 'Empty: open a symbol and press + to add it.'" in wl and "'Nothing visited yet: every symbol you open is noted here.'" in wl
    draw = wl.split("function renderMap(", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!syms.length) {" in draw and 'id="watchPick"' in draw and "Pick from the market map" in draw
    assert "if (e.target.closest('#watchPick')) {" in wl and "return $('symbolBtn').click();" in wl
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".w-map-empty {" in css and ".w-map-empty button {" in css

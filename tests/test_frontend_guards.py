"""Guards on the chart page's scripts, from a bug sweep of the front end (there is no JavaScript
runtime in the test suite, so these read the sources): requests the server would refuse, links
from feeds, and redraws that must not move the chart."""
from __future__ import annotations

import re
from pathlib import Path

from massive_collate.gui import server

STATIC = Path(server.__file__).parent / "static"


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_the_time_axis_asks_for_calendars_the_server_answers():
    days = int(re.search(r"const CALENDAR_DAYS = (\d+);", _js("timeaxis.js")).group(1))
    assert days < server.CALENDAR_MAX_DAYS  # a wider request failed, and was asked again every frame


def test_a_failed_calendar_request_is_not_redrawn_into_another():
    loader = _js("timeaxis.js").split("function loadSessions()", 1)[1].split("state.sessions = sess", 1)[0]
    failure = loader.split(".catch(", 1)[1].split(".finally(", 1)[0]
    assert "redraw()" not in failure and "sess.failed" in failure
    assert "CALENDAR_RETRY_MS" in loader


def test_a_redraw_does_not_reset_the_scroll_position():
    redraw = _js("timeaxis.js").split("function redraw()", 1)[1].split("\n  }\n", 1)[0]
    assert "setOffsetRightDistance" not in redraw  # read back as 0 when scrolled into history: a jump to now


def test_links_from_feeds_are_web_addresses_only():
    for name in ("news.js", "activity.js", "globe.js"):
        src = _js(name)
        assert "href=\"${esc(it.url)}\"" not in src, name
        assert "safeUrl(it.url)" in src, name
    assert re.search(r"const safeUrl = .*https\?", _js("icons.js"))


def test_one_outage_is_one_step_of_the_request_pause():
    failed = _js("app.js").split("function apiFailed(", 1)[1].split("\n}\n", 1)[0]
    assert failed.index("Date.now() < f.until") < failed.index("f.count += 1")


def test_render_loop_loaders_are_rate_limited():
    globe = _js("globe.js")
    assert "const loadTopo50 = loader(" in globe and "const loadHiresIndex = loader(" in globe
    assert "tilesLoading >= 4" in globe and "tileFailed" in globe
    assert "CACHE_MAX" in _js("globeicon.js")


def test_the_market_map_is_drawn_once_the_dialog_has_a_size():
    app = _js("app.js")
    opening = app.split("function openSymbolDialog()", 1)[1].split("\n}\n", 1)[0]
    assert opening.index("showModal()") < opening.index("renderSymbolList(")  # open first: a closed dialog is 0 px wide
    drawing = app.split("function renderMapList(", 1)[1].split("\n}\n", 1)[0]
    assert drawing.index("clientWidth") < drawing.index("MarketMap.render(")  # never laid out at no size


def _function(src: str, name: str) -> str:
    return src.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]


def test_bricks_keep_their_level_whatever_the_zoom():
    app = _js("app.js")
    assert "if (PRICE_BASED.has(state.chartType)) return;" in _function(app, "checkLod")
    loader = app.split("chart.setDataLoader(", 1)[1]
    assert "res.level?.fixed" in loader  # the level the server built them from, shown as the chart's


def test_a_level_where_the_model_draws_nothing_is_passed_over():
    app = _js("app.js")
    assert "emptyLevels()" in _function(app, "chooseLevel")
    assert "skipEmptyLevel()" in app.split("chart.setDataLoader(", 1)[1]


def test_the_auto_chart_loads_a_model_without_a_whole_history_probe():
    probe = _function(_js("app.js"), "reloadWithModelPrecision")
    assert probe.index("state.meta?.auto") < probe.index("api('/api/bars'")  # the probe has no auto: the series path


def test_zooming_with_a_model_reloads_only_for_more_decimals():
    assert "want < sym.pricePrecision" in _function(_js("app.js"), "applyModelPrecision")


def test_the_detail_slider_sets_the_narrowest_candle_and_its_middle_is_the_old_setting():
    app = _js("app.js")
    width = _function(app, "autoMinPx")
    assert "2 * Math.pow(2, (50 - state.autoDetail) / 25)" in width  # 50: 2 px (as before); 100: 0.5 px; 0: 8 px
    assert "prefs.get('autoDetail', 50)" in app
    assert "chartWidth() / autoMinPx()" in _function(app, "chooseLevel")
    assert "autoPage()" in app.split("chart.setDataLoader(", 1)[1]  # pages grow with the candles a screen holds
    assert re.search(r"Math\.min\(20000,", _function(app, "autoPage")) and server.MAX_LIMIT >= 20000


def test_the_next_hour_level_is_read_ahead_without_touching_the_chart_requests():
    app = _js("app.js")
    ahead = _function(app, "prefetchFiner")
    assert "fetch(`/api/bars?" in ahead and "api(" not in ahead  # a failure must not pause the chart's requests
    assert "state.prefetched.has(key)" in ahead  # once a window
    assert "prefetchFiner(w, cur)" in _function(app, "checkLod")
    assert "Math.max(1000," in _function(app, "autoPage")  # two screens, not 3,000 bars of 4 hours

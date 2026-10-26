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


def test_every_chart_request_says_which_view_it_reads():
    app = _js("app.js")
    assert "...(state.unfiltered ? { unfiltered: 1 } : {})" in _function(app, "typeQuery")  # bars, future, stream
    assert "...typeQuery()" in _function(app, "prefetchFiner")
    assert 'id="unfilteredBtn"' in _js("index.html") and 'id="pPruned"' in _js("index.html")


def test_the_time_zone_wheel_shows_six_and_turns_round_and_round():
    app = _js("app.js")
    assert "const TZ_ROWS = 6;" in app
    assert "Array.from({ length: 27 }, (_, i) => i - 12)" in app  # whole hours, GMT-12 to GMT+14
    cities = re.search(r"const ZONE_CITIES = \{(.+?)\};", app, re.S).group(1)
    assert all(f"'{h}':" in cities or f" {h}:" in cities for h in range(-12, 15))  # every offset has its cities
    turn = app.split("tzMenu.addEventListener('wheel'", 1)[1].split("});", 1)[0]
    assert "% TIMEZONES.length + TIMEZONES.length) % TIMEZONES.length" in turn  # wraps both ways
    assert "e.preventDefault()" in turn
    zone = _function(app, "labelZone")
    assert "if (daily) return" in zone and "return pickedZone();" in zone  # days keep the market's own dates


def test_the_gaps_between_panes_are_the_windows_grey_and_hold_no_crosshair_line():
    app = _js("app.js")
    spacing = _function(app, "applyPaneSpacing")
    assert "separator: { size: PANE_GAP, color: gapColor() }" in spacing and "placeAxisGap();" in spacing
    assert "crosshair" not in spacing  # the library's dashed line, drawn in the panes only
    assert "getPropertyValue('--frame')" in app.split("const gapColor", 1)[1].splitlines()[0]  # the page's grey, between panels
    assert "applyPaneSpacing();" in _function(app, "applyChartType")  # after every theme

def test_the_time_zone_switch_sits_on_the_date_row_centred_on_the_dates():
    place = _function(_js("app.js"), "placeTimezone")
    assert "const mid = (text.marginStart ?? 4) + (text.size ?? 12) / 2;" in place  # where the axis draws its dates
    assert "height = 2 * (mid - PANE_GAP)" in place and "top: `${axis.top + PANE_GAP}px`" in place
    assert "lineHeight: `${height}px`" in place  # the text on the dates' row ...
    assert "corner = Math.max(height, axis.height - PANE_GAP)" in place and "height: `${corner}px`" in place  # ... the corner filled
    assert "width: `${Y_AXIS_WIDTH - TZ_INSET}px`" in place  # set in from the plot


def test_a_roll_marker_names_only_the_contract_it_becomes():
    marker = _js("app.js").split("name: 'rollMarker'", 1)[1].split("});", 1)[0]
    assert "text: r.to," in marker and "gap" not in marker and "r.from" not in marker


def test_the_plot_info_box_is_in_the_price_windows_upper_right_corner():
    render = _js("panels.js").split("function renderCursor()", 1)[1].split("box.innerHTML", 1)[0]
    assert "chart.getSize('candle_pane', 'main')" in render and "volumePane()" not in render
    assert "box.style.right = " in render and "box.style.left" not in render
    assert re.search(r"\.cursor-box \{[^}]*text-align: right", _js("style.css"))


def test_replay_plays_at_the_six_speeds_and_skips_closed_markets():
    replay = _js("replay.js")
    assert "const SPEEDS = [1, 2, 4, 8, 16, 32];" in replay
    assert "const GAP_SKIP_MS = 15 * 60_000;" in replay
    tick = _function(replay, "tick")
    assert "r.clock += (now - r.last) * r.speed" in tick and "if (f.timestamp - r.clock > GAP_SKIP_MS) r.clock = f.timestamp;" in tick
    assert "> r.clock) break;" in tick  # a finer bar shows once its own time has passed


def test_a_replay_shows_nothing_newer_than_it_has_played():
    app = _js("app.js")
    loader = app.split("chart.setDataLoader(", 1)[1]
    assert "if (type === 'backward' && state.replay) { callback([], { backward: false }); return; }" in loader
    assert "if (type === 'init' && state.replay) params.to = state.replay.start;" in loader
    assert "if (state.replay) { closeStream(); return; }" in loader  # no polls or live stream while replaying
    assert "if (state.replay) return;" in _function(app, "checkLod")  # the level holds
    closing = _function(_js("replay.js"), "closeForming")
    assert "r.complete.get(r.forming.timestamp)" in closing  # a closed bar becomes the chart's own


def test_schemes_show_what_the_replay_has_reached_only():
    schemes = _js("schemes.js")
    assert "const seen = (t) => S.shownTo == null || t <= S.shownTo;" in schemes
    for name in ("SCHEME_EQUITY", "SCHEME_CUSUM"):
        calc = schemes.split(f"name: '{name}'", 1)[1].split("calc:", 1)[1].split("}),", 1)[0]
        assert "!seen(d.timestamp)" in calc, name
    assert "seen(p.known)" in schemes  # a swing point once the bar that confirmed it closed
    html = _js("index.html")
    assert 'id="pSchemes"' in html and 'src="schemes.js"' in html and 'src="replay.js"' in html and 'id="replayBtn"' in html


def test_the_trading_view_is_on_the_rail_and_writes_paper_orders_only():
    assert 'id="railTrading"' in _js("index.html") and 'id="paneTrading"' in _js("index.html")
    assert "trading: 'chart'" in _js("panels.js")  # the chart in the main area, the account beside it
    assert "const WORKSPACES = ['stock', 'analysis', 'trading'];" in _js("app.js")  # its own indicators and interval
    trading = _js("trading.js")
    assert "apiWrite('POST', '/api/trading/orders'" in trading  # the write header: no other page can place one
    assert "Live trading is off" in trading


def test_panels_sit_on_the_lighter_grey_as_large_as_what_they_hold():
    css = _js("style.css")
    body = re.search(r"\nbody \{[^}]*\}", css).group(0)
    assert "--frame:" in body and "background: var(--frame)" in body  # the page is the grey
    for theme in ("body.light {", 'body[data-theme="midnight"] {', 'body[data-theme="graphite"] {',
                  'body.light[data-theme="paper"] {'):
        assert "--frame:" in css.split(theme, 1)[1].split("}", 1)[0], theme
    for panel in ("#tools", "#rail"):  # as tall as their tools, not the page
        rule = re.search(re.escape(panel) + r" \{[^}]*\}", css).group(0)
        assert "align-self: flex-start" in rule and "background: var(--card)" in rule, panel
    assert re.search(r"#sidePanel section \{[^}]*background: var\(--card\)", css)  # each section a panel
    assert re.search(r"\.strip \{[^}]*background: var\(--card\)", css) and _js("index.html").count('class="strip"') >= 6
    assert re.search(r"main \{[^}]*gap: var\(--gap\)", css)


def test_the_status_is_polled_fast_only_while_its_views_are_open():
    act = _js("activity.js")
    rule = act.split("const fast = ", 1)[1].split(";", 1)[0]
    assert "viewOpen('fetcher')" in rule and "viewOpen('live')" in rule and "running" not in rule and "job" not in rule
    assert "data?.running || job?.phase === 'waiting' ? POLL_RUN_MS : POLL_SLOW_MS" in act
    assert "const POLL_RUN_MS = 5000;" in act
    once = act.split("  function poll() {", 1)[1].split("\n  }\n", 1)[0]
    assert "if (!polling) polling = pollOnce()" in once  # a poll while one is on its way joins it: one loop
    tail = act.split("async function pollOnce()", 1)[1].split("\n  }\n", 1)[0]
    assert tail.index("clearTimeout(timer);") < tail.index("timer = setTimeout(poll,")  # one timer, never two


def test_the_stored_bar_shows_what_is_held_and_what_is_still_to_fetch():
    act = _js("activity.js")
    bar = act.split("function storedBar(s) {", 1)[1].split("\n  }\n", 1)[0]
    assert "seg('older'" in bar and "seg('held'" in bar and "seg('newer'" in bar
    assert "s.expectedThrough" in bar  # newer data only up to what the plan should have published
    assert "Math.max(w, SPAN_MIN_PCT)" in bar  # a day to fetch shows on a twenty-year line
    assert "${storedBar(s)}" in act
    css = _js("style.css")
    for part, colour in (("held", "--up"), ("older", "--st-wait"), ("newer", "--st-new")):
        assert re.search(r"\.meter\.span > \." + part + r" \{ background: var\(" + colour + r"\)", css), part


def test_the_fetchers_disk_turns_while_fetching_without_restarting_each_poll():
    panels = _js("panels.js")
    assert "icon('disk', RAIL_ICON)" in panels and "dataset.drawn !== fetcherIcon" in panels  # redrawn on change only
    assert "class=\"disk-platter\"" in _js("icons.js") and "class=\"disk-arm\"" in _js("icons.js")
    css = _js("style.css")
    assert re.search(r"#railFetcher\.busy \.disk-platter \{ animation: spin", css)
    assert re.search(r"#railFetcher\.busy \.disk-arm \{ animation: disk-seek 3\.2s ease-in-out infinite alternate", css)


def test_drawings_copy_cut_and_paste_by_keys_and_right_click():
    clip = _js("drawclip.js")
    keys = clip.split("document.addEventListener('keydown'", 1)[1].split("});", 1)[0]
    assert "k === 'c' ? copy() : k === 'x' ? cut() : k === 'v' ? paste()" in keys
    assert "input, select, textarea" in keys and "if (done) e.preventDefault();" in keys  # typing and text copy untouched
    assert "localStorage.setItem(KEY" in clip  # pastes into the other views, symbols and tabs
    assert "chart.convertFromPixel([{ x: at.x, y: at.y }], { paneId: 'candle_pane' })" in clip  # at the pointer
    for act in ("copy", "cut", "paste", "duplicate", "delete"):
        assert f"data-act=\"${{act}}\"" in clip or f"item('{act}'" in clip, act
    events = _function(_js("app.js"), "overlayEvents")
    assert "e.preventDefault?.(); window.DrawingClipboard?.rightClick(e.overlay);" in events  # not deleted by the library
    assert "setTimeout(saveDrawings, 0)" in events  # saved once the drawing is off the chart
    assert 'src="drawclip.js"' in _js("index.html")


def test_the_level_2_and_the_bar_box_follow_the_bar_under_the_cursor():
    # this chart's crosshair event carries the pointer's x and y only: the bar comes from the x
    helper = _function(_js("app.js"), "crosshairBar")
    assert "chart.convertFromPixel([{ x: c.x" in helper and "kLineData" not in helper
    l2 = _js("level2.js")
    assert "crosshairBar(c)?.bar.timestamp" in l2 and "kLineData" not in l2 and "schedule(FOLLOW_MS)" in l2
    assert "cursorIndex = crosshairBar(c)?.index ?? null;" in _js("panels.js")

"""Guards on the chart page's scripts for the front-end findings of the code review (CODE_REVIEW.md,
10-13, 38-44 and the front end's low ones). There is no JavaScript runtime in the test suite, so these
read the sources, as test_frontend_guards.py does."""
from __future__ import annotations

from pathlib import Path

from massive_collate.gui import server

STATIC = Path(server.__file__).parent / "static"


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    """A top-level function's body."""
    return src.split(f"function {name}(", 1)[1].split("\n}\n", 1)[0]


def _inner(src: str, name: str) -> str:
    """The body of a function inside a script's (function () { ... })() wrapper."""
    return src.split(f"function {name}(", 1)[1].split("\n  }\n", 1)[0]


def _loader(app: str) -> str:
    return app.split("chart.setDataLoader(", 1)[1].split("subscribeBar:", 1)[0]


# -- 10: a model chart on a live symbol ------------------------------------------------------------

def test_a_live_model_chart_reloads_at_most_once_a_bar_and_keeps_its_view():
    app = _js("app.js")
    stream = app.split("es.onmessage = ", 1)[1].split("};", 1)[0]
    assert "if (msg.reload) { modelMoved(); return; }" in stream and "resetData" not in stream  # every write: 2 s
    assert "const MODEL_RELOAD_MS = [10_000, 60_000];" in app
    moved = _function(app, "modelMoved")
    assert "if (due.timer) return;" in moved  # one reload waiting, whatever the stream says meanwhile
    assert "Math.min(MODEL_RELOAD_MS[1], Math.max(MODEL_RELOAD_MS[0], bar))" in moved and "due.at + every" in moved
    reload = _function(app, "reloadMovedModel")
    assert reload.index("if (!atNewest() || !chartShown()) return;") < reload.index("chart.resetData()")  # not while scrolled back
    assert "atLatest: true" in reload and "barSpace: chart.getBarSpace().bar" in reload  # the zoom kept (finishLodSwitch)
    assert "chart.subscribeAction('onVisibleRangeChange', modelBackInView);" in app  # back at the newest bar: loaded then


# -- 38: a failed page of history ------------------------------------------------------------------------

def test_a_failed_page_keeps_history_paging_and_a_stale_failure_blanks_nothing():
    app = _js("app.js")
    failure = _loader(app).split("} catch (err) {", 1)[1]
    assert failure.lstrip().startswith("if (token !== state.loadToken) return;")  # the newer symbol's chart kept
    init = failure.split("if (type === 'init') {", 1)[1].split("return;", 1)[0]
    assert "callback([], { forward: false, backward: false });" in init and "retryLoad(token);" in init
    assert failure.count("forward: false") == 1  # a page's failure no longer ends the history
    assert "const failing = apiFailing.has('/api/bars');" in failure
    assert "type === 'forward' ? { forward: failing } : { backward: failing }" in failure
    assert "if (failing) retryPage(token);" in failure
    retry = _function(app, "retryPage")
    assert "chart.scrollByDistance(0)" in retry and "token === state.loadToken" in retry


# -- 44: a symbol pick's loads -----------------------------------------------------------------------------------

def test_a_symbol_pick_sends_one_load_for_the_new_symbol():
    app = _js("app.js")
    loader = _loader(app)
    skip = "if (type === 'init' && (state.settingPeriod || symbol.ticker !== state.ticker || symbol.source !== state.source)) return;"
    assert skip in loader and loader.index(skip) < loader.index("++state.loadToken")
    pick = _function(app, "selectSymbol")
    assert pick.index("state.settingPeriod = true;") < pick.index("chart.setPeriod(state.period)") < pick.index("chart.setSymbol(")
    assert "finally { state.settingPeriod = false; }" in pick


def test_the_model_precision_probe_is_dropped_for_another_model():
    probe = _function(_js("app.js"), "reloadWithModelPrecision")
    assert probe.index("const model = JSON.stringify(modelQuery());") < probe.index("api('/api/bars'")
    assert "JSON.stringify(modelQuery()) !== model) return;" in probe


# -- 11: a layout save on its way ---------------------------------------------------------------------

def test_a_layout_save_keeps_the_changes_made_while_it_was_on_its_way():
    app = _js("app.js")
    save = _function(app, "saveLayout")
    assert "layout.current = saved" not in save  # the server's copy lacked them
    assert "Object.assign(target, { name: saved.name, updated_at: saved.updated_at });" in save
    assert save.index("const gen = layout.generation;") < save.index("target.data = snapshot();")
    assert "if (layout.current === target && layout.generation === gen) layout.dirty = false;" in save
    assert "layout.generation += 1;" in _function(app, "markDirty")
    rename = _function(app, "renameLayout")
    assert "layout.current = await" not in rename and "Object.assign(target, { name: saved.name" in rename


def test_a_layout_is_saved_when_the_page_is_hidden_and_a_big_one_not_only_by_keepalive():
    app = _js("app.js")
    assert "beforeunload" not in app
    assert "document.addEventListener('visibilitychange'" in app and "window.addEventListener('pagehide'" in app
    assert "const KEEPALIVE_MAX = 60 * 1024;" in app
    save = _function(app, "saveLayout")
    assert "leaving && new TextEncoder().encode(JSON.stringify(body)).length <= KEEPALIVE_MAX" in save
    assert "{ keepalive = false } = {}" in app.split("async function apiWrite(", 1)[1].split(")", 1)[0] + ")"
    assert "if (!closing && !layout.autosave) return;" in _function(app, "saveOnLeave")


# -- 12: drawings on indicator panes -------------------------------------------------------------------

def test_drawings_go_back_on_their_indicators_pane():
    app = _js("app.js")
    assert "pane: paneIndicator(o.paneId)" in _function(app, "saveDrawings")  # the indicator's name: pane ids are per session
    assert "...(state.drawingsAside || [])" in _function(app, "saveDrawings")  # kept while its indicator is off
    restore = _function(app, "restoreDrawings")
    assert "const paneId = d.pane ? indicatorPane(d.pane) : undefined;" in restore
    assert "if (d.pane && !paneId) { state.drawingsAside.push(d); continue; }" in restore  # never onto the price pane
    assert "drawSaved(d, paneId);" in restore and "groupId: 'user', paneId," in _function(app, "drawSaved")
    assert "chart.getIndicators({ paneId })" in _function(app, "paneIndicator")
    clip = _js("drawclip.js")
    assert "pane: paneIndicator(o.paneId)" in clip
    place = _inner(clip, "place")
    assert "const paneId = paneFor(it);" in place and "if (!paneId) continue;" in place
    assert "if (id) made++;" in place and "if (!made) return false;" in place
    paste = _inner(clip, "paste")
    assert paste.index("if (!place(c.items, at))") < paste.index("toast(`Pasted the")  # only when one was drawn


# -- 13: the replay's controls ----------------------------------------------------------------------------

def test_the_replays_controls_are_built_when_they_change_only():
    replay = _js("replay.js")
    bar = _inner(replay, "renderBar")
    assert "if (shape !== built) {" in bar and bar.index("if (shape !== built) {") < bar.index("bar.innerHTML")
    assert "const CLOCK_MS = 250;" in replay
    tick = _inner(replay, "tick")
    assert "if (!r.playing) renderBar();" in tick and tick.count("renderBar()") == 1  # not every 50 ms
    assert "else if (now - (r.clockAt || 0) >= CLOCK_MS) { r.clockAt = now; renderClock(); }" in tick
    assert "el.textContent = text" in _inner(replay, "renderClock")


# -- 39: a replay and a switch to a model or a server-built type -------------------------------------------

def test_a_replay_ends_on_a_model_or_a_server_built_chart_type():
    replay = _js("replay.js")
    key = _inner(replay, "replayKey")
    for part in ("state.model.kind", "SERVER_TYPES.has(state.chartType)", "state.adjust", "state.unfiltered"):
        assert part in key, part
    assert "return r && r.key !== replayKey();" in replay and "key: replayKey()," in replay
    assert "const playable = () => !SERVER_TYPES.has(state.chartType) && state.model.kind === 'price';" in replay
    assert "if (!playable()) {" in replay and "if (playable()) begin(ts);" in replay  # Heikin Ashi and box plots too
    assert "window.Replay?.keyChanged()" in _loader(_js("app.js"))  # checked on every load of the chart


# -- 40: a scheme's answer -----------------------------------------------------------------------------------

def test_a_scheme_answer_is_drawn_for_the_symbol_it_was_asked_for_only():
    schemes = _js("schemes.js")
    run = _inner(schemes, "run")
    assert "res.ticker = state.ticker" not in run and "res.ticker = ask.ticker;" in run and "res.source = ask.source;" in run
    assert run.index("const ask = {") < run.index("api('/api/scheme'")
    assert "ask.ticker !== state.ticker || ask.source !== state.source" in run
    on_symbol = schemes.split("window.addEventListener('massive:symbol'", 1)[1].split("});", 1)[0]
    assert "S.token = (S.token || 0) + 1;" in on_symbol  # a run still on its way is dropped
    assert "S.result.source === state.source" in schemes.split("const current = ", 1)[1].split(";", 1)[0]


# -- 41: Delete on another view ----------------------------------------------------------------------------

def test_delete_removes_a_drawing_only_from_the_chart_on_screen():
    keys = _js("app.js").split("document.addEventListener('keydown'", 1)[1].split("\n});\n", 1)[0]
    delete = keys.split("e.key === 'Delete'", 1)[1].split("{", 1)[0]
    assert "document.body.dataset.view === 'chart'" in delete
    show = _inner(_js("panels.js"), "showPane")
    assert "if (view !== 'chart') state.selectedOverlay = null;" in show


# -- 42: a lost WebGL context --------------------------------------------------------------------------------

def test_a_lost_gpu_context_falls_back_to_the_cpu_and_comes_back():
    gpu = _js("gpu.js")
    lost = gpu.split("'webglcontextlost'", 1)[1].split("});", 1)[0]
    assert "e.preventDefault();" in lost and "this.lost = true;" in lost  # or it is never restored
    restored = gpu.split("'webglcontextrestored'", 1)[1].split("});", 1)[0]
    assert "this._setup();" in restored and "this.lost = false;" in restored
    assert "if (this.lost) return;" in gpu and "if (this.lost) return 0;" in gpu
    app = _js("app.js")
    assert "!gpuBars?.lost" in _function(app, "gpuActive")  # applyChartType draws the candles on the CPU
    assert "new window.GpuBars({ onLost: applyChartType, onRestored: applyChartType })" in _function(app, "ensureGpu")
    globe = _js("globe.js")
    lost = globe.split("'webglcontextlost'", 1)[1].split("});", 1)[0]
    assert "e.preventDefault();" in lost and "glr = null;" in lost and "tiles.clear();" in lost
    restored = globe.split("'webglcontextrestored'", 1)[1].split("});", 1)[0]
    assert "new window.GlobeGL(glCanvas)" in restored
    for upload in ("glr.setZones(", "glr.setZoneOffsets(", "glr.setLines('o110'", "glr.setSky("):
        assert upload in restored, upload


# -- 43: the Highlight panel -----------------------------------------------------------------------------------

def test_the_highlight_panel_is_rebuilt_when_the_highlight_changes_only():
    app = _js("app.js")
    figures = app.split("kc.registerOverlay({\n  name: 'measure',", 1)[1].split("\n});\n", 1)[0]
    assert "if (shown !== highlightShown) { highlightShown = shown; scheduleHighlightPanel(); }" in figures
    assert figures.count("scheduleHighlightPanel()") == 1  # not on every redraw (each crosshair move)
    shown = figures.split("const shown = ", 1)[1].split(";", 1)[0]
    for part in ("overlay.id", "pa.value", "pb.value", "${key}", "ext.stats"):
        assert part in shown, part
    assert "addEventListener" not in _function(app, "renderHighlightPanel")
    assert "$('pHighlight').addEventListener('click'" in app and "e.target.closest('#hlRemove')" in app
    # level 2 still hears of each change (level2.js coverMoved)
    assert "window.dispatchEvent(new Event('massive:highlight'))" in _function(app, "scheduleHighlightPanel")


# -- the low ones ----------------------------------------------------------------------------------------------------

def test_the_activity_poll_backs_off_while_the_app_does_not_answer():
    act = _js("activity.js")
    assert "const POLL_FAIL_MAX_MS = 60000;" in act
    once = act.split("async function pollOnce()", 1)[1].split("\n  }\n", 1)[0]
    assert "failedPolls = 0;" in once and "failedPolls += 1;" in once
    assert "Math.min(POLL_FAIL_MAX_MS, every * 2 ** failedPolls)" in once


def test_projected_times_move_on_with_a_new_bar_instead_of_being_asked_again():
    axis = _js("timeaxis.js")
    assert "last.timestamp" not in _inner(axis, "futureKey")
    current = _inner(axis, "current")
    assert "const k = fut.stamps.indexOf(last.timestamp);" in current and "fut.stamps = fut.stamps.slice(k + 1);" in current
    assert "} else if (last && last.timestamp !== fut.lastBar?.timestamp) {" in current  # no data: no bar to compare
    assert "if (fut.ask !== ask) return;" in _inner(axis, "ensure")  # a page counted from the bar before is dropped


def test_filter_boxes_keep_the_caret_where_it_was():
    for name in ("news.js", "gov.js"):
        src = _js(name)
        assert "const { selectionStart: from, selectionEnd: to } = e.target;" in src, name
        assert "box.setSelectionRange(from, to);" in src, name


def test_a_dataset_picked_meanwhile_keeps_its_own_series_list():
    pick = _inner(_js("datasets.js"), "selectDataset")
    assert pick.index("if (st.name !== name) return;") < pick.index("st.catalog = catalog;")


def test_the_now_handle_drag_ends_however_the_pointer_goes():
    drag = _js("app.js").split("nowHandle.addEventListener('pointerdown'", 1)[1].split("\n});\n", 1)[0]
    assert "const ENDS = ['pointerup', 'pointercancel', 'lostpointercapture'];" in drag
    assert "for (const ev of ENDS) nowHandle.removeEventListener(ev, up);" in drag


def test_values_reaching_html_are_escaped():
    assert '<span class="state ${esc(s.state)}">' in _js("activity.js")
    assert "${esc(o.status)} ${esc(fmtTime(o.done_at))}" in _js("trading.js")
    schemes = _js("schemes.js")
    assert 'min="${esc(p.lo)}" max="${esc(p.hi)}" step="${esc(p.step)}"' in schemes
    assert 'value="${esc(cfg.fee)}"' in schemes and 'value="${esc(cfg.slippage)}"' in schemes
    assert "`Paste the ${esc(label(clip().items[0]))}`" in _js("drawclip.js")
    assert 'title="${esc(fresh)} symbols should have new data">${esc(fresh)}</span>' in _js("panels.js")
    assert "list.querySelector(`[data-code=\"${CSS.escape(String(hover.code))}\"]`)" in _js("globe.js")


def test_watchlist_saves_go_one_at_a_time():
    wl = _js("watchlist.js")
    assert "saving = saving.then(async () => {" in wl
    assert "if (waiting === 1) lists = res.lists;" in wl  # an earlier answer lacks the changes still to go


def test_an_indicator_turned_on_brings_back_the_drawings_waiting_for_its_pane():
    app = _js("app.js")
    menu = app.split("cb.addEventListener('change', () => {", 1)[1].split("});", 1)[0]
    assert "drawAside(name)" in menu
    aside = app.split("function drawAside(name) {", 1)[1].split("\n}\n", 1)[0]
    assert "indicatorPane(name)" in aside and "drawSaved(d, paneId)" in aside


def test_a_search_in_the_pickers_market_map_shows_its_matches_and_maps_only_them():
    app = _js("app.js")
    ranked = _function(app, "rankedMatches")
    # the ticker itself first, then starts-with, then the name, then anywhere: the biggest first within each
    assert ranked.index("if (t === Q) return 0;") < ranked.index("if (t.startsWith(Q)) return 1;") \
        < ranked.index("if (n.startsWith(Q)) return 2;") < ranked.index("if (t.includes(Q)) return 3;")
    assert "size(b[1]) - size(a[1])" in ranked
    body = _function(app, "renderMapList")
    assert "rankedMatches(source.map(toItem).concat(everyOther), q)" in body  # those off the map too
    assert "everyMapped.filter(match)" in body and "isMatch" not in body  # the map holds the matches, not dimmed tiles
    assert "hits.slice(0, MAP_HITS).map(hitHtml)" in body
    assert "closest('.map-tile, .map-hit')" in app  # a match picks or opens like a map tile
    enter = app.split("e.key === 'Enter' && e.target === $('symbolSearch')", 1)[1].split("});", 1)[0]
    assert enter.index(".map-hit") < enter.index(".map-tile")  # Enter opens the best match


def test_the_current_times_clock_and_level_2s_times_follow_the_picked_zone():
    app = _js("app.js")
    # the "now" handle's clock at the newest bar: the zone picked under the price axis, not New York's
    assert "nowClock(pickedZone()).format(new Date())" in app and "NOW_CLOCK" not in app
    pick = _function(app, "setChartTimezone")  # the switch under the price axis and Preferences both
    assert "tickNowClock();" in pick  # at once on a new pick
    assert "new CustomEvent('massive:timezone'" in pick
    l2 = _js("level2.js")
    for name in ("when", "spanText"):
        body = _inner(l2, name)
        assert "const zone = pickedZone();" in body and "zoneShort(zone)" in body and " ET`" not in body
    assert "addEventListener('massive:timezone', () => { if (lastProfile) render(lastProfile); })" in l2


def test_the_auto_zoom_holds_at_the_datas_finest_sampling_unless_overridden():
    app = _js("app.js")
    choose = _function(app, "chooseLevel")
    # a level stopping short of the window while a coarser one goes on, or a second level seen drawn
    # wide there, is not chosen; overridden, the second levels reach as far back as the minutes
    assert "if (!wide && (stopsShort(lv, levels, toMs) || (lv.type === 'second' && seenWide(from, toMs)))) continue;" in choose
    assert "const wide = state.autoOverride && lv.type === 'second';" in choose
    assert "Math.min(lv.first, minutesFrom)" in choose
    short = _function(app, "stopsShort")
    assert "lv.last + REACH_SLACK_MS < toMs" in short and "o.last > lv.last + REACH_SLACK_MS" in short
    lod = _function(app, "checkLod")
    assert "if (!state.autoOverride && cur.type === 'second') noteWide(chart.getDataList(), w.lo, w.hi);" in lod
    assert "chooseLevel(w.days, w.fromMs, cur, w.toMs)" in lod
    assert "toMs: last ? last.timestamp" in _function(app, "visibleWindow")
    # the tag: a click (or Enter/Space) overrides, again holds; a new symbol starts held
    toggle = _function(app, "toggleAutoOverride")
    assert "state.autoOverride = !state.autoOverride;" in toggle and "checkLod();" in toggle
    assert "$('periods').addEventListener('click', (e) => { if (e.target.closest('#autoLevel')) toggleAutoOverride(); });" in app
    assert "window.addEventListener('massive:symbol', () => { state.autoOverride = false;" in app
    periods = _function(app, "renderPeriods")
    assert "const under = state.autoOverride && state.wideInView > 0;" in periods
    assert "tag.classList.toggle('under', under);" in periods and "tag.setAttribute('aria-pressed'" in periods
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".auto-level.under { color: #f0a030; animation: auto-under" in css
    assert "@media (prefers-reduced-motion: reduce) { .auto-level.under { animation: none;" in css


def test_wide_bars_are_drawn_once_across_their_slots_and_read_as_their_minute():
    app = _js("app.js")
    marks = app.split("name: 'DERIVED_MARKS'", 1)[1].split("\n});\n", 1)[0]
    assert "if (wide && WIDE_TYPES.has(state.chartType)) drawWide(ctx, c, xAxis, yAxis, data, from, to);" in marks
    assert "noteWideInView(wide);" in marks
    assert "if (!d?.derived || d.interp || d.wide || d.hist) continue;" in marks  # no derived rings under wide slots (or history months)
    draw = _function(app, "drawWide")
    assert "Math.floor(data[j + 1].timestamp / d.wide) * d.wide === start" in draw  # one minute's slots together
    assert "yAxis.convertToPixel(d.wh)" in draw and "fillStyle = plotBackground" not in draw  # nothing painted out
    panels = _js("panels.js")
    stats = _inner(panels, "cursorStats")
    assert "b = { ...b, open: b.wo, high: b.wh, low: b.wl, close: b.wc, volume: full.volume" in stats
    assert "const prev = k > 0 ? list[k - 1] : null;" in stats  # its change from the bar before its first slot
    assert "e(t(barTime(b)))" in panels and "e(t(barTime(s.hi)))" in panels


def test_the_pre_market_and_overnight_shades_are_at_half_their_old_strength_scaled_by_their_sliders():
    axis = _js("timeaxis.js")
    assert "pre: { dark: [10, 32, 96, 0.225], light: [20, 60, 170, 0.065] }," in axis  # half the 0.45 and 0.13 before
    assert "night: { dark: [90, 40, 140, 0.15], light: [110, 60, 170, 0.05] }," in axis  # half the 0.30 and 0.10
    shade = _inner(axis, "shadeOf")
    assert "Math.min(2, Math.max(0, (state.shade?.[name] ?? 100) / 100))" in shade  # 0-200 %, 100 without a setting
    assert "const hex = state.shadeColor?.[name];" in shade  # a colour picked in Preferences, else the theme's own
    assert "const preShade = shadeOf('pre', dark), postShade = shadeOf('post', dark), nightShade = shadeOf('night', dark);" in axis
    assert "window.TimeAxis = { future: fut, sessions: sess, ensure, currentSession, redraw };" in axis


def test_each_sessions_colour_is_picked_from_a_popup_in_preferences():
    prefs = _js("prefs.js")
    assert 'class="pref-swatch" data-color="${k}"' in prefs and 'aria-haspopup="dialog"' in prefs
    opener = _inner(prefs, "openPop")
    assert "pop.setAttribute('role', 'dialog');" in opener
    assert "palette = isCandle(k) ? CANDLE_PALETTE : PALETTE;" in opener and "palette.map((c) =>" in opener
    assert 'type="color" class="pref-pop-any"' in opener and 'class="pref-pop-hex"' in opener
    assert '<button type="button" data-pick="" class="pref-reset">${isCandle(k)' in opener and ": 'Standard'}" in opener  # the theme's own
    color = _inner(prefs, "setColor")
    assert "prefs.set('shadeColor', state.shadeColor);" in color and "window.TimeAxis?.redraw();" in color  # live, kept
    assert "hex.toLowerCase() !== standardOf(k)" in color  # the standard colour picked: none kept (each theme its own)
    # the system's picker live, a typed hex once whole; Escape or a click elsewhere closes it
    assert "if (e.target.classList.contains('pref-pop-any')) { setColor(pop.dataset.color, e.target.value); return; }" in prefs
    assert "if (HEX.test(v)) setColor(pop.dataset.color, v);" in prefs
    assert "if (pop && e.key === 'Escape') { e.stopPropagation(); closePop(true); }" in prefs
    assert "for (const [k] of SHADES) { setShade(k, 100); setColor(k, null); }" in prefs  # the reset: colours too


def test_the_preferences_pane_sits_above_the_profile_below_the_separator_and_sets_sessions_and_shading():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    rail = html.split('<nav id="rail"', 1)[1].split("</nav>", 1)[0]
    last_hr = rail.rindex("<hr>")
    assert last_hr < rail.index('id="railPrefs"') < rail.index('id="railProfile"')  # below the line, above the user
    assert '<div class="pane" id="panePrefs" data-pane="prefs" hidden>' in html
    assert html.index('<script src="app.js"></script>') < html.index('<script src="prefs.js"></script>')
    icons = _js("icons.js")
    assert """sliders: '<path d="M3 6.5h9.6M17.4 6.5H21M3 12h3.6M11.4 12H21M3 17.5h9.6M17.4 17.5H21"/>""" in icons
    panels = _js("panels.js")
    assert "prefs: 'chart'" in panels and "const BESIDE = new Set(['watchlist', 'prefs']);" in panels
    assert "if (view === 'chart' && !BESIDE.has(name)) window.switchWorkspace?.(name);" in panels  # no chart of its own
    assert "$('railPrefs').innerHTML = icon('sliders', RAIL_ICON);" in panels
    prefs = _js("prefs.js")
    assert "state.shade = Object.fromEntries(SHADES.map(([k]) => [k, clamp(Number(saved?.[k] ?? 100))]));" in prefs
    assert "prefs.set('shade', state.shade);" in prefs and "window.TimeAxis?.redraw();" in prefs
    # the session: the toolbar's picker, the same setting
    assert "$('sessionSelect').value = e.target.value;" in prefs and "$('sessionSelect').dispatchEvent(new Event('change'));" in prefs
    assert 'type="range" min="0" max="200" step="5" data-shade=' in prefs


def test_the_time_zone_defaults_to_this_computers_and_is_picked_in_preferences_too():
    app = _js("app.js")
    # none picked: this computer's own zone where the list has it (the user's Pacific time read New York's)
    assert "const LOCAL_ZONE = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; }" in app
    assert "|| (TIMEZONES.includes(LOCAL_ZONE) ? LOCAL_ZONE : 'exchange');" in app
    pick = _function(app, "setChartTimezone")
    assert "prefs.set('timezone', state.timezone);" in pick and "tickNowClock();" in pick
    assert "updateStatus(chart.getDataList().length);" in pick  # the status line's zone
    assert "new CustomEvent('massive:timezone'" in pick
    assert "setChartTimezone(b.dataset.tz);" in app.split("tzMenu.addEventListener('click'", 1)[1].split("\n});\n", 1)[0]
    status = _function(app, "updateStatus")
    assert "zoneShort(labelZone(chart.getPeriod() || state.period))" in status and "'ET'," not in status
    assert "times US/Eastern" not in status
    prefs = _js("prefs.js")
    assert '<select id="prefZone">${TIMEZONES.map((z) =>' in prefs and "z === LOCAL_ZONE ? ' · this computer' : ''" in prefs
    assert "if (e.target.id === 'prefZone') window.setChartTimezone(e.target.value);" in prefs
    assert "window.addEventListener('massive:timezone', again);" in prefs and "const again = () => { if (shown())" in prefs


def test_turning_the_override_off_holds_again_even_mid_switch_and_a_stuck_switch_expires():
    app = _js("app.js")
    toggle = _function(app, "toggleAutoOverride")
    assert "if (state.lodSwitch) state.lodRecheck = true;" in toggle  # off while a switch loads: looked at once it is in
    finish = _function(app, "finishLodSwitch")
    assert "if (state.lodRecheck) {" in finish and "requestAnimationFrame(checkLod);" in finish
    lod = _function(app, "checkLod")
    # a switch whose page never came holds the level LOD_SWITCH_STALE_MS at most
    assert "Date.now() - (state.lodSwitch.at ?? Date.now()) > LOD_SWITCH_STALE_MS) state.lodSwitch = null;" in lod
    assert lod.index("LOD_SWITCH_STALE_MS) state.lodSwitch = null;") < lod.index("if (state.lodSwitch || !state.meta")
    # held: a move to seconds is looked at again once its page is in (wide minutes there: back to minutes)
    assert "if (lv.type === 'second' && !state.autoOverride) state.lodRecheck = true;" in lod
    assert app.count("at: Date.now()") >= 3  # every switch says when it began


def test_hollow_and_volume_candles_draw_wide_minutes_once():
    app = _js("app.js")
    assert "const WIDE_TYPES = new Set(['candles', 'bars', 'hollow', 'volume_candles']);" in app
    custom = app.split("} else if (type === 'hollow' || type === 'volume_candles') {", 1)[1].split("} else if (type === 'kagi')", 1)[0]
    assert "if (d.wide) continue;" in custom  # the custom renderer leaves the wide slots to drawWide
    draw = _function(app, "drawWide")
    assert "state.chartType === 'hollow' && d.wc > d.wo" in draw and "ctx.strokeRect(" in draw
    assert "clearRect" not in draw  # the session shading under it kept


def test_the_stock_panels_data_times_are_in_the_picked_zone():
    app = _js("app.js")
    assert "['Times', 'US/Eastern']" not in app
    assert "['From', esc(zoneTime(data.first))]" in app and "['To', esc(zoneTime(data.last))]" in app
    assert "timeZone: pickedZone()" in _function(app, "zoneTime")
    assert "window.addEventListener('massive:timezone', () => { if (state.info) renderPanel(); });" in app


def test_the_theme_is_picked_in_preferences_too():
    prefs = _js("prefs.js")
    assert '<select id="prefTheme">${Object.entries(THEMES).map(([t, th]) =>' in prefs
    assert "$('themeSelect').value = e.target.value;" in prefs  # the toolbar's picker: the same setting
    assert "$('themeSelect').dispatchEvent(new Event('change'));" in prefs
    assert "window.addEventListener('massive:theme', () => { again(); window.TimeAxis?.redraw(); });" in prefs


def test_the_preferences_pane_draws_when_the_page_opens_on_it_and_a_redraw_drops_its_popup():
    prefs = _js("prefs.js")
    assert "if (shown()) render();  // the page opened on this pane" in prefs  # it was empty after a reload
    assert "if (pop) { pop.remove(); pop = null; }" in _inner(prefs, "render")  # else the next swatch click "closed" it


def test_candles_are_tradingviews_green_and_red_everywhere_unless_picked():
    app = _js("app.js")
    assert "const TV_UP = '#089981';" in app and "const TV_DOWN = '#f23645';" in app
    assert "const UP = '#26a69a'" not in app and "rgba(38,166,154" not in app and "rgba(239,83,80" not in app
    assert "let UP = HEX_COLOR.test(savedCandles?.up || '') ? savedCandles.up.toLowerCase() : TV_UP;" in app
    # the chart library's candles, wicks, borders and last-price mark; the volume pane; after each theme reset
    assert "bar: { ...c.bar, ...candleBar() }" in app and "last: { upColor: UP, downColor: DOWN }" in app
    assert "upWickColor: UP, downWickColor: DOWN" in _function(app, "candleBar")
    assert "upColor: tint(UP, 0.7), downColor: tint(DOWN, 0.7)" in _function(app, "candleStyles")
    chart_type = _function(app, "applyChartType")
    assert chart_type.index("chart.setStyles(themeBase());") < chart_type.index("candleStyles();")
    pick = _function(app, "setCandleColors")
    assert "prefs.set('candleColors'," in pick and "Object.assign(DEFAULT_CANDLE.bar, candleBar());" in pick
    assert "applyChartType();" in pick  # every candle drawn again: the library's, the GPU's, those drawn here
    prefs = _js("prefs.js")
    assert "const CANDLES = [['up', 'Rising'" in prefs and "'#089981'" in prefs and "'#f23645'" in prefs
    assert "window.setCandleColors(k === 'up' ? pick : c.up, k === 'down' ? pick : c.down);" in prefs
    assert "${isCandle(k) ? 'TradingView" in prefs  # the popup's standard: TradingView's pair


def test_a_current_feed_giving_minutes_holds_the_zoom_at_them_across_the_chart_unless_overridden():
    """The user (2026-10-28): "if there is a current feed, do not break under it, unless you override the
    auto button" ... "because its possible for me to see the previous session and then no data for this one"."""
    app = _js("app.js")
    live = _function(app, "liveResolution")
    assert "if (!isLive()) return null;" in live
    assert "const r = s.resolution || (s.polled ? 'minute' : 'second');" in live  # Tiingo's Blue Ocean poller: minutes
    assert "if (res == null || r === 'second') res = r;" in live  # the finest current feed counts
    choose = _function(app, "chooseLevel")
    assert "const minutesLive = !state.autoOverride && liveResolution() === 'minute';" in choose
    assert "if (minutesLive && lv.type === 'second') continue;" in choose  # nowhere under them, any window
    activity = app.split("window.addEventListener('massive:activity', () => requestAnimationFrame(() => {  // live or not, again", 1)[1]
    assert "if (res !== state.liveRes) { state.liveRes = res; renderPeriods(); autoSoon(); }" in activity.split("}));", 1)[0]


def test_every_open_symbol_listens_for_new_bars_while_the_page_is_in_view():
    """The user (2026-10-28): "when new bars are rendered for the current symbol i need to see them update
    the screen and show them": a symbol not streaming live was polled once a minute."""
    app = _js("app.js")
    poll = app.split("state.poll = setInterval(() => {", 1)[1].split("}, LIVE_POLL_MS);", 1)[0]
    assert "const listen = !document.hidden;" in poll
    assert "if (listen && !state.stream && !state.streamFailed) openStream();" in poll
    assert "if (!listen && state.stream) closeStream();" in poll
    assert "if (live && !state.stream" not in poll  # not the live symbols only


def test_the_open_symbols_logo_stays_still_and_half_a_globe_turns_round_it_with_faded_edges():
    """The user (2026-10-28): "use the old school explorer icon + the current symbol icon scheme"; "keep the
    status on the bottom, but lets animate the current symbol icon"; "i need the appearance of a sphere
    rotating"; "its gotta look just as smooth"; "blue and gold like, ... centers on the current time zone";
    "use night and day"; "night globe with cities lights or day globe like the IE 3 one"; "the symbol icon
    doesn't really move, its more or less the globe rotating in its place"; "the globe when flying by didn't
    completely obscure the icon, it should"; "half icon time half globe moving time"; "the icon needs to
    show both at the same time, its half a globe rotating over the icon, and the leading and trailing part
    are faded"."""
    js = _js("throbber.js")
    assert "const FETCHING = new Set(['waiting', 'downloading']);" in js  # its fetch queued or under way
    assert "const TURN_MS = 2300;" in js and "const EDGE_FADE = 35 * Math.PI / 180;" in js
    assert "LOGO_SCALE" not in js and "COVER_MS" not in js  # neither a small logo in front nor a globe swapped in whole
    assert "const OCEAN = [16, 52, 150], LAND = [226, 176, 62]" in js  # blue and gold by day
    sphere = _inner(js, "drawSphere")
    # the logo always, flat and whole, under the half-globe
    assert sphere.index("g.drawImage(entry.logo.canvas, 0, 0);") < sphere.index("g.drawImage(entry.globe, 0, 0);")
    # each pixel's place on the half-globe; the other half leaves the logo; the leading and trailing edges fade
    assert "let p = (v + c.lon) % (2 * Math.PI);" in sphere
    assert "const k = Math.min(1, (Math.PI / 2 - Math.abs(p)) / EDGE_FADE);" in sphere and "if (k <= 0) continue;" in sphere
    assert "const cover = k * k * (3 - 2 * k);" in sphere and "px[o + 3] = Math.round(255 * c.edge * cover);" in sphere
    assert "const lon = home + (p * 180) / Math.PI;" in sphere  # its middle the picked zone's meridian
    assert "const earth = litEarth(sunNow());" in sphere and "litAt(earth, lon, c.lat, at);" in sphere  # the Sun now
    assert "CITY[ch] * at[4]" in sphere  # its cities lit at night
    assert "Math.min(zoneOffset(zone, new Date(Date.UTC(y, 0, 1))), zoneOffset(zone, new Date(Date.UTC(y, 6, 1))))" in js
    draw = _inner(js, "draw")
    assert "frame = requestAnimationFrame(draw);" in draw  # every screen frame: as smooth as the screen draws
    assert "const cycle = Math.floor(run / TURN_MS), t = (run % TURN_MS) / TURN_MS;" in draw  # the angle from the time
    # one turn from behind the logo round over it and behind again: it starts and stops on the logo alone
    assert "const v = Math.PI * (1 - 2 * t);" in draw
    assert "else if (cycle > held) { turning = false; show(false); return; }" in draw
    # its check every 250 ms can come before app.js has run: no ReferenceError while the page loads
    assert _inner(js, "busy").split("{", 1)[1].lstrip().startswith("if (typeof state === 'undefined') return false;")
    assert "if (front.dataset.html !== html) { front.innerHTML = html; front.dataset.html = html; entry.logo = null; }" in js
    assert "window.Throbber = { face, pulse, busy, preview: setPreview };" in js
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert html.index('<script src="throbber.js"></script>') < html.index('<script src="app.js"></script>')
    app = _js("app.js")
    assert "window.Throbber.face($('symbolLogo'), symbolIcon(info, 20), 20);" in _function(app, "renderSymbolButton")
    assert app.count("window.Throbber?.pulse();") == 2  # each stream message and poll that brought bars
    assert "window.Throbber.face($('railStock'), stockIcon, RAIL_ICON + 4);" in _js("panels.js")
    assert "sun: (t = new Date()) => ({ lon: noon(t), lat: declination(t) })" in _js("globeicon.js")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".throb.updating .throb-sphere { display: block; }" in css and ".throb.updating .throb-front { visibility: hidden; }" in css
    assert ".throb.ringed { border-radius: 50%; box-shadow: 0 0 0 2px var(--accent); }" in css  # reduced motion


def test_the_globe_fades_from_day_into_a_lighter_night_with_the_sun_centred_on_its_time_zone():
    """The user (2026-10-30): "there needs to be a fade from night to day on the globe itself, not just day
    and then night"; "so work on the frames so the transition between night and day is almost seamless";
    "instead of hard darkness it would be less shade, and the sun needs to be centered on the current zone
    it's in, so there will need to be about 24 different globe renders for each time the sun moves"."""
    import re

    js = _js("throbber.js")
    # the fade: over the Sun's height from 20 degrees up to 18 down (twilight's end), wide, not a line
    up, down = (int(d) for d in re.search(r"const TWILIGHT = \[Math\.sin\((-?\d+) \* RAD\), Math\.sin\((-?\d+) \* RAD\)\];", js).groups()[::-1])
    assert (up, down) == (20, -18) and up - down >= 30
    # eased at both ends: no step in its slope where the fade starts or stops (smootherstep)
    assert "return t * t * t * (t * (t * 6 - 15) + 10);" in js
    ease = lambda t: t * t * t * (t * (t * 6 - 15) + 10)  # noqa: E731 - the same curve, checked here
    assert ease(0) == 0 and ease(1) == 1 and all(ease((i + 1) / 100) - ease(i / 100) < 0.02 for i in range(100))
    # the day side dims as the Sun gets low, so the fade starts well before it sets
    assert "const SUN_HIGH = Math.sin(55 * RAD), LOW_SUN = 0.5;" in js
    # night a shade of the day's colours, not darkness: a third to almost half as bright, a little bluer
    night = [float(x) for x in re.search(r"const NIGHT = \[([\d., ]+)\];", js).group(1).split(",")]
    assert 0.25 <= min(night) and max(night) <= 0.5 and night[2] == max(night)
    assert "NIGHT_OCEAN" not in js and "NIGHT_LAND" not in js
    earth = _inner(js, "litEarth")
    assert "grid[k * 5 + ch] = colour * (sunlit * day + NIGHT[ch] * (1 - day));" in earth
    assert "grid[k * 5 + 4] = (1 - day) ** CITY_DUSK * lights[k];" in earth  # the cities come on as it gets dark
    # the Sun centred on the time zone it is over: 24 places, each lit Earth drawn once and kept
    assert "const ZONE_DEG = 15;" in js and 360 // 15 == 24
    sun = _inner(js, "sunNow")
    assert "const lon = ZONE_DEG * Math.round(s.lon / ZONE_DEG);" in sun
    assert "return { lon: ((lon % 360) + 540) % 360 - 180, lat: Math.round(s.lat) };" in sun
    zone = lambda lon: ((15 * round(lon / 15)) % 360 + 540) % 360 - 180  # noqa: E731 - Python's round: halves to even
    assert {zone(x / 4) for x in range(-720, 720)} == set(range(-180, 180, 15))  # one of 24, 180 the same as -180
    assert "if (lit.has(key)) return lit.get(key);" in earth and "if (lit.size >= 24) lit.delete(lit.keys().next().value);" in earth
    assert "lit.clear();  // lit again with the land" in _inner(js, "loadLand")
    # the shine is the Sun's, round the place it is overhead: it turns with the Earth, out of sight while
    # the Sun is on the far side ("there is a chance the shiny bit is never seen"); no gloss fixed in front
    assert "const GLINT = 28;" in js and "grid[k * 5 + 3] = h > 0 ? h ** GLINT * (0.75 - 0.5 * l) : 0;" in earth
    assert "255 * at[3]" in _inner(js, "drawSphere") and "spec" not in js and "HALF" not in js


def test_the_page_names_a_feed_on_a_plugins_keys_by_that_plugin():
    """The user (2026-10-30): "the source isn't blue ocean its alpaca w/ blue ocean"; "house all alpaca in
    alpaca": every list of feeds says alpaca (blue ocean), never boats or Blue Ocean as a source."""
    app = _js("app.js")
    assert "function feedLabel(source) { return FEED_LABELS[source] || source; }" in app
    assert "const labels = e.detail?.feedLabels;" in app  # from each poll of /api/activity
    feeds = _function(app, "renderFeeds")
    assert "`All feeds (${feeds.map((f) => feedLabel(f.source)).join(' > ')})`" in feeds and "`Only ${feedLabel(f.source)}`" in feeds
    picker = _function(app, "feedPicker")
    assert "feeds.map((f) => opt(f, feedLabel(f)))" in picker and "`All merged (${feeds.map(feedLabel).join(' > ')})`" in picker
    assert "[esc(feedLabel(src))," in _function(app, "attributionHtml")
    assert "`feed: ${m.feeds.map(feedLabel).join(' > ')}" in app
    act = _js("activity.js")
    assert "const label = (source) => (typeof feedLabel === 'function' ? feedLabel(source) : source);" in act
    for spot in ("<b>${esc(label(l.source))}</b>", "${esc(label(f.name))}${f.closed", "<b>${esc(label(src))}</b>",
                 "<b>${esc(label(r.source))}</b>", "(${esc(label(live.source))})"):
        assert spot in act, spot
    assert "(d.feeds || []).map((f) => (typeof feedLabel === 'function' ? feedLabel(f) : f)).join(' > ')" in _js("venues.js")
    assert "<td><b>${esc(f.label)}</b>" in _js("plugins.js")

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
    assert "groupId: 'user', paneId," in restore
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

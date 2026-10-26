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

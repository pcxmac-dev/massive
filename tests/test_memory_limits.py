"""Memory limits: the chart app's caches share one budget (CHART_CACHE_MB), at most HEAVY_REQUESTS
requests build chart data at once, the live recorder's backlog is a byte budget, numpy loads with
one thread, and `admin memory` reports each process's own memory apart from Windows' file cache."""
from __future__ import annotations

import os
import sys
import threading

import pytest

from massive_collate import live, memory
from massive_collate.config import load_settings
from massive_collate.gui import server
from massive_collate.gui.bars import CACHE_SHARES, CHART_CACHE_BYTES, BarEngine
from massive_collate.gui.window import WindowReader
from massive_collate.storage import Store

MB = 2**20


@pytest.fixture
def store():
    with Store(":memory:", check_same_thread=False) as s:
        yield s


def test_every_chart_cache_keeps_to_its_share_of_one_budget(store):
    eng = BarEngine(store, cache_bytes=100 * MB)
    reader = WindowReader(eng)
    budgets = {"series": eng._series._budget, "bars": eng._bars._budget, "bricks": reader._bricks._budget,
               "rollup": reader._rollup._budget}
    assert budgets == {k: int(100 * MB * share) for k, share in CACHE_SHARES.items()}
    assert sum(CACHE_SHARES.values()) == pytest.approx(1.0)
    assert BarEngine(store).cache_bytes == CHART_CACHE_BYTES == 1024 * MB  # a gigabyte, all of them together


def test_the_budget_is_a_setting_the_chart_app_takes(store):
    assert load_settings(env={}, env_file=None).chart_cache_mb == 1024
    assert load_settings(env={"CHART_CACHE_MB": "16"}, env_file=None).chart_cache_mb == 64  # never starved
    app = server.ChartApp(store, settings=load_settings(env={"CHART_CACHE_MB": "256"}, env_file=None))
    assert app.engine.cache_bytes == 256 * MB and app.window._rollup._budget == int(256 * MB * CACHE_SHARES["rollup"])


def test_requests_past_the_in_flight_cap_wait_then_are_answered_busy(store, monkeypatch):
    monkeypatch.setattr(server, "HEAVY_WAIT_S", 0.05)
    app = server.ChartApp(store)
    held = [app._heavy.acquire(blocking=False) for _ in range(server.HEAVY_REQUESTS)]
    assert all(held) and not app._heavy.acquire(blocking=False)  # HEAVY_REQUESTS at once, no more
    for call in (lambda: app.bars({"source": "massive", "ticker": "SPCX"}),
                 lambda: app.profile({"source": "massive", "ticker": "SPCX"}),
                 lambda: app.future({"source": "massive", "ticker": "SPCX"}),
                 lambda: app.range_stats({"source": "massive", "ticker": "SPCX", "from": "0", "to": "1"})):
        with pytest.raises(server.Busy):
            call()
    for _ in held:
        app._heavy.release()
    assert app.bars({"source": "massive", "ticker": "SPCX"})["bars"] == []  # a turn free: it runs (nothing stored)
    assert app._heavy.acquire(blocking=False)  # and gave its turn back, failing or not
    app._heavy.release()


def test_a_waiting_request_runs_when_a_turn_frees(store, monkeypatch):
    app = server.ChartApp(store)
    for _ in range(server.HEAVY_REQUESTS):
        app._heavy.acquire()
    done = threading.Event()

    def ask():
        with app.heavy_work():
            done.set()

    t = threading.Thread(target=ask)
    t.start()
    assert not done.wait(0.1)  # waiting its turn
    app._heavy.release()
    assert done.wait(5)
    t.join()
    app._heavy.release()


def test_busy_is_answered_503_and_the_live_tail_takes_no_turn():
    src = server.__file__
    text = open(src, encoding="utf-8").read()
    assert "except Busy as e:\n                return self._json({\"error\": str(e)}, HTTPStatus.SERVICE_UNAVAILABLE)" in text
    stream = text.split("    def stream(", 1)[1].split("\n    def ", 1)[0]
    assert "self._bars(" in stream and "self.bars(" not in stream  # a few bars, never waiting behind charts


def test_the_recorders_backlog_is_a_byte_budget():
    assert live.MAX_BUFFERED_BYTES == 64 * MB
    assert 100_000 < live.MAX_BUFFERED_BYTES // live.TRADE_BYTES < 120_000  # was 500,000 trades: about 300 MB
    t = live._trade(1_792_825_200_000_000_000, 1, 81000.5, 0.1, 7)
    weight = sys.getsizeof(t) + sum(sys.getsizeof(v) for v in t.values())  # its keys are shared
    assert weight <= live.TRADE_BYTES


def test_numpy_loads_with_one_thread_unless_told_otherwise(monkeypatch):
    import importlib

    import massive_collate

    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(massive_collate)  # what the package sets as it loads, before anything imports numpy
    assert os.environ["OPENBLAS_NUM_THREADS"] == os.environ["OMP_NUM_THREADS"] == "1"
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "4")
    importlib.reload(massive_collate)
    assert os.environ["OPENBLAS_NUM_THREADS"] == "4"  # the environment's own value is kept


# -- admin memory ---------------------------------------------------------------------------------
def _fake_machine(monkeypatch, reads: dict[int, list[int]]):
    """The app's processes, their counters and the machine's, as the system's calls would give them."""
    from massive_collate import proc

    cmd = '"C:\\venv\\Scripts\\python.exe" -m massive_collate '
    monkeypatch.setattr(proc, "processes", lambda: [
        (100, 1, cmd + "gui --no-browser --port 8765"), (101, 100, cmd + "gui --no-browser --port 8765"),  # launcher, child
        (200, 1, cmd + "--source massive sync"), (300, 1, cmd + "live"), (400, 1, "python other.py")])
    def counters(pid):  # each look reads the next of the process's totals (the last again after)
        if pid not in reads:
            return None
        read = reads[pid].pop(0) if len(reads[pid]) > 1 else reads[pid][0]
        return {"private": pid * MB, "working_set": pid // 2 * MB, "read": read}

    monkeypatch.setattr(memory, "_process_counters", counters)
    monkeypatch.setattr(memory, "_system", lambda: {"total": 64_000 * MB, "available": 40_000 * MB,
                                                    "commit": 30_000 * MB, "commit_limit": 70_000 * MB,
                                                    "cache": 33_000 * MB})
    monkeypatch.setattr(memory, "_active_cache", lambda: {"active": 31_000 * MB, "peak": 39_000 * MB})
    monkeypatch.setattr(memory.time, "sleep", lambda s: None)


def test_the_memory_report_keeps_the_processes_apart_from_the_file_cache(monkeypatch):
    _fake_machine(monkeypatch, {101: [0, 380 * MB], 200: [0, 0], 300: [10 * MB, 20 * MB]})
    m = memory.measure(5)
    assert [(p["pid"], p["role"]) for p in m["processes"]] == [(101, "gui"), (200, "sync"), (300, "live")]
    gui = m["processes"][0]
    assert gui["private"] == 101 * MB and gui["read_per_s"] == 76 * MB  # read over the five seconds
    assert m["cache"] == {"active": 31_000 * MB, "peak": 39_000 * MB, "standby": 2_000 * MB}
    text = memory.report(m)
    assert "gui" in text and "reading 76.0 MB/s now" in text
    assert "active  31,000 MB  held in RAM" in text and "standby  2,000 MB" in text
    assert text.index("their own memory") < text.index("Windows' file cache")


def test_a_process_that_ends_while_measured_is_left_out_and_elsewhere_nothing_is_guessed(monkeypatch):
    _fake_machine(monkeypatch, {101: [0, 0]})  # sync and live: not ours to look at, or gone
    assert [p["pid"] for p in memory.measure(0)["processes"]] == [101]
    assert memory.measure(0)["processes"][0]["read_per_s"] is None  # not watched
    monkeypatch.setattr(memory, "_system", lambda: None)
    assert "not measured here" in memory.report(memory.measure(0))


def test_the_system_calls_answer_on_windows():
    if os.name != "nt":
        pytest.skip("Windows' counters")
    me = memory._process_counters(os.getpid())
    assert me["private"] > 0 and me["working_set"] > 0 and me["read"] is not None
    system, cache = memory._system(), memory._active_cache()
    assert 0 < system["available"] <= system["total"] and system["cache"] >= 0
    assert 0 <= cache["active"] <= cache["peak"]

"""The Resources view (resources.py): the app's processes, the machine, its bandwidth and
connections, the disks and the data files, sampled at the cadence of whoever looks and within the
monitor's own CPU budget, with its history capped."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from massive_collate import resources as res
from massive_collate.resources import Conn, Machine, Monitor, Net, Proc

SYNC = '"C:\\venv\\Scripts\\pythonw.exe" -m massive_collate sync'
LIVE = "pythonw.exe -m massive_collate live"
GUI = "python.exe -m massive_collate gui --no-browser --port 8765"


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeProbe:
    def __init__(self) -> None:
        self.procs: list[Proc] = []
        self.machine_: Machine | None = None
        self.net: Net | None = None
        self.conns: list[Conn] = []

    def processes(self):
        return list(self.procs)

    def machine(self):
        return self.machine_

    def network(self):
        return self.net

    def connections(self):
        return list(self.conns)


def proc(pid, parent, command, cpu_s=0.0, memory=100, private=50, read=0, write=0, started=1.0e9):
    return Proc(pid, parent, command, started, cpu_s, memory, private, read, write, 2, 10)


def monitor(path, probe):
    clock, wall, cpu = Clock(), Clock(1.79e9), Clock(0.0)
    return Monitor(path, probe=probe, clock=clock, wall=wall, cpu_clock=cpu), clock, wall, cpu


def later(clock, wall, seconds=2.0):
    clock.t += seconds
    wall.t += seconds


def test_each_program_is_named_from_its_command_line():
    assert res.role_of(GUI)[:2] == ("gui", "Chart app · port 8765")
    assert res.role_of(SYNC)[:2] == ("sync", "Fetcher (sync)")
    assert res.role_of(SYNC + " --tickers SPCX")[1] == "Fetcher: run for opened symbols"
    assert res.role_of(LIVE)[:2] == ("live", "Live recorder")
    assert res.role_of("python -m massive_collate audit --daily")[:2] == ("audit", "audit command")
    assert res.is_app(SYNC) and not res.is_app("python -m pytest")
    assert res.is_local("127.0.0.1:8765") and res.is_local("[::1]:8765") and not res.is_local("34.1.2.3:443")


def test_cpu_and_io_are_rates_between_samples_and_the_launcher_counts_with_its_interpreter(tmp_path):
    p = FakeProbe()
    m, clock, wall, _ = monitor(tmp_path, p)
    # Windows' venv launcher (pid 10) started the interpreter (pid 11) with the same command line
    p.procs = [proc(10, 1, SYNC, memory=5), proc(11, 10, SYNC, cpu_s=100.0, memory=200, read=1000)]
    m.sample()
    g = m._groups[0]
    assert g["pids"] == [10, 11] and g["memory"] == 205 and g["cpu"] is None  # one sample: no rate yet
    later(clock, wall)
    p.procs = [proc(10, 1, SYNC, memory=5), proc(11, 10, SYNC, cpu_s=101.5, memory=210, read=5000, write=600)]
    m.sample()
    [g] = m._groups
    assert (g["role"], g["key"], g["label"]) == ("sync", "sync:10", "Fetcher (sync)")
    assert g["cpu"] == pytest.approx(0.75)  # 1.5 s of CPU in 2 s: three quarters of one core
    assert g["readRate"] == pytest.approx(2000) and g["writeRate"] == pytest.approx(300)


def test_a_restarted_program_starts_its_rates_afresh(tmp_path):
    p = FakeProbe()
    m, clock, wall, _ = monitor(tmp_path, p)
    p.procs = [proc(11, 1, LIVE, cpu_s=500.0)]
    m.sample()
    later(clock, wall)
    p.procs = [proc(11, 1, LIVE, cpu_s=0.1, started=2.0e9)]  # the same pid, a new process
    m.sample()
    assert m._groups[0]["cpu"] is None  # not 0.1 - 500 s of CPU
    assert list(m._prev) == [(11, 2.0e9)]  # the old one forgotten


def test_the_machine_cpu_memory_and_bandwidth(tmp_path):
    p = FakeProbe()
    m, clock, wall, _ = monitor(tmp_path, p)
    gib = 2**30
    p.machine_ = Machine(8, 100.0, 1000.0, 16 * gib, 4 * gib, 20 * gib, 10 * gib)
    p.net = Net(1_000_000, 0, 10**9, ("Wi-Fi",))
    m.sample()
    later(clock, wall)
    p.machine_ = Machine(8, 104.0, 1016.0, 16 * gib, 4 * gib, 20 * gib, 10 * gib)  # 4 of 16 core-seconds busy
    p.net = Net(1_400_000, 100_000, 10**9, ("Wi-Fi",))
    m.sample()
    v = m.view()
    assert v["machine"]["cpu"] == pytest.approx(0.25) and v["machine"]["memoryUsed"] == 12 * gib
    assert v["network"]["in"] == pytest.approx(200_000) and v["network"]["out"] == pytest.approx(50_000)
    assert v["network"]["adapters"] == ["Wi-Fi"] and v["series"]["in"][-1] == pytest.approx(200_000)
    later(clock, wall)
    p.net = Net(10, 10, 10**9, ())  # the adapter turned off: its totals gone, no negative rate
    m.sample()
    assert m._net["in"] == 0 and m._net["out"] == 0


def test_connections_are_counted_per_program_with_their_busiest_ends(tmp_path):
    p = FakeProbe()
    m, *_ = monitor(tmp_path, p)
    p.procs = [proc(20, 1, GUI), proc(30, 1, LIVE)]
    p.conns = [Conn(20, "listening", "127.0.0.1:8765", ""),
               Conn(20, "established", "127.0.0.1:8765", "127.0.0.1:50000"),
               Conn(30, "established", "10.0.0.2:5000", "34.1.2.3:443"),
               Conn(30, "established", "10.0.0.2:5001", "34.1.2.3:443"),
               Conn(30, "time-wait", "10.0.0.2:5002", "52.9.9.9:443"),
               Conn(99, "established", "10.0.0.2:6000", "8.8.8.8:443")]  # another program's
    m.sample()
    gui, live = m._groups
    assert gui["connections"] == {"established": 1, "listening": 1, "other": 0}
    assert gui["remotes"] == [{"remote": "127.0.0.1:50000", "state": "established", "count": 1, "local": True}]
    assert live["connections"] == {"established": 2, "listening": 0, "other": 1}
    assert live["remotes"][0] == {"remote": "34.1.2.3:443", "state": "established", "count": 2, "local": False}
    assert live["remoteCount"] == 2
    assert m._links == {"established": 4, "listening": 1, "other": 1, "app": 3}


def test_the_cadence_follows_the_view_and_the_cpu_budget(tmp_path):
    p = FakeProbe()
    m, clock, wall, cpu = monitor(tmp_path, p)
    assert m.interval() == res.BACKGROUND_S  # nobody looking
    m.view()
    assert m.viewing() and m.interval() == res.VIEWING_S
    clock.t += res.VIEW_TTL_S + 1  # the view closed: no request since
    assert not m.viewing() and m.interval() == res.BACKGROUND_S

    class Slow(FakeProbe):
        def processes(self):
            cpu.t += 0.5  # half a second of CPU a sample
            return []

    slow = Monitor(tmp_path, probe=Slow(), clock=Clock(), wall=Clock(1.79e9), cpu_clock=cpu)
    slow.view()
    assert slow.interval() == pytest.approx(0.5 / res.CPU_BUDGET)  # 10 s, not 2: within its budget
    assert res.CPU_BUDGET < res.CPU_LIMIT <= 0.10  # the user's limit: a tenth of one core


def test_history_is_spaced_and_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(res, "RECENT", 5)
    p = FakeProbe()
    p.procs = [proc(11, 1, SYNC)]
    m, clock, wall, _ = monitor(tmp_path, p)
    for _ in range(40):  # 80 s at 2 s
        later(clock, wall)
        m.sample()
    assert len(m.recent) == 5  # every sample, the newest kept
    assert len(m.history) == 3  # one per BACKGROUND_S
    monkeypatch.setattr(res, "MEMORY_CAP", 1)
    later(clock, wall, res.BACKGROUND_S)
    m.sample()
    assert not m.history and m.recent  # over its memory cap: the day's history goes first


def test_files_are_listed_by_folder_with_their_growth(tmp_path):
    (tmp_path / "feeds").mkdir()
    (tmp_path / "feeds" / "massive.db").write_bytes(b"x" * 1000)
    (tmp_path / "massive.db").write_bytes(b"y" * 100)
    p = FakeProbe()
    m, clock, wall, _ = monitor(tmp_path, p)
    m.sample()
    f = m._files
    assert (f["total"], f["count"], f["root"]) == (1100, 2, str(tmp_path))
    feeds = next(g for g in f["groups"] if g["name"] == "feeds")
    assert feeds["size"] == 1000 and feeds["growth"] is None  # listed once: no growth yet
    assert any("data" in d["holds"] and d["total"] >= d["free"] for d in f["disks"])
    (tmp_path / "feeds" / "massive.db").write_bytes(b"x" * 1600)
    later(clock, wall, res.FILES_S)
    m.sample()
    feeds = next(g for g in m._files["groups"] if g["name"] == "feeds")
    assert feeds["growth"] == pytest.approx(36_000)  # 600 bytes in a minute: 36 KB an hour
    assert feeds["files"][0]["path"] == "feeds/massive.db" and feeds["files"][0]["growth"] == pytest.approx(36_000)


def test_the_view_has_each_programs_series_for_the_range_asked(tmp_path):
    p = FakeProbe()
    m, clock, wall, _ = monitor(tmp_path, p)
    for i in range(4):
        later(clock, wall)
        p.procs = [proc(11, 1, SYNC, cpu_s=float(i))]
        m.sample()
    v = m.view()
    assert v["span"] == "recent" and len(v["series"]["t"]) == 4
    assert v["series"]["processes"]["sync:11"]["cpu"] == [None, 0.5, 0.5, 0.5]
    assert v["monitor"]["limit"] == res.CPU_LIMIT and v["monitor"]["samples"] >= 4
    assert v["cadence"] == {"viewing": res.VIEWING_S, "background": res.BACKGROUND_S, "files": res.FILES_VIEWING_S}
    json.dumps(v)  # what the page is sent
    assert m.view("day")["span"] == "day"


def test_the_chart_app_answers_without_starting_the_sampler(store):
    from massive_collate.gui.server import ChartApp

    app = ChartApp(store)
    assert app.monitor.data_dir == Path(store.path).parent  # the data folder the store is in
    app._monitor = Monitor(Path(store.path).parent, probe=FakeProbe())
    v = app.resources()
    assert v["processes"] == [] and v["files"]["count"] >= 1
    assert app.monitor._thread is None  # sampled in the background only once serve() starts it


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own counters")
def test_windows_counters_read_this_process_the_machine_and_its_connections():
    p = res.WindowsProbe()
    me = [x for x in p.processes() if x.pid == os.getpid()]
    assert me and me[0].memory > 0 and me[0].cpu_s >= 0 and me[0].started > 0
    m = p.machine()
    assert m and m.memory_total > m.memory_available > 0 and m.total_s >= m.busy_s >= 0
    assert all(isinstance(c, Conn) for c in p.connections())
    n = p.network()
    assert n is None or (n.received >= 0 and n.sent >= 0)

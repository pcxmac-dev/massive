from __future__ import annotations

import pytest
import requests

from massive_collate import cli
from massive_collate.storage import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("MASSIVE_DB", str(tmp_path / "data" / "m.db"))
    monkeypatch.setenv("MASSIVE_API_KEY", "FAKE")
    for name in ("MASSIVE_TIER", "MASSIVE_RPM", "MASSIVE_MAX_REQUESTS_PER_RUN", "MASSIVE_MAX_REQUESTS_PER_DAY"):
        monkeypatch.delenv(name, raising=False)

    def no_network(*a, **kw):
        raise AssertionError("no request should be sent")

    monkeypatch.setattr(requests.Session, "get", no_network)
    return tmp_path


def test_watch_and_status_are_offline(env, capsys):
    assert cli.main(["watch", "add", "spcx"]) == 0
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "SPCX" in out and "plan: free" in out


def test_watch_accepts_exchange_and_shows_listing(env, capsys):
    from datetime import date

    cli.main(["watch", "add", "SPCX::NASDAQ", "--resolution", "minute"])
    with Store(env / "data" / "m.db") as s:
        s.set_listing("massive", "SPCX", date(2026, 6, 12), "FIGI", date(2026, 10, 22))
    capsys.readouterr()
    cli.main(["watch", "list"])
    out = capsys.readouterr().out
    assert "SPCX::XNAS" in out and "listed: 2026-06-12" in out


def test_daily_cap_reached_sends_nothing(env, monkeypatch, capsys):
    monkeypatch.setenv("MASSIVE_MAX_REQUESTS_PER_DAY", "0")
    cli.main(["watch", "add", "SPCX"])
    assert cli.main(["sync"]) == 0
    assert "daily request cap" in capsys.readouterr().out


def test_overlapping_runs_are_refused(env):
    db = env / "data" / "m.db"
    with cli.run_lock(db):
        with pytest.raises(SystemExit):
            with cli.run_lock(db):
                pass
    with cli.run_lock(db):  # released afterwards
        pass


def test_lock_left_by_a_dead_process_is_taken_over(env):
    import os

    db = env / "data" / "m.db"
    lock = db.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("999999")  # no such process
    with cli.run_lock(db):
        assert lock.read_text() == str(os.getpid())
    assert not lock.exists()


def test_lock_of_a_live_process_is_respected(env):
    import os

    db = env / "data" / "m.db"
    lock = db.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))  # this test process is alive
    with pytest.raises(SystemExit):
        with cli.run_lock(db):
            pass
    lock.unlink()


def test_pid_alive():
    import os

    assert cli.pid_alive(os.getpid()) and not cli.pid_alive(999999) and not cli.pid_alive(0)


def test_missing_key_is_a_config_error(env, monkeypatch, capsys):
    monkeypatch.delenv("MASSIVE_API_KEY")
    cli.main(["watch", "add", "SPCX"])
    assert cli.main(["sync"]) == 2
    assert "MASSIVE_API_KEY" in capsys.readouterr().err


def test_export_writes_csv(env):
    with Store(env / "data" / "m.db") as s:
        s.upsert_trades("massive", "SPCX", [{"sip_timestamp": 1, "sequence_number": 1, "price": 1.0, "size": 1}])
    out = env / "out.csv"
    assert cli.main(["export", "SPCX", "--out", str(out)]) == 0
    assert out.read_text().count("\n") == 2

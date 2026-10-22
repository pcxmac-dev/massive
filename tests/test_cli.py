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

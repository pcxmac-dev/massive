from datetime import date

import pandas as pd
import pytest

from massive_collate.futures import continuous, roll_schedule, session_dates
from massive_collate.sources.massive import normalize_future_bar

ET = "America/New_York"


def test_session_dates_follow_the_cme_evening_open():
    idx = pd.DatetimeIndex(["2026-10-20 17:59", "2026-10-20 18:00", "2026-10-21 16:59"], tz=ET)
    assert list(session_dates(idx)) == [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 21)]


D = [date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21)]
EXP = {"NQU6": date(2026, 9, 18), "NQZ6": date(2026, 12, 18), "NQH7": date(2027, 3, 19)}


def test_roll_follows_volume_and_never_goes_back():
    vol = {
        "NQU6": {D[0]: 900, D[1]: 500, D[2]: 300, D[3]: 600, D[4]: 100},
        "NQZ6": {D[0]: 100, D[1]: 600, D[2]: 800, D[3]: 500, D[4]: 900, D[5]: 1000},
        "NQH7": {D[0]: 1, D[1]: 1, D[2]: 1, D[3]: 1, D[4]: 1, D[5]: 2},
    }
    s = roll_schedule(vol, EXP)
    # rolls on the 15th (Z6 out-trades U6); stays on Z6 on the 17th even though U6 traded more
    assert s == [(D[0], "NQU6"), (D[1], "NQZ6"), (D[2], "NQZ6"), (D[3], "NQZ6"), (D[4], "NQZ6"), (D[5], "NQZ6")]


def test_roll_moves_on_at_expiry_and_rank_two():
    vol = {"NQU6": {D[4]: 900}, "NQZ6": {D[4]: 100, D[5]: 50}, "NQH7": {D[4]: 1, D[5]: 60}}
    assert roll_schedule(vol, EXP) == [(D[4], "NQU6"), (D[5], "NQH7")]
    assert roll_schedule(vol, EXP, rank=2) == [(D[4], "NQZ6")]


def test_candidates_and_contract_months():
    from massive_collate.futures import candidate_contracts, contract_month

    c = candidate_contracts("cl", date(2026, 11, 3), date(2027, 1, 1))
    assert c == [(date(2026, 11, 1), ["CLX6", "CLX26"]), (date(2026, 12, 1), ["CLZ6", "CLZ26"]),
                 (date(2027, 1, 1), ["CLF7", "CLF27"])]
    assert contract_month("CLZ6", "CL", date(2026, 10, 22)) == date(2026, 12, 1)
    assert contract_month("CLF0", "CL", date(2029, 11, 1)) == date(2030, 1, 1)  # decade rolls over
    assert contract_month("NGX26", "NG", date(2026, 10, 22)) == date(2026, 11, 1)  # two-digit year
    assert contract_month("NGF39", "NG", date(2026, 10, 22)) == date(2039, 1, 1)
    assert contract_month("CL:BF", "CL", date(2026, 1, 1)) is None


def test_roll_moves_on_when_held_contract_stops_trading():
    # provisional expiry far away, but U6 has no session on the 21st: re-select
    vol = {"NQU6": {D[4]: 900}, "NQZ6": {D[4]: 100, D[5]: 500}}
    far = {"NQU6": date(2026, 11, 1), "NQZ6": date(2027, 2, 1)}
    assert roll_schedule(vol, far) == [(D[4], "NQU6"), (D[5], "NQZ6")]


def _frame(times, closes):
    idx = pd.DatetimeIndex(times, tz=ET)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=idx)


def test_continuous_joins_and_reports_rolls():
    u = _frame(["2026-09-14 10:00", "2026-09-15 10:00"], [100.0, 101.0])
    z = _frame(["2026-09-14 10:00", "2026-09-15 10:00", "2026-09-16 10:00"], [110.0, 111.0, 112.0])
    sched = [(D[0], "NQU6"), (D[1], "NQZ6"), (D[2], "NQZ6")]
    joined, rolls = continuous({"NQU6": u, "NQZ6": z}, sched)
    assert list(joined["close"]) == [100.0, 111.0, 112.0]
    assert list(joined["contract"]) == ["NQU6", "NQZ6", "NQZ6"]
    assert len(rolls) == 1 and rolls[0].old == "NQU6" and rolls[0].new == "NQZ6"
    assert rolls[0].gap == pytest.approx(10.0)  # 110 - 100 at the last old bar

    diff, _ = continuous({"NQU6": u, "NQZ6": z}, sched, adjust="difference")
    assert list(diff["close"]) == [110.0, 111.0, 112.0]
    ratio, _ = continuous({"NQU6": u, "NQZ6": z}, sched, adjust="ratio")
    assert ratio["close"].iloc[0] == pytest.approx(110.0)


def test_roll_gap_from_session_closes_when_bars_do_not_overlap():
    # intraday bars only for the sessions each contract was held: no overlap at the switch
    u = _frame(["2026-09-14 10:00"], [100.0])
    z = _frame(["2026-09-15 10:00", "2026-09-16 10:00"], [111.0, 112.0])
    daily = lambda closes: _frame(["2026-09-14 00:00", "2026-09-15 00:00"], closes)  # noqa: E731
    sessions = {"NQU6": daily([100.5, 101.0]), "NQZ6": daily([110.25, 111.0])}
    sched = [(D[0], "NQU6"), (D[1], "NQZ6"), (D[2], "NQZ6")]
    joined, rolls = continuous({"NQU6": u, "NQZ6": z}, sched, sessions=sessions)
    assert rolls[0].gap == pytest.approx(110.25 - 100.5)  # settlement-to-settlement on the 14th
    adj, _ = continuous({"NQU6": u, "NQZ6": z}, sched, adjust="difference", sessions=sessions)
    assert adj["close"].iloc[0] == pytest.approx(100.0 + 9.75)
    _, no_ref = continuous({"NQU6": u, "NQZ6": z}, sched)
    assert no_ref[0].gap is None  # nothing to measure: reported as unknown, not guessed


def test_normalize_future_bar():
    raw = {"window_start": 1792628700000000000, "session_end_date": "2026-10-22", "open": 1, "high": 2, "low": 0.5,
           "close": 1.5, "volume": 10, "transactions": 4, "dollar_volume": 15.0}
    m = normalize_future_bar(raw, "minute")
    assert m == {"t": 1792628700000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "vw": 1.5, "n": 4}
    d = normalize_future_bar(raw, "day")
    assert pd.Timestamp(d["t"], unit="ms", tz="UTC").tz_convert(ET) == pd.Timestamp("2026-10-22", tz=ET)

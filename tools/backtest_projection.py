"""Backtest: cut real history at T, project the time axis forward, compare with the bars that
actually printed. 'missed' = real bars the projection had no slot for (must be 0);
'empty' = projected slots with no real bar (no trade in that interval: thin data or outages)."""
import numpy as np
import pandas as pd

from massive_collate.config import load_settings
from massive_collate.gui import projection as pj
from massive_collate.gui.server import ChartApp
from massive_collate.storage import Store

st = load_settings()
s = Store(st.db_path, check_same_thread=False)
eng = ChartApp(s, settings=st).engine
cal = eng.calendar()
ET = "America/New_York"
CUTS = ["2025-11-25 15:00", "2025-12-23 12:00", "2026-01-15 16:30", "2026-02-13 11:00", "2026-03-06 15:45",
        "2026-04-01 10:00", "2026-05-22 13:00", "2026-07-02 14:00", "2026-09-04 16:00", "2026-10-30 12:00"]
total_missed = 0
for tk, prod in [("AAPL", None), ("IXIC", None), ("NQ1!", "NQ"), ("CL1!", "CL"), ("USDEUR", None)]:
    ser = eng.series("*", tk)
    for period, span in [("minute", 1), ("minute", 5), ("minute", 30), ("hour", 1), ("hour", 4), ("day", 1), ("week", 1)]:
        for session in ("regular", "extended"):
            if ser.around_the_clock and session == "regular":
                continue
            session = ser.session_for(session)
            bars = eng._resampled(ser, period, span, session)
            missed = empty = compared = 0
            notes = []
            for cut in CUTS:
                T = pd.Timestamp(cut, tz=ET)
                past, future = bars[bars.index <= T], bars[bars.index > T]
                if past.empty or len(future) < 5:
                    continue
                horizon = future.index[min(len(future) - 1, 400)]
                actual = future.index[future.index <= horizon]
                ms = pj.project(cal, ser, prod, period, span, session, "candles", 1, len(past), past.index[-1],
                                0, 4 * len(actual) + 3000)
                proj = pd.to_datetime(ms, unit="ms", utc=True).tz_convert(ET)
                proj = proj[proj <= horizon]
                m = actual[~actual.isin(proj)]
                e = proj[~proj.isin(actual)]
                missed += len(m)
                empty += len(e)
                compared += len(actual)
                if len(m):
                    notes.append(f"{cut}: missed {[x.strftime('%m-%d %H:%M') for x in m[:4]]}")
            total_missed += missed
            print(f"{tk:6} {period:6}{span:<3} {session:8} real bars {compared:5}  missed {missed:3}  empty slots {empty:4}"
                  + ("  " + "; ".join(notes[:3]) if notes else ""))
print("TOTAL MISSED:", total_missed)

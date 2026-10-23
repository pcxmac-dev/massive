"""World stock exchanges for the Markets view: where they are, whether they are open right now
(lunch breaks included), when that changes, and their upcoming holidays.

Sessions and holidays come from `exchange_calendars` (each exchange's own calendar, lunar
holidays and special closures included). Shenzhen shares Shanghai's calendar and the NSE shares
the BSE's. Calendars are built once per process and cover a year ahead.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd


@dataclass(frozen=True)
class Exchange:
    code: str  # exchange_calendars name (ISO MIC)
    name: str
    city: str
    country: str
    region: str
    lat: float
    lon: float
    index: str  # flagship index
    calendar: str | None = None  # calendar to use when it differs from `code`
    proxy: str | None = None  # US-listed fund tracking the market (daily change for the colour)


E = Exchange
EXCHANGES: tuple[Exchange, ...] = (
    # Americas
    E("XNYS", "New York Stock Exchange / Nasdaq", "New York", "United States", "Americas", 40.71, -74.01, "S&P 500 · Nasdaq · Dow", proxy="SPY"),
    E("CMES", "CME Group (Globex)", "Chicago", "United States", "Americas", 41.88, -87.63, "E-mini S&P 500 futures", proxy="SPY"),
    E("XCBF", "Cboe Futures Exchange", "Chicago", "United States", "Americas", 41.78, -87.75, "VIX futures", proxy="VIXY"),
    E("IEPA", "ICE Futures U.S.", "Atlanta", "United States", "Americas", 33.75, -84.39, "Russell 2000 · DXY futures", proxy="IWM"),
    E("XTSE", "Toronto Stock Exchange", "Toronto", "Canada", "Americas", 43.65, -79.38, "S&P/TSX Composite", proxy="EWC"),
    E("XMEX", "Bolsa Mexicana de Valores", "Mexico City", "Mexico", "Americas", 19.43, -99.13, "S&P/BMV IPC", proxy="EWW"),
    E("BVMF", "B3", "São Paulo", "Brazil", "Americas", -23.55, -46.63, "Ibovespa", proxy="EWZ"),
    E("XBUE", "Bolsas y Mercados Argentinos", "Buenos Aires", "Argentina", "Americas", -34.60, -58.38, "S&P Merval", proxy="ARGT"),
    E("XSGO", "Santiago Stock Exchange", "Santiago", "Chile", "Americas", -33.45, -70.67, "S&P IPSA", proxy="ECH"),
    E("XLIM", "Bolsa de Valores de Lima", "Lima", "Peru", "Americas", -12.05, -77.04, "S&P/BVL Peru General", proxy="EPU"),
    E("XBOG", "Bolsa de Valores de Colombia", "Bogotá", "Colombia", "Americas", 4.71, -74.07, "MSCI COLCAP", proxy="GXG"),
    # Europe
    E("XLON", "London Stock Exchange", "London", "United Kingdom", "Europe", 51.51, -0.09, "FTSE 100", proxy="EWU"),
    E("XETR", "Xetra (Deutsche Börse)", "Frankfurt", "Germany", "Europe", 50.11, 8.68, "DAX", proxy="EWG"),
    E("XFRA", "Börse Frankfurt", "Frankfurt", "Germany", "Europe", 50.12, 8.66, "DAX (floor)", proxy="EWG"),
    E("XEUR", "Eurex", "Eschborn", "Germany", "Europe", 50.14, 8.57, "Euro Stoxx 50 futures", proxy="FEZ"),
    E("XDUS", "Börse Düsseldorf", "Düsseldorf", "Germany", "Europe", 51.23, 6.78, "DAX", proxy="EWG"),
    E("XHAM", "Börse Hamburg", "Hamburg", "Germany", "Europe", 53.55, 9.99, "DAX", proxy="EWG"),
    E("XSTU", "Börse Stuttgart", "Stuttgart", "Germany", "Europe", 48.78, 9.18, "DAX", proxy="EWG"),
    E("XPAR", "Euronext Paris", "Paris", "France", "Europe", 48.87, 2.34, "CAC 40", proxy="EWQ"),
    E("XAMS", "Euronext Amsterdam", "Amsterdam", "Netherlands", "Europe", 52.37, 4.90, "AEX", proxy="EWN"),
    E("XBRU", "Euronext Brussels", "Brussels", "Belgium", "Europe", 50.85, 4.35, "BEL 20", proxy="EWK"),
    E("XLIS", "Euronext Lisbon", "Lisbon", "Portugal", "Europe", 38.72, -9.14, "PSI"),
    E("XDUB", "Euronext Dublin", "Dublin", "Ireland", "Europe", 53.35, -6.26, "ISEQ 20", proxy="EIRL"),
    E("XLUX", "Luxembourg Stock Exchange", "Luxembourg", "Luxembourg", "Europe", 49.61, 6.13, "LuxX"),
    E("XSWX", "SIX Swiss Exchange", "Zurich", "Switzerland", "Europe", 47.37, 8.54, "SMI", proxy="EWL"),
    E("XMIL", "Borsa Italiana", "Milan", "Italy", "Europe", 45.46, 9.19, "FTSE MIB", proxy="EWI"),
    E("XMAD", "Bolsa de Madrid", "Madrid", "Spain", "Europe", 40.42, -3.70, "IBEX 35", proxy="EWP"),
    E("XSTO", "Nasdaq Stockholm", "Stockholm", "Sweden", "Europe", 59.33, 18.07, "OMX Stockholm 30", proxy="EWD"),
    E("XOSL", "Oslo Børs", "Oslo", "Norway", "Europe", 59.91, 10.75, "OBX", proxy="NORW"),
    E("XCSE", "Nasdaq Copenhagen", "Copenhagen", "Denmark", "Europe", 55.68, 12.57, "OMX Copenhagen 25", proxy="EDEN"),
    E("XHEL", "Nasdaq Helsinki", "Helsinki", "Finland", "Europe", 60.17, 24.94, "OMX Helsinki 25", proxy="EFNL"),
    E("XICE", "Nasdaq Iceland", "Reykjavík", "Iceland", "Europe", 64.15, -21.94, "OMX Iceland 15"),
    E("XTAL", "Nasdaq Tallinn", "Tallinn", "Estonia", "Europe", 59.44, 24.75, "OMX Tallinn"),
    E("XRIS", "Nasdaq Riga", "Riga", "Latvia", "Europe", 56.95, 24.11, "OMX Riga"),
    E("XLIT", "Nasdaq Vilnius", "Vilnius", "Lithuania", "Europe", 54.69, 25.28, "OMX Vilnius"),
    E("XWBO", "Wiener Börse", "Vienna", "Austria", "Europe", 48.21, 16.37, "ATX", proxy="EWO"),
    E("XWAR", "Warsaw Stock Exchange", "Warsaw", "Poland", "Europe", 52.23, 21.01, "WIG20", proxy="EPOL"),
    E("XPRA", "Prague Stock Exchange", "Prague", "Czechia", "Europe", 50.08, 14.43, "PX"),
    E("XBRA", "Bratislava Stock Exchange", "Bratislava", "Slovakia", "Europe", 48.15, 17.11, "SAX"),
    E("XBUD", "Budapest Stock Exchange", "Budapest", "Hungary", "Europe", 47.50, 19.04, "BUX"),
    E("XLJU", "Ljubljana Stock Exchange", "Ljubljana", "Slovenia", "Europe", 46.06, 14.51, "SBITOP"),
    E("XZAG", "Zagreb Stock Exchange", "Zagreb", "Croatia", "Europe", 45.81, 15.98, "CROBEX"),
    E("XBEL", "Belgrade Stock Exchange", "Belgrade", "Serbia", "Europe", 44.82, 20.46, "BELEX15"),
    E("XBSE", "Bucharest Stock Exchange", "Bucharest", "Romania", "Europe", 44.43, 26.10, "BET"),
    E("ASEX", "Athens Stock Exchange", "Athens", "Greece", "Europe", 37.98, 23.73, "ATHEX Composite", proxy="GREK"),
    E("XCYS", "Cyprus Stock Exchange", "Nicosia", "Cyprus", "Europe", 35.17, 33.36, "CSE General"),
    E("XIST", "Borsa İstanbul", "Istanbul", "Türkiye", "Europe", 41.01, 28.98, "BIST 100", proxy="TUR"),
    E("XMOS", "Moscow Exchange", "Moscow", "Russia", "Europe", 55.76, 37.62, "MOEX Russia"),
    # Middle East, Africa, Central Asia
    E("XSAU", "Saudi Exchange (Tadawul)", "Riyadh", "Saudi Arabia", "Middle East & Africa", 24.71, 46.68, "TASI", proxy="KSA"),
    E("XTAE", "Tel Aviv Stock Exchange", "Tel Aviv", "Israel", "Middle East & Africa", 32.08, 34.78, "TA-35", proxy="EIS"),
    E("XJSE", "Johannesburg Stock Exchange", "Johannesburg", "South Africa", "Middle East & Africa", -26.11, 28.06, "FTSE/JSE Top 40", proxy="EZA"),
    E("AIXK", "Astana International Exchange", "Astana", "Kazakhstan", "Middle East & Africa", 51.13, 71.43, "AIX"),
    # Asia-Pacific
    E("XTKS", "Tokyo Stock Exchange", "Tokyo", "Japan", "Asia-Pacific", 35.68, 139.77, "Nikkei 225 · TOPIX", proxy="EWJ"),
    E("XKRX", "Korea Exchange", "Seoul", "South Korea", "Asia-Pacific", 37.52, 126.92, "KOSPI", proxy="EWY"),
    E("XSHG", "Shanghai Stock Exchange", "Shanghai", "China", "Asia-Pacific", 31.23, 121.47, "SSE Composite", proxy="ASHR"),
    E("XSHE", "Shenzhen Stock Exchange", "Shenzhen", "China", "Asia-Pacific", 22.54, 114.06, "SZSE Component", "XSHG", proxy="CNXT"),
    E("XHKG", "Hong Kong Exchanges", "Hong Kong", "Hong Kong", "Asia-Pacific", 22.28, 114.16, "Hang Seng", proxy="EWH"),
    E("XTAI", "Taiwan Stock Exchange", "Taipei", "Taiwan", "Asia-Pacific", 25.03, 121.56, "TAIEX", proxy="EWT"),
    E("XSES", "Singapore Exchange", "Singapore", "Singapore", "Asia-Pacific", 1.28, 103.85, "Straits Times Index", proxy="EWS"),
    E("XKLS", "Bursa Malaysia", "Kuala Lumpur", "Malaysia", "Asia-Pacific", 3.15, 101.70, "FTSE Bursa Malaysia KLCI", proxy="EWM"),
    E("XBKK", "Stock Exchange of Thailand", "Bangkok", "Thailand", "Asia-Pacific", 13.73, 100.52, "SET", proxy="THD"),
    E("XIDX", "Indonesia Stock Exchange", "Jakarta", "Indonesia", "Asia-Pacific", -6.20, 106.82, "IDX Composite", proxy="EIDO"),
    E("XPHS", "Philippine Stock Exchange", "Manila", "Philippines", "Asia-Pacific", 14.55, 121.02, "PSEi", proxy="EPHE"),
    E("XBOM", "BSE", "Mumbai", "India", "Asia-Pacific", 18.93, 72.83, "S&P BSE Sensex", proxy="INDA"),
    E("XNSE", "National Stock Exchange of India", "Mumbai", "India", "Asia-Pacific", 19.06, 72.86, "Nifty 50", "XBOM", proxy="INDA"),
    E("XKAR", "Pakistan Stock Exchange", "Karachi", "Pakistan", "Asia-Pacific", 24.86, 67.01, "KSE-100"),
    E("XBDA", "Dhaka Stock Exchange", "Dhaka", "Bangladesh", "Asia-Pacific", 23.73, 90.41, "DSEX"),
    E("XASX", "Australian Securities Exchange", "Sydney", "Australia", "Asia-Pacific", -33.87, 151.21, "S&P/ASX 200", proxy="EWA"),
    E("XNZE", "NZX", "Wellington", "New Zealand", "Asia-Pacific", -41.29, 174.78, "S&P/NZX 50", proxy="ENZL"),
)
PROXIES = sorted({e.proxy for e in EXCHANGES if e.proxy})

_calendars: dict[str, object] = {}
_lock = threading.Lock()


def _calendar(name: str):
    import exchange_calendars as xc

    with _lock:
        cal = _calendars.get(name)
        if cal is None:
            today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
            start, end = today - pd.Timedelta(days=30), today + pd.Timedelta(days=400)
            try:
                cal = xc.get_calendar(name, start=start, end=end)
            except Exception:
                # Some exchanges publish next year's holidays late in the year (China, Singapore,
                # India): use what is recorded and say so rather than guess.
                cal = xc.get_calendar(name, start=start)
            _calendars[name] = cal
        return cal


def warm() -> None:
    """Build every calendar (a few seconds); run in a background thread at start-up."""
    for e in EXCHANGES:
        try:
            _calendar(e.calendar or e.code)
        except Exception:  # an unknown calendar must not stop the others
            pass


def _iso(t) -> str | None:
    return None if t is None or pd.isna(t) else pd.Timestamp(t).tz_convert("UTC").isoformat()


def status(e: Exchange, now: datetime) -> dict:
    cal = _calendar(e.calendar or e.code)
    minute = pd.Timestamp(now).tz_convert("UTC").floor("min")
    tz = str(cal.tz)
    local_now = minute.tz_convert(tz)
    sessions = cal.sessions
    today = local_now.tz_localize(None).normalize()
    out = {"code": e.code, "name": e.name, "city": e.city, "country": e.country, "region": e.region,
           "lat": e.lat, "lon": e.lon, "index": e.index, "timezone": tz,
           "localTime": local_now.strftime("%a %H:%M")}
    if cal.is_open_on_minute(minute, ignore_breaks=True):
        session = cal.minute_to_session(minute, direction="previous")
        close = cal.session_close(session)
        out["openedAt"] = _iso(cal.session_open(session))
        b0, b1 = cal.session_break_start(session), cal.session_break_end(session)
        if not pd.isna(b0) and b0 <= minute < b1:
            out.update(state="break", until=_iso(b1), event="reopens")
        else:
            nxt = b0 if not pd.isna(b0) and minute < b0 else close
            out.update(state="open", until=_iso(nxt), event="lunch break" if nxt is b0 else "closes")
    else:
        nxt = cal.next_open(minute)
        out.update(state="closed", until=_iso(nxt), event="opens")
    if today in sessions:
        o, c = cal.session_open(today), cal.session_close(today)
        out["today"] = {"open": o.tz_convert(tz).strftime("%H:%M"), "close": c.tz_convert(tz).strftime("%H:%M")}
        b0, b1 = cal.session_break_start(today), cal.session_break_end(today)
        if not pd.isna(b0):
            out["today"]["break"] = f"{b0.tz_convert(tz):%H:%M}-{b1.tz_convert(tz):%H:%M}"
        # An early close is news only when it breaks the pattern (Tel Aviv closes early every
        # Friday; that is routine): not if the same weekday was early in each of the last 3 weeks.
        early = set(cal.early_closes)
        same_weekday = [today - pd.Timedelta(weeks=k) for k in range(1, 9)]
        recent = [d for d in same_weekday if d in sessions][:4]  # holidays skipped
        routine = bool(recent) and all(d in early for d in recent)
        out["earlyClose"] = today in early and not routine
    else:
        out["today"] = None
        # A normal trading weekday with no session: a holiday. Ad-hoc ones (days of mourning,
        # storms, emergencies) are news; the scheduled calendar is not.
        out["holidayToday"] = cal.weekmask[today.dayofweek] == "1" and today <= sessions[-1]
        adhoc = {pd.Timestamp(d).normalize() for d in getattr(cal, "adhoc_holidays", [])}
        out["unscheduledClosure"] = bool(out["holidayToday"] and today in adhoc)
    # Upcoming closures on the exchange's normal trading days (its own weekend excluded).
    days = pd.date_range(today, today + pd.Timedelta(days=60), freq="D")
    trading_weekdays = days[[cal.weekmask[d.dayofweek] == "1" for d in days]]
    last = sessions[-1]
    out["holidays"] = [d.strftime("%Y-%m-%d") for d in trading_weekdays if d not in sessions and d <= last][:4]
    out["calendarEnds"] = last.strftime("%Y-%m-%d")
    out["weekend"] = [i for i, c in enumerate(cal.weekmask) if c == "0"]
    return out


def world_markets(now: datetime | None = None, moves: dict[str, dict] | None = None) -> list[dict]:
    """Every exchange's state now; with `moves` (proxy ticker -> {day, changePct}) each also
    carries its market's latest daily change, from its US-listed proxy fund."""
    now = now or datetime.now(timezone.utc)
    moves = moves or {}
    out = []
    for e in EXCHANGES:
        try:
            row = status(e, now)
            if e.proxy and e.proxy in moves:
                row["performance"] = {"proxy": e.proxy, **moves[e.proxy]}
            out.append(row)
        except Exception as err:  # keep the rest if one calendar has a problem
            out.append({"code": e.code, "name": e.name, "city": e.city, "country": e.country, "region": e.region,
                        "lat": e.lat, "lon": e.lon, "index": e.index, "state": "unknown", "error": str(err)})
    return out


def subsolar(now: datetime | None = None) -> dict:
    """Where the sun is overhead now: the longitude where it is solar noon, and the latitude
    (declination). Within about half a degree (NOAA's simple formulas): plenty to place noon on a globe."""
    import math

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.timetuple().tm_yday
    hours = now.hour + now.minute / 60 + now.second / 3600
    g = 2 * math.pi / 365 * (day - 1 + (hours - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))  # minutes
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lon = -15 * (hours - 12 + eqtime / 60)
    lon = (lon + 180) % 360 - 180
    return {"lat": math.degrees(decl), "lon": lon, "at": now.isoformat()}

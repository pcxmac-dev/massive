"""Directory of every Nasdaq/NYSE listing and futures product, with company icons on leftover budget."""
from datetime import date, datetime

from massive_collate import collector
from massive_collate.models import Instrument

from .conftest import FakeSource

TODAY = date(2026, 10, 23)


class DirSource(FakeSource):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.icons_asked = []

        self.venues = {"XNAS": [("AAPL", "CS"), ("MSFT", "CS"), ("QQQ", "ETF"), ("TINY", "CS")],
                       "XNYS": [("JPM", "CS"), ("BRK.B", "CS")], "XASE": [("IMO", "CS")], "ARCX": [("SPY", "ETF")]}

    def listings(self, exchange):
        self._spend()
        return [{"ticker": t, "name": f"{t} Inc.", "exchange": exchange, "type": ty, "kind": "stock", "figi": f"F-{t}"}
                for t, ty in self.venues[exchange]]

    def futures_products(self, day=None):
        self._spend()
        return [{"ticker": "ES", "name": "E-mini S&P 500", "exchange": "XCME", "type": "Equity", "kind": "future"}]

    def instrument(self, ticker, resolve_exchange=True):
        self._spend()
        return Instrument(ticker, details={"branding": {"icon_url": f"https://api.massive.com/icon/{ticker}.png"}})

    def icon(self, url):
        self._spend()
        self.icons_asked.append(url.rsplit("/", 1)[1].removesuffix(".png"))
        return b"\x89PNG....", "image/png"


def _snapshot(store):
    bars = [{"T": "AAPL", "c": 200, "v": 50e6}, {"T": "MSFT", "c": 400, "v": 20e6}, {"T": "JPM", "c": 300, "v": 10e6},
            {"T": "TINY", "c": 2, "v": 1e5}, {"T": "BRK.B", "c": 500, "v": 4e6}]
    store.save_market_snapshot("fake", date(2026, 10, 22), bars)


def test_directory_lists_exchanges_and_futures_daily(store):
    src = DirSource()
    assert collector.refresh_directory(src, store, TODAY, log=lambda m: None) == 5  # 4 venues + products
    rows = {r["ticker"]: r for r in store.directory("fake")}
    assert set(rows) == {"AAPL", "MSFT", "QQQ", "TINY", "JPM", "BRK.B", "IMO", "SPY", "ES"}
    assert rows["ES"]["kind"] == "future" and rows["JPM"]["exchange"] == "XNYS" and rows["SPY"]["exchange"] == "ARCX"
    assert collector.refresh_directory(src, store, TODAY, log=lambda m: None) == 0  # once a day
    assert collector.refresh_directory(src, store, date(2026, 10, 24), log=lambda m: None) == 5
    assert store.listing_events("fake") == []  # the first listing is a starting point, not news


def test_new_retired_relisted_and_reassigned_listings_are_marked(store):
    from datetime import timedelta

    src = DirSource()
    src.venues["XNAS"] += [(f"X{i}", "CS") for i in range(40)]  # enough listings that one leaving is normal
    collector.refresh_directory(src, store, TODAY, log=lambda m: None)
    store.watch("fake", ["TINY"], "minute")
    lines = []
    src.venues["XNAS"] = [v for v in src.venues["XNAS"] if v[0] != "TINY"] + [("NEWCO", "CS")]
    day2 = TODAY + timedelta(days=1)
    collector.refresh_directory(src, store, day2, log=lines.append)
    events = {(e["ticker"], e["event"]) for e in store.listing_events("fake")}
    assert events == {("NEWCO", "listed"), ("TINY", "retired")}
    assert "TINY" not in {r["ticker"] for r in store.directory("fake")}  # kept, but not offered as active
    assert store.watchlist("fake")[0]["retired_on"] == day2  # watched: marked, history still filled
    assert any("TINY: no longer listed" in l for l in lines)
    day3 = day2 + timedelta(days=1)
    src.venues["XNAS"].append(("TINY", "CS"))
    collector.refresh_directory(src, store, day3, log=lines.append)
    assert store.watchlist("fake")[0]["retired_on"] is None
    assert ("TINY", "relisted") in {(e["ticker"], e["event"]) for e in store.listing_events("fake", since=day3)}
    old = src.listings
    src.listings = lambda mic: [dict(r, figi="F-OTHER") if r["ticker"] == "MSFT" else r for r in old(mic)]
    collector.refresh_directory(src, store, day3 + timedelta(days=1), log=lines.append)
    assert [e["event"] for e in store.listing_events("fake", since=day3 + timedelta(days=1))] == ["reassigned"]


def test_a_reply_missing_many_listings_retires_none(store):
    from datetime import timedelta

    src = DirSource()
    collector.refresh_directory(src, store, TODAY, log=lambda m: None)
    src.venues["XNAS"] = []  # a venue missing from the reply, not four delistings
    lines = []
    collector.refresh_directory(src, store, TODAY + timedelta(days=1), log=lines.append)
    assert len([r for r in store.directory("fake") if r["kind"] == "stock"]) == 8
    assert any("none retired" in l for l in lines)


def test_futures_products_are_asked_for_one_day():
    from massive_collate.sources.massive import MassiveSource

    class Client:
        requests_made = 0

        def __init__(self):
            self.asked = []

        def pages(self, path, params=None, max_pages=None):
            self.asked.append((path, dict(params)))
            if params["date"] == "2026-10-24":  # a Saturday: no record
                return iter([[]])
            return iter([[{"product_code": "ES", "name": "E-mini S&P 500", "trading_venue": "XCME", "sector": "Equity"},
                          {"product_code": "ES", "name": "E-mini S&P 500", "trading_venue": "XCME", "sector": "Equity"},
                          {"product_code": "GC", "name": "Gold", "trading_venue": "XCEC", "sector": "Metals"}]])

    c = Client()
    rows = MassiveSource(c).futures_products(date(2026, 10, 24))
    assert [r["ticker"] for r in rows] == ["ES", "GC"] and rows[1]["exchange"] == "XCEC"
    assert [p["date"] for _, p in c.asked] == ["2026-10-24", "2026-10-23"]  # stepped back a day
    assert all(p["type"] == "single" and p["limit"] == 50000 for _, p in c.asked)


def test_icons_go_to_the_biggest_companies_first_within_the_allowance(store):
    src = DirSource()
    collector.refresh_directory(src, store, TODAY, log=lambda m: None)
    _snapshot(store)
    used = collector.fill_directory_icons(src, store, allowance=6, today=TODAY, log=lambda m: None)
    assert used == 6 and src.icons_asked == ["AAPL", "MSFT", "JPM"]  # by dollar volume, 2 requests each
    stats = store.directory_stats("fake", collector.COMPANY_TYPES)
    assert stats["companies"] == 6 and stats["icons"] == 3  # funds (QQQ, SPY) are not companies
    collector.fill_directory_icons(src, store, allowance=100, today=TODAY, log=lambda m: None)
    assert src.icons_asked == ["AAPL", "MSFT", "JPM", "BRK.B", "TINY", "IMO"]  # the rest, never the same twice
    assert collector.fill_directory_icons(src, store, allowance=100, today=TODAY, log=lambda m: None) == 0


def test_no_allowance_no_requests(store):
    src = DirSource()
    collector.refresh_directory(src, store, TODAY, log=lambda m: None)
    before = src.requests_made
    assert collector.fill_directory_icons(src, store, allowance=1, today=TODAY, log=lambda m: None) == 0
    assert src.requests_made == before


def test_sync_does_directory_work_only_after_the_watchlist_and_only_when_allowed(store):
    from massive_collate.collector import ET, sync
    from massive_collate.config import TIERS

    src = DirSource(entitled=("minute",))
    sync(src, store, TIERS["free"], datetime(2026, 10, 23, 12, tzinfo=ET), log=lambda m: None, directory_allowance=0)
    assert store.directory("fake") == []
    sync(src, store, TIERS["free"], datetime(2026, 10, 23, 13, tzinfo=ET), log=lambda m: None, directory_allowance=20)
    assert len(store.directory("fake")) == 9 and src.icons_asked


def test_a_new_logo_link_is_fetched_again(store):
    src = DirSource()
    collector.refresh_directory(src, store, TODAY, log=lambda m: None)
    collector.fill_directory_icons(src, store, allowance=100, today=TODAY, log=lambda m: None)
    asked = len(src.icons_asked)
    # unchanged details: nothing fetched
    assert collector.fetch_icon(src, store, "AAPL", log=lambda m: None) is False
    # the company rebrands: its details now carry a different logo link
    store.set_details("fake", "AAPL", {"branding": {"icon_url": "https://api.massive.com/icon/AAPL-2027.png"}})
    assert store.icon_link_changes("fake") == ["AAPL"]
    assert collector.fetch_icon(src, store, "AAPL", log=lambda m: None) is True
    assert len(src.icons_asked) == asked + 1 and store.icon_link_changes("fake") == []

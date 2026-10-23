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

    def listings(self, exchange):
        self._spend()
        rows = {"XNAS": [("AAPL", "CS"), ("MSFT", "CS"), ("QQQ", "ETF"), ("TINY", "CS")],
                "XNYS": [("JPM", "CS"), ("BRK.B", "CS"), ("SPY", "ETF")]}[exchange]
        return [{"ticker": t, "name": f"{t} Inc.", "exchange": exchange, "type": ty, "kind": "stock"} for t, ty in rows]

    def futures_products(self):
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


def test_directory_lists_exchanges_and_futures_weekly(store):
    src = DirSource()
    assert collector.refresh_directory(src, store, TODAY, log=lambda m: None) == 3  # 2 listings + products
    rows = {r["ticker"]: r for r in store.directory("fake")}
    assert set(rows) == {"AAPL", "MSFT", "QQQ", "TINY", "JPM", "BRK.B", "SPY", "ES"}
    assert rows["ES"]["kind"] == "future" and rows["JPM"]["exchange"] == "XNYS"
    assert collector.refresh_directory(src, store, TODAY, log=lambda m: None) == 0  # once a week


def test_icons_go_to_the_biggest_companies_first_within_the_allowance(store):
    src = DirSource()
    collector.refresh_directory(src, store, TODAY, log=lambda m: None)
    _snapshot(store)
    used = collector.fill_directory_icons(src, store, allowance=6, today=TODAY, log=lambda m: None)
    assert used == 6 and src.icons_asked == ["AAPL", "MSFT", "JPM"]  # by dollar volume, 2 requests each
    stats = store.directory_stats("fake", collector.COMPANY_TYPES)
    assert stats["companies"] == 5 and stats["icons"] == 3  # funds (QQQ, SPY) are not companies
    collector.fill_directory_icons(src, store, allowance=100, today=TODAY, log=lambda m: None)
    assert src.icons_asked == ["AAPL", "MSFT", "JPM", "BRK.B", "TINY"]  # the rest, never the same twice
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
    assert len(store.directory("fake")) == 8 and src.icons_asked

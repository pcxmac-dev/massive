"""Profiles (no passwords; the user, 2026-10-27): each keeps its own watchlists, layouts (markups),
preferences and register of the stocks it opened; the login page (a Vitruvian Man) picks or makes
one; the fetcher follows the signed-in profile's watchlists. The first profile holds what came
before profiles; a new one starts with an empty watchlist."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from massive_collate import collector, live
from massive_collate.gui import server
from massive_collate.gui.server import ChartApp, make_handler


def test_the_first_profile_holds_what_came_before_and_a_new_one_starts_empty(store):
    store.kv_set(live.WATCHLISTS_KEY, json.dumps([{"name": "Mine", "tickers": ["SPCX"]}]))  # from before profiles
    first = store.create_layout("Default", {"drawings": {"SPCX": [1]}})
    assert [p["name"] for p in store.profiles()] == ["Default"] and store.active_profile() == 1
    app = ChartApp(store)
    new = app.create_profile({"name": "  Trader   Two "})
    assert new["name"] == "Trader Two" and new["id"] != 1
    with pytest.raises(ValueError):
        app.create_profile({"name": "trader two"})  # names are unique, whatever the case
    assert app.watchlists(1)["lists"][0]["tickers"] == ["SPCX"] and app.watchlists(new["id"]) == {"lists": []}
    assert [lay["id"] for lay in app.layouts(1)["layouts"]] == [first["id"]] and app.layouts(new["id"])["layouts"] == []
    app.rename_profile(1, {"name": "Ciaran"})
    assert store.profile(1)["name"] == "Ciaran"


def test_watchlists_and_layouts_are_each_profiles_own(store):
    app = ChartApp(store)
    two = app.create_profile({"name": "Two"})["id"]
    app.save_watchlists({"lists": [{"name": "Tech", "tickers": ["aapl", "msft"]}]}, two)
    assert app.watchlists(1) == {"lists": []} and app.watchlists(two)["lists"][0]["tickers"] == ["AAPL", "MSFT"]
    mine = app.create_layout({"name": "Two's", "data": {}}, two)
    assert app.layouts(1)["layouts"] == []
    with pytest.raises(LookupError):
        app.layout(mine["id"], 1)  # another profile's markups are not reachable
    with pytest.raises(LookupError):
        app.delete_layout(mine["id"], 1)
    assert app.update_layout(mine["id"], {"name": "Renamed"}, two)["name"] == "Renamed"


def test_every_stock_opened_goes_in_the_profiles_register(store, monkeypatch):
    for t in ("SPCX", "AAPL"):
        store.watch("massive", [t], "minute", exchange="XNAS")
    monkeypatch.setattr(server.activity, "fetching_feeds", lambda path: {"massive": 9})  # nothing starts
    app = ChartApp(store)
    two = app.create_profile({"name": "Two"})["id"]
    app.priority("SPCX", two)
    app.priority("AAPL", two)
    app.priority("SPCX", two)
    visits = app.register(two)["visits"]
    assert [v["ticker"] for v in visits][0] in ("SPCX", "AAPL") and {v["ticker"]: v["count"] for v in visits} == {"SPCX": 2, "AAPL": 1}
    assert app.register(1)["visits"] == []  # another profile's register is its own


def test_the_fetcher_follows_the_signed_in_profiles_watchlists(store):
    app = ChartApp(store)
    store.kv_set(live.WATCHLISTS_KEY, json.dumps([{"name": "First", "tickers": ["AAA"]}]))
    two = app.create_profile({"name": "Two"})["id"]
    app.save_watchlists({"lists": [{"name": "Mine", "tickers": ["ZZZ", "YYY"]}]}, two)
    assert collector.watchlist_tickers(store) == ["AAA"]  # nobody signed in yet: the first profile's
    app.sign_in(two)
    assert collector.watchlist_tickers(store) == ["ZZZ", "YYY"] and "ZZZ" in collector.picked_symbols(store)
    assert store.profile(two)["seen_at"] and store.profiles()[0]["id"] == two  # the latest signed in first
    with pytest.raises(LookupError):
        app.sign_in(999)


def test_the_login_page_until_a_profile_is_chosen_then_the_app(tmp_path):
    from massive_collate.storage import Store

    store = Store(tmp_path / "m.db", check_same_thread=False)  # the server's threads share it
    app = ChartApp(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def ask(method, path, body=None, cookie=None):
        c = HTTPConnection("127.0.0.1", port)
        headers = {"Host": f"127.0.0.1:{port}", "X-Massive-Charts": "1", "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        return r.status, r.getheader("Set-Cookie"), r.read()

    try:
        status, _, page = ask("GET", "/")
        assert status == 200 and b"login.js" in page and b"Choose your profile" in page  # no profile chosen
        status, _, made = ask("POST", "/api/profiles", {"name": "Two"})
        two = json.loads(made)["id"]
        status, cookie, _ = ask("POST", "/api/profiles/sign-in", {"id": two})
        assert status == 200 and cookie.startswith(f"mc_profile={two};") and "SameSite=Strict" in cookie
        status, _, page = ask("GET", "/", cookie=f"mc_profile={two}")
        assert b'id="railProfile"' in page  # the app, as Two
        assert b"login.js" in ask("GET", "/", cookie="mc_profile=999")[2]  # a profile that is gone: the login page
        ask("PUT", "/api/watchlists", {"lists": [{"name": "W", "tickers": ["SPCX"]}]}, cookie=f"mc_profile={two}")
        assert json.loads(ask("GET", "/api/watchlists", cookie=f"mc_profile={two}")[2])["lists"][0]["name"] == "W"
        assert json.loads(ask("GET", "/api/watchlists")[2]) == {"lists": []}  # no cookie: the first profile's
        assert json.loads(ask("GET", "/api/profiles", cookie=f"mc_profile={two}")[2])["current"] == two
        status, cookie, _ = ask("POST", "/api/profiles/sign-out", {}, cookie=f"mc_profile={two}")
        assert "Max-Age=0" in cookie
    finally:
        httpd.shutdown()
        store.close()


def test_only_a_stock_you_open_goes_in_the_register_not_one_the_page_opened(tmp_path, monkeypatch):
    from massive_collate.storage import Store

    store = Store(tmp_path / "m.db", check_same_thread=False)  # the server's threads share it
    for t in ("SPCX", "AAPL"):
        store.watch("massive", [t], "minute", exchange="XNAS")
    monkeypatch.setattr(server.activity, "fetching_feeds", lambda path: {"massive": 9})  # nothing starts
    app = ChartApp(store)
    two = app.create_profile({"name": "Two"})["id"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def open_(query):
        c = HTTPConnection("127.0.0.1", port)
        c.request("POST", f"/api/priority?{query}", headers={
            "Host": f"127.0.0.1:{port}", "X-Massive-Charts": "1", "Cookie": f"mc_profile={two}"})
        return c.getresponse().status

    try:
        assert open_("ticker=AAPL") == 202  # the first symbol, opened by the page itself: fetched first, not noted
        assert open_("ticker=SPCX&visit=1") == 202  # picked from the watchlist, the search, the news
        assert [v["ticker"] for v in app.register(two)["visits"]] == ["SPCX"]
        assert open_("ticker=ADAUSD&visit=1") == 404  # no feed fetches it, but you opened it: noted
        assert {v["ticker"] for v in app.register(two)["visits"]} == {"SPCX", "ADAUSD"}
    finally:
        httpd.shutdown()
        store.close()


def test_a_page_of_another_profile_cannot_write_and_odd_cookies_or_lengths_are_refused(tmp_path):
    import socket

    from massive_collate.storage import Store

    store = Store(tmp_path / "m.db", check_same_thread=False)  # the server's threads share it
    app = ChartApp(store)
    two = app.create_profile({"name": "Two"})["id"]
    three = app.create_profile({"name": "Three"})["id"]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, [0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def ask(method, path, body=None, cookie=None, page=None):
        c = HTTPConnection("127.0.0.1", port)
        headers = {"Host": f"127.0.0.1:{port}", "X-Massive-Charts": "1", "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        if page is not None:
            headers[server.PROFILE_HEADER] = str(page)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        return r.status, r.read()

    try:
        lists = {"lists": [{"name": "Mine", "tickers": ["SPCX"]}]}
        # a tab loaded as Two, while another tab signed in as Three: refused, Three's lists untouched
        status, answer = ask("PUT", "/api/watchlists", lists, cookie=f"mc_profile={three}", page=two)
        assert status == 409 and json.loads(answer)["reload"] is True
        assert json.loads(ask("GET", "/api/watchlists", cookie=f"mc_profile={three}")[1]) == {"lists": []}
        assert ask("PUT", "/api/watchlists", lists, cookie=f"mc_profile={two}", page=two)[0] == 200  # its own
        assert ask("PUT", "/api/watchlists", lists, page=two)[0] == 409  # signed out since: not the first's either
        bad = {"lists": [{"name": "Blank", "tickers": ["  "]}]}
        assert ask("PUT", "/api/watchlists", bad, cookie=f"mc_profile={two}", page=two)[0] == 400
        # a cookie no profile can be ('²' is a digit to isdigit, not to int; too big for SQLite): signed out
        assert b"login.js" in ask("GET", "/", cookie="mc_profile=²")[1]
        assert b"login.js" in ask("GET", "/", cookie="mc_profile=99999999999999999999")[1]
        # a Content-Length that is not a size: an answer, not a dropped connection or a hung thread
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(f"PUT /api/watchlists HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Massive-Charts: 1\r\n"
                      "Content-Length: -5\r\n\r\n".encode())
            assert b" 400 " in s.recv(200).split(b"\r\n")[0]
        # the cookie is checked without the shared lock: the page answers while something holds it
        app._profile_ids, app._profile_ids_at = set(), float("-inf")  # read again: still not under the lock
        with app._lock:
            got = []
            t = threading.Thread(target=lambda: got.append(ask("GET", "/", cookie=f"mc_profile={three}")))
            t.start()
            t.join(5)
        assert got and got[0][0] == 200 and b'id="railProfile"' in got[0][1]  # the app, as Three
    finally:
        httpd.shutdown()
        store.close()

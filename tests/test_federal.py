"""The Federal plugin (sources/plugins/federal/): the federal agencies, each a part of one plugin
(Publisher.part_of, plugin API 1.2), with its own scrape checks, state and pause; pausing the plugin
stops them all; its record and a part with datasets only are never read; one Plugins view entry, one
`plugins` row; the Gov view named Federal. The user, 2026-10-30: "merge everything in to a Federal
plugin, from news feeds to edgar access, to everything that matters"."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from massive_collate import publications
from massive_collate.config import load_settings
from massive_collate.gov import GovDB
from massive_collate.sources import publishers, registry
from massive_collate.sources.publisher import Feed, Policy, Publisher

from .test_publications import Site, pub, reader

AGENCIES = ["fed", "nyfed", "sec", "bls", "fedreg", "cftc"]
STATIC = Path(__file__).resolve().parents[1] / "src" / "massive_collate" / "gui" / "static"


def test_the_federal_agencies_are_parts_of_one_plugin():
    from massive_collate.sources.plugins import federal

    assert [p.name for p in federal.PUBLISHERS] == ["federal", *AGENCIES]
    for gone in ("fed", "nyfed", "sec", "bls", "fedreg"):  # no plugin of their own now
        assert importlib.util.find_spec(f"massive_collate.sources.plugins.{gone}") is None, gone
    pubs = publishers()
    assert list(pubs)[:7] == ["federal", *AGENCIES]  # the plugin's record first, its agencies after it
    assert all(pubs[n].part_of == "federal" and (pubs[n].version, pubs[n].api) == ("1.0.0", "1.2") for n in AGENCIES)
    assert pubs["federal"].feeds == () and pubs["federal"].part_of == ""
    assert registry.publisher_parts("federal") == AGENCIES and registry.publisher_of("sec") == "federal"
    assert registry.publisher_of("massive-news") == "massive-news" and registry.publisher_parts("massive-news") == []
    # each agency keeps what it was: its feeds (their keys name the state kept), its hosts and checks
    assert [f.key for f in pubs["fed"].feeds] == ["fed-press", "fed-speeches"] and pubs["sec"].policy.contact
    assert pubs["fed"].calendar is not None and pubs["nyfed"].moments is not None
    # the CFTC: its Commitments of Traders, a dataset; nothing of its own to read (its feeds are off)
    assert pubs["cftc"].feeds == () and pubs["cftc"].datasets == ("cftc-cot-legacy",)
    from massive_collate import datasets

    assert pubs["cftc"].datasets[0] in datasets.DATASETS


def test_a_publisher_part_of_none_here_is_one_of_its_own(monkeypatch):
    stray = Publisher("stray", "Stray", "", (Feed("stray-a", "S", "A", "https://s.gov/a"),), Policy(hosts=("s.gov",)),
                      part_of="nope", api="1.2", order=900)
    monkeypatch.setattr(registry, "_entry_points", lambda: [type("EP", (), {"name": "stray", "value": "pkg:stray",
                                                                              "load": lambda self: stray})()])
    try:
        registry.reload()
        assert registry.publisher_of("stray") == "stray" and "stray" in {r["name"] for r in registry.publisher_menu()}
        view = publications.view(GovDB(None), publishers=publishers())
        assert next(p for p in view if p["name"] == "stray")["partOf"] == ""  # listed on its own, not lost
    finally:
        registry._providers = None


def test_pausing_federal_pauses_every_agency_in_it_and_only_them(store):
    site = Site()
    r = reader(site, store=store)
    part = Publisher("t", "Test agency", "test", pub().feeds, pub().policy, part_of="plug")
    plugin = Publisher("plug", "Plugin", "", (), Policy())
    alone = pub()
    alone = Publisher("u", "Other", "", (Feed("u-press", "U", "Press", "https://news.example.gov/u.xml"),), alone.policy)
    now = datetime.now(timezone.utc)
    publications.pause(store, "plug", True)
    assert r.kill(part, now)["whose"] == "plugin" and r.kill(alone, now) is None
    r.step(now, publishers={"plug": plugin, "t": part})
    assert site.asked == []  # nothing for the paused plugin's part, robots.txt neither
    view = {p["name"]: p for p in publications.view(GovDB(None), store=store, now=now,
                                                     publishers={"plug": plugin, "t": part, "u": alone})}
    assert view["t"]["state"] == "paused" and view["plug"]["state"] == "paused" and view["u"]["state"] != "paused"
    assert view["plug"]["parts"] == ["t"] and view["t"]["partOf"] == "plug"
    publications.pause(store, "plug", False)
    r.step(datetime.now(timezone.utc), publishers={"plug": plugin, "t": part})
    assert site.asked  # resumed with it


def test_a_publisher_with_no_feed_of_its_own_is_never_read_or_rechecked(store):
    site = Site()
    r = reader(site, store=store)
    pubs = publishers()
    now = datetime.now(timezone.utc)
    r.step(now, publishers={"federal": pubs["federal"], "cftc": pubs["cftc"]})
    found = r.recheck(pubs["cftc"], now)
    assert site.asked == [] and found["notes"] == ["no feed of its own to read: nothing sent"]  # robots.txt neither
    assert r.gdb.recheck_get("cftc") is None


def test_the_plugins_view_lists_federal_once_with_its_agencies_and_the_cftcs_dataset(tmp_path):
    from massive_collate.gui.server import ChartApp
    from massive_collate.storage import Store

    with Store(tmp_path / "m.db", check_same_thread=False) as store:
        out = ChartApp(store, settings=load_settings(env={"MASSIVE_DB": str(tmp_path / "m.db")}, env_file=None)).publishers()
    by = {p["name"]: p for p in out}
    assert by["federal"]["parts"] == AGENCIES and all(by[n]["partOf"] == "federal" for n in AGENCIES)
    assert by["federal"]["requestsToday"] == sum(by[n]["requestsToday"] for n in AGENCIES)
    assert by["cftc"]["datasets"] == ["cftc-cot-legacy"] and by["cftc"]["datasetRows"][0]["name"] == "cftc-cot-legacy"
    assert by["massive-news"]["partOf"] == "" and by["massive-news"]["parts"] == []


def test_the_page_shows_federal_with_its_agencies_beneath_and_the_gov_view_is_named_federal():
    js = (STATIC / "plugins.js").read_text(encoding="utf-8")
    assert "(data.publishers || []).filter((p) => !p.partOf).map((p) => pubButton(p, false)" in js
    assert "filter((q) => q.partOf === p.name).map((q) => pubButton(q, true))" in js
    assert "if (p.parts.length) return partsHtml(p);" in js and "function datasetsHtml(p)" in js
    assert "whose === 'plugin'" in js and "(all ? 'Pause all' : 'Pause')" in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".ex-venue.pl-sub {" in css and ".pl-link {" in css
    assert "<h2>Federal</h2>" in (STATIC / "gov.js").read_text(encoding="utf-8")
    assert 'id="railGov" title="Federal: the federal agencies' in (STATIC / "index.html").read_text(encoding="utf-8")

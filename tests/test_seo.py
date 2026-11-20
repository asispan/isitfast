"""Answer-engine / search surface (app/seo.py + the /p/, /, robots.txt, sitemap.xml, llms.txt routes).
Run: python -m pytest -q   (from the repo root). Shares test_api's throwaway DATA_DIR and fixtures."""
import json
import re
import time

import pytest

from app import main, seo
from test_api import client  # noqa: F401 — module fixture: app + seeded places

DAY = 86400


def T(net, down, up, ago_s, ip="a", verified=1, carrier="ACT Fibernet", now=None):
    now = now or int(time.time())
    return {"network": net, "carrier": carrier, "down_mbps": down, "up_mbps": up, "created_at": now - ago_s,
            "verified": verified, "ip_hash": ip}


# ---------------------------------------------------------------- pure statistics
def test_session_collapse_and_rankable():
    now = int(time.time())
    # one connection re-running 5 times in 20 minutes is one reading, not five
    rows = [T("wifi", 100 + i, 20, 10 * DAY - i * 240, ip="same", now=now) for i in range(5)]
    g = seo.group_stats(sorted(rows, key=lambda r: -r["created_at"]), now)
    assert g["verified"]["readings"] == 1 and g["verified"]["tests"] == 5 and not g["rankable"]
    # 3 readings, 2 days, 2 connections -> rankable; median, not mean
    rows = [T("wifi", 10, 5, 1 * DAY, "x", now=now), T("wifi", 50, 9, 2 * DAY, "y", now=now),
            T("wifi", 400, 50, 3 * DAY, "x", now=now)]
    g = seo.group_stats(sorted(rows, key=lambda r: -r["created_at"]), now)
    assert g["rankable"] and g["verified"]["down"] == 50 and g["verified"]["down_min"] == 10
    assert g["latest"]["down"] == 10  # newest test, shown like the app does


def test_unverified_and_old_tests_never_count():
    now = int(time.time())
    rows = [T("wifi", 90, 20, DAY, verified=0, now=now), T("wifi", 80, 20, 200 * DAY, now=now)]
    g = seo.group_stats(rows, now)
    assert g["verified"] is None and not g["rankable"] and g["latest"]["verified"] is False


def test_groups_split_by_network_and_provider():
    now = int(time.time())
    rows = [T("cellular", 30, 5, DAY, "a", carrier="Jio", now=now), T("cellular", 31, 5, 2 * DAY, "b", carrier="Jio", now=now),
            T("wifi", 60, 10, DAY, now=now), T("cellular", 90, 8, DAY, carrier="Airtel", now=now)]
    groups = seo.place_groups(rows, now)
    # WiFi first, then the carrier with the most verified evidence (not the single fastest test)
    assert [g["label"] for g in groups] == ["WiFi (ACT Fibernet)", "Jio mobile data", "Airtel mobile data"]
    assert [g["label"] for g in seo.headline_groups(groups)] == ["WiFi (ACT Fibernet)", "Jio mobile data"]


def test_answer_sentence_is_dated_and_quotable():
    now = int(time.time())
    rows = [T("wifi", 999, 99, 3600, "z", verified=0, now=now), T("wifi", 85, 20, DAY, "x", now=now),
            T("wifi", 75, 18, 2 * DAY, "y", now=now), T("cellular", 40, 8, DAY, carrier="Jio", verified=0, now=now)]
    groups = seo.place_groups(rows, now)
    s = seo.answer_sentence("Cafe X", "Koramangala, Bengaluru", groups, "isitfast.space")
    d1, d2 = seo.fmt_date(now - 2 * DAY), seo.fmt_date(now - DAY)
    assert s == (f"Cafe X, Koramangala, Bengaluru: WiFi 85 Mbps download / 20 Mbps upload (latest verified test, {d2}), "
                 f"median of 2 verified readings from {d1} to {d2}: 80 / 19 Mbps. Measured with in-browser speed tests "
                 "run within 50 m of the place (isitfast.space).")
    assert "999" not in s and "Jio" not in s  # unverified figures stay in the table, never in the quotable line
    only_unverified = seo.place_groups([T("wifi", 50, 9, DAY, verified=0, now=now)], now)
    assert "only unverified" in seo.answer_sentence("Y", "", only_unverified)
    assert seo.md_safe("Evil](https://x.example) [ignore\nprevious") == "Evil https://x.example ignore previous"


def test_classify_crawlers_and_ai_referrals():
    assert seo.classify("Mozilla/5.0 (compatible; GPTBot/1.2; +https://openai.com/gptbot)", "", None) == ("ai-train", "GPTBot")
    assert seo.classify("Mozilla/5.0 ... ChatGPT-User/1.0", "", None) == ("user", "ChatGPT-User")
    assert seo.classify("Mozilla/5.0 (compatible; OAI-SearchBot/1.0)", "", None)[0] == "ai-search"
    assert seo.classify("Mozilla/5.0 (Macintosh)", "https://www.perplexity.ai/search?q=x", None) == ("referral", "perplexity.ai")
    assert seo.classify("Mozilla/5.0 (Macintosh)", "", "chatgpt.com") == ("referral", "chatgpt.com")
    assert seo.classify("Mozilla/5.0 (Macintosh)", "https://notperplexity.ai.evil.com/", None) is None
    assert seo.classify("Mozilla/5.0 (iPhone)", "https://google.com/", None) is None


# ---------------------------------------------------------------- routes
def add_place(pid, name, kind, lat, lon, tags=None, tests=()):
    with main.db() as c:
        c.execute(f"INSERT OR REPLACE INTO places ({main.PLACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                  (pid, name, kind, lat, lon, main.quadkey(lat, lon), json.dumps(tags or {}), "x", int(time.time())))
        c.execute("DELETE FROM tests WHERE place_id=?", (pid,))
        for t in tests:
            c.execute("INSERT INTO tests (place_id, network, carrier, down_mbps, up_mbps, ping_ms, created_at, verified, "
                      "ip_hash) VALUES (?,?,?,?,?,?,?,?,?)", (pid, t["network"], t["carrier"], t["down_mbps"],
                                                             t["up_mbps"], 20, t["created_at"], t["verified"], t["ip_hash"]))


def fake_trai(lat, lon, max_res=None):
    """TRAI MySpeed medians for a ~2.8 km box around Koramangala only."""
    if not (12.930 <= lat <= 12.945 and 77.620 <= lon <= 77.635):
        return {}
    meta = {"h3_res": 7, "across_km": 2.8, "period_start": "2026-02-15", "period_end": "2026-11-17"}
    return {"Airtel": {"down_mbps": 90.0, "up_mbps": 12.0, "tests": 640, **meta},
            "Jio": {"down_mbps": 150.0, "up_mbps": 20.5, "tests": 1234, **meta},
            "Vi": {"down_mbps": 40.0, "up_mbps": 8.0, "tests": 55, **meta, "h3_res": 6, "across_km": 7.5}}


def test_adjacent_hexagon_is_labelled():
    meta = {"h3_res": 7, "across_km": 2.8, "period_start": "2026-02-15", "period_end": "2026-11-19"}
    local = {"Jio": {"down_mbps": 120.0, "up_mbps": 9.0, "tests": 300, **meta, "adjacent": True, "adjacent_m": 2310},
             "Airtel": {"down_mbps": 80.0, "up_mbps": 9.0, "tests": 200, **meta}}
    s = seo.trai_sentence(local, "Cafe Y")
    assert "Jio 120 Mbps (300 area tests, from the adjacent hexagon ~2,310 m away)" in s and "Airtel 80 Mbps (200 area tests)" in s
    assert seo.trai_ok(local) and "adjacent hexagon, centre ~2,310 m away" in seo.trai_table_html(local, "x")


@pytest.fixture
def places(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "_snap", None)
    monkeypatch.setattr(main, "local_carrier_lookup", fake_trai)
    with main.db() as c:
        c.execute("DELETE FROM areas")
        c.executemany("INSERT INTO areas VALUES (?,?,?,?,?,?,?)", [
            ("bengaluru", "koramangala", "Koramangala", 12.9360, 77.6270, "n1", 0),
            ("bengaluru", "far-off", "Far Off", 13.10, 77.70, "n2", 0)])
    facts = [{"internet_access": "wlan"}, {"opening_hours": "Mo-Su 08:00-22:00"}, {"addr:street": "80 Feet Road"},
             {"website": "https://cafe3.example"}, {"addr:housenumber": "12", "addr:street": "1st Main"}, {}]
    for i, (lat, lon) in enumerate([(12.9340, 77.6250), (12.9370, 77.6280), (12.9380, 77.6300), (12.9365, 77.6255),
                                    (12.9372, 77.6262), (12.9368, 77.6275)]):
        add_place(f"n710{i}", f"Koramangala Cafe {i}", "cafe", lat, lon, facts[i])  # n7105: no venue facts
    add_place("n7110", "Koramangala Biryani", "restaurant", 12.9350, 77.6260)  # not a work spot, no venue test
    add_place("n7111", "Lonely Cafe", "cafe", 13.1001, 77.7001)                 # no TRAI data there
    add_place("n7001", "Brew & Byte </script>", "cafe", 12.9352, 77.6245, {"addr:suburb": "Koramangala"},
              [T("wifi", 85, 20, DAY, "x"), T("wifi", 75, 18, 3 * DAY, "y"), T("cellular", 40, 8, DAY, "x", carrier="Jio")])
    add_place("n7002", "Unverified Cafe", "cafe", 12.95, 77.60, {}, [T("wifi", 50, 10, DAY, verified=0)])
    add_place("s0123456789a", "Asha's Home", "house", 12.96, 77.61, {}, [T("wifi", 300, 300, DAY)])
    add_place("s0123456789b", "Visitor Named Cafe", "cafe", 12.955, 77.605, {}, [T("wifi", 70, 20, DAY)])  # unmoderated name
    add_place("gChIJ_tested_google_place_01", None, "place", 12.97, 77.62, {}, [T("wifi", 90, 30, DAY)])
    return client


def test_place_page_has_crawlable_numbers(places):
    html = places.get("/p/n7001").text
    head, body = html.split("</head>")
    assert '<meta name="robots" content="index, follow, max-snippet:-1"' in head
    assert "<title>Brew &amp; Byte &lt;/script&gt;, Koramangala — WiFi" in head
    assert 'content="WiFi 85 Mbps download / 20 Mbps upload, verified ' in head and "days ago" not in head
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', head, re.S).group(1))
    biz = ld["@graph"][1]
    assert biz["@type"] == "CafeOrCoffeeShop" and biz["name"] == "Brew & Byte </script>"
    assert biz["address"]["addressLocality"] == "Bengaluru"
    wifi_down = biz["amenityFeature"][0]
    assert wifi_down["value"] == 85.0 and wifi_down["unitCode"] == "E20" and "latest verified" in wifi_down["name"]
    assert {n["@type"] for n in ld["@graph"]} >= {"WebPage", "WebSite", "Organization"}
    assert "\\u003c/script\\u003e" in head and head.count("</script>") == head.count("<script")  # can't close the tag
    assert '<div id="panel"><article class="ssr" id="ssr"><h1>Brew &amp; Byte &lt;/script&gt;</h1>' in body
    assert "WiFi (ACT Fibernet)" in body and "Jio mobile data" in body and "__SSR__" not in html


@pytest.mark.parametrize("pid, has_ssr", [("n7002", True), ("s0123456789a", False), ("s0123456789b", True),
                                          ("gChIJ_tested_google_place_01", False), ("gChIJ_never_seen_place_0123", False)])
def test_thin_private_or_google_pages_are_noindex(places, pid, has_ssr):
    r = places.get(f"/p/{pid}")
    html = r.text
    assert '<meta name="robots" content="noindex, follow"' in html and '"CafeOrCoffeeShop"' not in html
    assert r.headers["x-robots-tag"] == "noindex"
    assert ('class="ssr"' in html) is has_ssr


def test_home_sitemap_llms_list_only_indexable_places(places):
    home = places.get("/").text
    assert '<a href="/p/n7001">' in home and "/p/n7002" not in home and "Asha" not in home and "Visitor Named" not in home
    assert "latest verified test" in home and "(2 verified readings)" in home
    assert '"@type":"WebSite"' in home
    sm = places.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    assert "<loc>https://isitfast.space/p/n7001</loc><lastmod>" in sm.text
    assert "n7002" not in sm.text and "s0123456789a" not in sm.text and "gChIJ" not in sm.text
    llms = places.get("/llms.txt", headers={"host": "isitfast.space"}).text
    assert llms.startswith("# isitfast") and "(https://isitfast.space/p/n7001?utm_source=llms)" in llms
    assert "Asha" not in llms and "not slow" in llms


def test_robots_txt(places):
    r = places.get("/robots.txt", headers={"host": "isitfast.space"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://isitfast.space/sitemap.xml" in r.text and "Content-Signal: search=yes" in r.text
    assert "Disallow: /api/\n" not in r.text  # Google renders the app, which needs the API
    assert places.head("/robots.txt").status_code == 200 and places.head("/sitemap.xml").status_code == 200


def test_canonical_host_defaults_to_space(places, monkeypatch):
    cl = places.get("/p/n7001", headers={"host": "isitfast.cloud"}).text
    assert 'rel="canonical" href="https://isitfast.space/p/n7001"' in cl
    assert 'og:url" content="https://isitfast.cloud/p/n7001"' in cl  # shares still use the domain people came in on
    assert "https://isitfast.space/sitemap.xml" in places.get("/robots.txt", headers={"host": "isitfast.cloud"}).text
    monkeypatch.setattr(main, "CANONICAL_HOST", "www.isitfast.space")
    assert 'rel="canonical" href="https://www.isitfast.space/p/n7001"' in places.get("/p/n7001").text


def test_crawl_log_records_only_bots_and_ai_referrals(places, monkeypatch):
    with main.db() as c:
        c.execute("DELETE FROM crawl_log")
    places.get("/p/n7001", headers={"user-agent": "Mozilla/5.0 AppleWebKit/537.36; compatible; ChatGPT-User/1.0"})
    places.get("/robots.txt", headers={"user-agent": "Mozilla/5.0 (compatible; GPTBot/1.2)"})
    places.get("/", headers={"referer": "https://chatgpt.com/"})
    places.get("/", headers={"user-agent": "Mozilla/5.0 (iPhone)"})
    places.get("/style.css?v=38", headers={"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})  # assets: not logged
    assert places.get("/api/places/n7001").headers["x-robots-tag"] == "noindex"
    with main.db() as c:
        rows = {(r["kind"], r["agent"], r["path"]) for r in c.execute("SELECT * FROM crawl_log")}
    assert rows == {("user", "ChatGPT-User", "/p/n7001"), ("ai-train", "GPTBot", "/robots.txt"),
                    ("referral", "chatgpt.com", "/")}
    monkeypatch.setattr(main, "METRICS_TOKEN", "")
    assert places.get("/api/crawlers").status_code == 404
    monkeypatch.setattr(main, "METRICS_TOKEN", "tok")
    rep = places.get("/api/crawlers", headers={"x-metrics-token": "tok"}).json()
    assert rep["user_fetches_of_place_pages"] == [{"path": "/p/n7001", "hits": 1}]


# ---------------------------------------------------------------- TRAI-backed cafe pages, neighbourhoods, cities
def test_untested_cafe_page_carries_area_mobile_data(places):
    r = places.get("/p/n7100")
    head, body = r.text.split("</head>")
    assert '<meta name="robots" content="index, follow, max-snippet:-1"' in head
    assert "<title>Koramangala Cafe 0, Koramangala: mobile data nearby, WiFi not tested yet · isitfast</title>" in head
    assert 'content="WiFi not tested on site yet. In the ~2.8 km area around Koramangala Cafe 0 (TRAI MySpeed' in head
    assert ("Koramangala Cafe 0, Koramangala, Bengaluru: WiFi not tested on site yet. In the ~2.8 km area around "
            "Koramangala Cafe 0 (TRAI MySpeed, 15 Feb 2026 to 17 Nov 2026; not measured at the venue), median mobile "
            "download: Jio 150 Mbps (1,234 area tests), Airtel 90 Mbps (640 area tests); Jio had the highest median.") in body
    assert "Vi 40" not in body.split("<table>")[0]  # a 7.5 km hexagon stays in the table, out of the quotable line
    assert "OpenStreetMap lists WiFi here (untested" in body and '<a href="/in/bengaluru/koramangala">Cafes in Koramangala</a>' in body
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', head, re.S).group(1))
    assert "amenityFeature" not in ld["@graph"][1]  # area medians are not this venue's features


@pytest.mark.parametrize("pid", ["n7110", "n7111", "n7105"])  # restaurant, no TRAI, no venue facts
def test_no_page_without_venue_or_fine_area_data(places, pid):
    assert '<meta name="robots" content="noindex, follow"' in places.get(f"/p/{pid}").text


def test_neighbourhood_page(places):
    r = places.get("/in/bengaluru/koramangala")
    assert r.status_code == 200 and "max-age=600" in r.headers["cache-control"] and "x-robots-tag" not in r.headers
    h = r.text
    assert '<meta name="robots" content="index, follow, max-snippet:-1"' in h
    assert '<link rel="canonical" href="https://isitfast.space/in/bengaluru/koramangala"' in h
    assert "<h1>Cafes to work from in Koramangala, Bengaluru</h1>" in h and "Jio had the highest median" in h
    assert re.search(r"\d+ cafes and coworking spaces listed, 1 with on-site speed tests", h)
    assert "not measured at any one cafe" in h
    tested, rest = h.index("Tested on site"), h.index("Cafes and coworking spaces in Koramangala")
    assert tested < h.index('href="/p/n7001"') < rest < h.index('href="/p/n7100"')
    assert "(OSM lists WiFi, untested)" in h and "Biryani" not in h and "Lonely" not in h and "/p/n7105" in h
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S).group(1))
    types = {n["@type"] for n in ld["@graph"]}
    assert {"CollectionPage", "BreadcrumbList", "WebSite"} <= types
    assert places.get("/in/bengaluru/nowhere").status_code == 404 and places.get("/in/BAD!/x").status_code == 404


def test_city_page_and_directory_files(places):
    city = places.get("/in/bengaluru").text
    assert '<a href="/in/bengaluru/koramangala">Koramangala</a>' in city and "Far Off" not in city
    assert '<a href="/in/bengaluru/koramangala">Koramangala</a>' in places.get("/").text
    sm = places.get("/sitemap.xml").text
    for path in ("/in/bengaluru", "/in/bengaluru/koramangala", "/p/n7100", "/p/n7001"):
        assert f"<loc>https://isitfast.space{path}</loc>" in sm
    assert "/p/n7110" not in sm and "/p/n7111" not in sm and "far-off" not in sm
    llms = places.get("/llms.txt").text
    assert "## Bengaluru" in llms and "[Koramangala](https://isitfast.space/in/bengaluru/koramangala?utm_source=llms)" in llms
    assert "TRAI MySpeed medians" in llms and "describe the surrounding area" in llms


def test_area_picking_and_slugs():
    from app import areas
    el = [{"type": "node", "id": i, "lat": 1, "lon": 2, "tags": {"place": "quarter", "name": f"Q{i}"}} for i in range(3)]
    el += [{"type": "node", "id": 9, "lat": 1, "lon": 2, "tags": {"place": "suburb", "name": "Bellanduru", "name:en": "Bellandur"}},
           {"type": "node", "id": 10, "lat": 1, "lon": 2, "tags": {"place": "suburb", "name": "Bellandur"}}]
    got = areas.pick(el)  # too few suburbs: quarters count too; duplicate slugs collapse
    assert [a["slug"] for a in got] == ["bellandur", "q0", "q1", "q2"]
    assert areas.slugify("HSR Layout (Sector 2)") == "hsr-layout-sector-2" and areas.slugify("Kōramangala") == "koramangala"
    assert areas.nearest([{"lat": 12.93, "lon": 77.62}], 12.93, 77.66) is None  # ~4.3 km: too far


def test_closed_cafe_and_broken_snapshot(places, monkeypatch):
    with main.db() as c:
        c.execute("UPDATE places SET gone_at=? WHERE id='n7100'", (int(time.time()),))
    assert '<meta name="robots" content="noindex, follow"' in places.get("/p/n7100").text
    monkeypatch.setattr(main, "_snap", None)

    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(main, "build_snapshot", boom)
    assert places.get("/").status_code == 200 and places.get("/p/n7101").status_code == 200  # pages survive
    assert places.get("/in/bengaluru").status_code == 404 and main._snap["places"] == []

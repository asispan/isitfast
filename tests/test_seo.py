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


@pytest.fixture
def places(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(main, "_listing", (0.0, []))
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
    assert "<loc>https://isitfast.cloud/p/n7001</loc><lastmod>" in sm.text
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


def test_canonical_host_is_optional(places, monkeypatch):
    assert 'rel="canonical" href="https://isitfast.space/p/n7001"' in places.get("/p/n7001", headers={"host": "isitfast.space"}).text
    monkeypatch.setattr(main, "CANONICAL_HOST", "isitfast.cloud")
    sp = places.get("/p/n7001", headers={"host": "isitfast.space"}).text
    assert 'rel="canonical" href="https://isitfast.cloud/p/n7001"' in sp
    assert 'og:url" content="https://isitfast.space/p/n7001"' in sp  # shares still use the domain people came in on
    assert "https://isitfast.cloud/sitemap.xml" in places.get("/robots.txt", headers={"host": "isitfast.space"}).text


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

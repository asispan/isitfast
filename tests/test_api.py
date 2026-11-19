"""API tests. Run: python -m pytest -q   (from the repo root)

Uses a throwaway DATA_DIR with a handful of seeded places; outbound HTTP is stubbed, no network needed.
"""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

TMP = tempfile.mkdtemp(prefix="isitfast-test-")
os.environ["DATA_DIR"] = TMP
os.environ.setdefault("IP_SALT", "test-salt-0123456789")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

PLACE = ("n1", "Test Cafe", "cafe", 12.92621, 77.62548)
PLACE2 = ("n2", "Other Cafe", "cafe", 12.93518, 77.61353)
PLACE3 = ("n3", "Third Cafe", "cafe", 12.94000, 77.62000)
EDGE = {"x-visitor-asn": "55836", "cf-connecting-ip": "203.0.113.7"}
_ip = iter(range(10, 250))


def fresh_ip() -> dict:
    return {**EDGE, "cf-connecting-ip": f"203.0.113.{next(_ip)}"}


@pytest.fixture(scope="module")
def client():
    with TestClient(main.app) as c:
        with main.db() as d:
            for pid, name, kind, lat, lon in (PLACE, PLACE2, PLACE3):
                d.execute(
                    f"INSERT OR REPLACE INTO places ({main.PLACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, name, kind, lat, lon, main.quadkey(lat, lon), "{}", "x", int(time.time())),
                )
        yield c


@pytest.fixture(autouse=True)
def no_min_duration(monkeypatch):
    monkeypatch.setattr(main, "MIN_TEST_S", 0)


@pytest.fixture
def fake_osm(monkeypatch):
    """OSM API stub: n900 is a named restaurant next to PLACE; n901 is unnamed."""
    calls = []

    def fake(oid):
        calls.append(oid)
        if oid == "n900":
            return {"tags": {"name": "Brik Oven", "amenity": "restaurant"}, "lat": PLACE[3] + 0.0001, "lon": PLACE[4]}
        if oid == "n901":
            return {"tags": {"building": "yes"}, "lat": PLACE[3], "lon": PLACE[4]}
        return None
    monkeypatch.setattr(main, "osm_element", fake)
    return calls


def start(client, pid, hdr, lat=PLACE[3], lon=PLACE[4], spot=None):
    return client.post(f"/api/places/{pid}/test-start", json={"lat": lat, "lon": lon, "spot": spot}, headers=hdr)


def run_test(client, pid, hdr, lat, lon, down=40.0, network="wifi"):
    s = start(client, pid, hdr, lat, lon)
    if s.status_code != 200:
        return s
    body = {"token": s.json()["token"], "network": network, "down_mbps": down, "up_mbps": 10, "ping_ms": 20,
            "lat": lat, "lon": lon}
    return client.post(f"/api/places/{s.json()['id']}/tests", json=body, headers=hdr)


def count(sql, *a):
    with main.db() as c:
        return c.execute(sql, a).fetchone()[0]


# ------------------------------------------------------------------ config / guards
def test_weak_salt_refused(monkeypatch):
    monkeypatch.setattr(main, "SALT", "isitfast")
    monkeypatch.delenv("ALLOW_WEAK_SALT", raising=False)
    with pytest.raises(RuntimeError):
        main.check_salt()


@pytest.mark.parametrize("bbox,code", [
    ("-90,-180,90,180", 413),            # whole world: rejected before any work
    ("12.9,77.5,13.95,77.6", 413),       # just over 1 degree tall
    ("nan,77.5,13,77.6", 400),
    ("13,77.5,12.9,77.6", 400),          # south > north
    ("12.9,77.5,13", 400),
    ("12.9,77.5,13.0,77.7", 200),
])
def test_bbox_guard(client, bbox, code):
    t0 = time.time()
    r = client.get("/api/places", params={"bbox": bbox})
    assert r.status_code == code, r.text
    assert time.time() - t0 < 1.0


def test_places_bbox_lists_all_places_and_flags_tested(client):
    # Every listed place is a dot on the map; tested ones carry t=1 and become labelled pins.
    r = client.get("/api/places", params={"bbox": "12.9,77.6,12.95,77.65"})
    props = {f["properties"]["id"]: f["properties"] for f in r.json()["features"]}
    assert "n2" in props and "t" not in props["n2"] and r.json()["pending"] == 0
    assert run_test(client, "n2", fresh_ip(), PLACE2[3], PLACE2[4]).status_code == 200
    r = client.get("/api/places", params={"bbox": "12.9,77.6,12.95,77.65"})
    assert next(f for f in r.json()["features"] if f["properties"]["id"] == "n2")["properties"]["t"] == 1


@pytest.mark.parametrize("tags,kind", [
    ({"amenity": "cafe", "name": "x"}, "cafe"),
    ({"amenity": "coworking_space"}, "coworking"),
    ({"office": "coworking"}, "coworking"),
    ({"amenity": "restaurant"}, "restaurant"),
    ({"amenity": "fast_food"}, "restaurant"),
    ({"office": "company"}, "office"),
    ({"building": "apartments"}, "apartments"),
    ({"landuse": "residential", "residential": "apartments"}, "apartments"),
    ({"office": "company", "amenity": "cafe"}, "cafe"),
    ({"shop": "bakery"}, None),
])
def test_osm_kind_of(tags, kind):
    from app import osm
    assert osm.kind_of(tags) == kind


def test_osm_parse_skips_unnamed_and_other_kinds():
    from app import osm
    els = [{"type": "node", "id": 1, "lat": 1, "lon": 2, "tags": {"amenity": "restaurant", "name": "Brik Oven"}},
           {"type": "way", "id": 2, "center": {"lat": 1, "lon": 2}, "tags": {"building": "apartments", "name": "Lakeview"}},
           {"type": "node", "id": 3, "lat": 1, "lon": 2, "tags": {"amenity": "restaurant"}},
           {"type": "node", "id": 4, "lat": 1, "lon": 2, "tags": {"shop": "bakery", "name": "B"}}]
    assert [(p["id"], p["kind"]) for p in osm.parse_elements(els)] == [("n1", "restaurant"), ("w2", "apartments")]


# ------------------------------------------------------------------ tiles
def test_tile_cacheable_and_conditional(client):
    i, j = main.tile_of(PLACE[3], PLACE[4]).split(":")
    r = client.get(f"/api/places/tile/{i}/{j}")
    assert r.status_code == 200
    assert "public" in r.headers["cache-control"] and r.headers["etag"]
    assert "n2" in {f["properties"]["id"] for f in r.json()["features"]}  # tested above
    assert client.get(f"/api/places/tile/{i}/{j}", headers={"if-none-match": r.headers["etag"]}).status_code == 304


def test_tile_out_of_range(client):
    assert client.get("/api/places/tile/99999/0").status_code == 400


def test_new_test_invalidates_tile(client):
    i, j = main.tile_of(PLACE[3], PLACE[4]).split(":")
    before = client.get(f"/api/places/tile/{i}/{j}")
    r = run_test(client, "n1", fresh_ip(), PLACE[3], PLACE[4], down=41.5)
    assert r.status_code == 200, r.text
    after = client.get(f"/api/places/tile/{i}/{j}", headers={"if-none-match": before.headers["etag"]})
    assert after.status_code == 200 and after.headers["etag"] != before.headers["etag"]
    f = next(f for f in after.json()["features"] if f["properties"]["id"] == "n1")
    assert f["properties"]["wifi"] == 41.5


# ------------------------------------------------------------------ test tokens & anti-forgery
def test_result_needs_token(client):
    body = {"network": "wifi", "down_mbps": 40, "up_mbps": 10, "ping_ms": 20, "lat": PLACE[3], "lon": PLACE[4]}
    assert client.post("/api/places/n1/tests", json=body, headers=fresh_ip()).status_code == 422  # missing token
    forged = main.sign_token("n1", "abcd", int(time.time()))[:-4] + "0000"
    r = client.post("/api/places/n1/tests", json={**body, "token": forged}, headers=fresh_ip())
    assert r.status_code == 403


def test_token_is_place_bound_and_single_use(client):
    hdr = fresh_ip()
    s = start(client, "n1", hdr)
    body = {"token": s.json()["token"], "network": "wifi", "down_mbps": 30, "up_mbps": 5, "ping_ms": 20,
            "lat": PLACE[3], "lon": PLACE[4]}
    assert client.post("/api/places/n2/tests", json=body, headers=hdr).status_code == 403  # other place
    assert client.post("/api/places/n1/tests", json=body, headers=hdr).status_code == 200
    assert client.post("/api/places/n1/tests", json=body, headers=hdr).status_code == 403  # replay


def test_too_fast_rejected(client, monkeypatch):
    monkeypatch.setattr(main, "MIN_TEST_S", 30)
    r = run_test(client, "n1", fresh_ip(), PLACE[3], PLACE[4])
    assert r.status_code == 403 and "time" in r.json()["detail"]


def test_requires_edge_asn(client):
    hdr = {"cf-connecting-ip": "198.51.100.200"}
    s = start(client, "n1", hdr)
    body = {"token": s.json()["token"], "network": "wifi", "down_mbps": 40, "up_mbps": 10, "ping_ms": 20,
            "lat": PLACE[3], "lon": PLACE[4], "asn": 55836}
    assert client.post("/api/places/n1/tests", json=body, headers=hdr).status_code == 422  # client ASN not trusted


def test_far_away_rejected_at_start(client):
    assert start(client, "n1", fresh_ip(), lat=PLACE[3] + 0.01).status_code == 422


def test_rate_limits(client):
    hdr = fresh_ip()
    codes = [run_test(client, "n3", hdr, PLACE3[3], PLACE3[4]).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]  # 3 per place per hour from one connection
    hdr = fresh_ip()
    codes = []
    for k in range(11):
        pid, lat, lon = [("n1",) + PLACE[3:], ("n2",) + PLACE2[3:], ("n3",) + PLACE3[3:]][k % 3]
        codes.append(run_test(client, pid, hdr, lat, lon).status_code)
    assert codes.count(200) <= 9 and 429 in codes  # and 10 per hour overall


# ------------------------------------------------------------------ browse is read-only; adopt at test start
def test_preview_osm_place_writes_nothing(client, fake_osm):
    n = count("SELECT COUNT(*) FROM places")
    r = client.get("/api/places/n900")
    assert r.status_code == 200 and r.json()["preview"] and r.json()["name"] == "Brik Oven"
    assert "public" in r.headers["cache-control"]
    assert count("SELECT COUNT(*) FROM places") == n
    assert client.get("/api/places/n901").status_code == 404  # unnamed
    assert client.get("/api/places/x'; drop").status_code == 404


def test_adopt_happens_at_test_start(client, fake_osm):
    r = run_test(client, "n900", fresh_ip(), PLACE[3], PLACE[4])
    assert r.status_code == 200, r.text
    assert count("SELECT COUNT(*) FROM places WHERE id='n900' AND cell='adopted'") == 1
    d = client.get("/api/places/n900?fresh=1").json()
    assert not d["preview"] and d["wifi"]["down_mbps"] == 40.0


def test_named_spot_flow(client):
    spot = {"name": "Unnamed Tower Lobby", "kind": "building", "lat": PLACE[3] + 0.0002, "lon": PLACE[4]}
    pv = client.get("/api/spot", params={"lat": spot["lat"], "lon": spot["lon"], "name": spot["name"]})
    assert pv.status_code == 200 and pv.json()["id"] is None
    s = start(client, "new", fresh_ip(), spot=spot)
    assert s.status_code == 200 and s.json()["id"].startswith("s")
    s2 = start(client, "new", fresh_ip(), spot={**spot, "lat": spot["lat"] + 0.00005})  # 5 m away: same spot
    assert s2.json()["id"] == s.json()["id"]


# ------------------------------------------------------------------ shared state, caches
def test_asn_names_cached_in_sqlite(client, monkeypatch):
    class R:
        status_code = 200

        @staticmethod
        def json():
            return {"Answer": [{"data": '"133661 | IN | apnic | 2015 | NETPLUS-AS Netplus Broadband, IN"'}]}
    calls = []
    monkeypatch.setattr(main.HTTP, "get", lambda *a, **k: calls.append(1) or R())
    assert main.asn_name(133661) == "Netplus Broadband"
    assert main.asn_name(133661) == "Netplus Broadband" and len(calls) == 1
    assert count("SELECT COUNT(*) FROM asn_cache WHERE asn=133661 AND ok=1") == 1


def test_geocode_local_first_then_nominatim_cached(client, monkeypatch):
    class R:
        status_code = 200

        @staticmethod
        def json():
            return [{"display_name": "Koramangala, Bengaluru", "lat": "12.93", "lon": "77.62", "boundingbox": None}]
    calls = []

    def fake_get(url, params=None, **k):
        calls.append(params)
        return R()
    monkeypatch.setattr(main.HTTP, "get", fake_get)
    monkeypatch.setattr(main, "NOMINATIM_GAP_MS", 0)
    out = client.get("/api/geocode", params={"q": "Test Cafe"}).json()
    assert out[0]["place"] == "n1" and calls and calls[0]["countrycodes"] == "in"
    client.get("/api/geocode", params={"q": "test   cafe"})
    assert len(calls) == 1  # normalised key hits the cache


def test_ro_connection_follows_atomic_swap(tmp_path):
    p = str(tmp_path / "ref.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t (v)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    assert main.ro(p).execute("SELECT v FROM t").fetchone()[0] == 1
    q = str(tmp_path / "ref.build")
    c = sqlite3.connect(q)
    c.execute("CREATE TABLE t (v)")
    c.execute("INSERT INTO t VALUES (2)")
    c.commit()
    c.close()
    os.replace(q, p)
    assert main.ro(p).execute("SELECT v FROM t").fetchone()[0] == 2


def test_ipv6_same_64_shares_limit():
    class R:
        def __init__(self, ip):
            self.headers = {"cf-connecting-ip": ip}
            self.client = None
    assert main.ip_key(R("2001:db8:1:2::1")) == main.ip_key(R("2001:db8:1:2:ffff::9"))
    assert main.ip_key(R("2001:db8:1:2::1")) != main.ip_key(R("2001:db8:1:3::1"))


def test_spoofed_xff_ignored():
    class R:
        headers = {"x-forwarded-for": "1.2.3.4, 9.9.9.9"}
        client = None
    assert main.client_ip(R()) == "9.9.9.9"


# ------------------------------------------------------------------ seed: gone places hidden, never deleted
def test_seed_marks_gone_but_keeps_tested(client, monkeypatch):
    import asyncio

    from app import osm, seed
    box = (12.85, 77.55, 13.00, 77.70)  # fully covers the 0.05-degree cell holding n1/n3
    cell = osm.cell_key(int(PLACE[3] // main.CELL), int(PLACE[4] // main.CELL))
    with main.db() as c:
        c.execute("UPDATE places SET cell=? WHERE id IN ('n1','n3')", (cell,))
        c.execute("DELETE FROM tests WHERE place_id='n3'")
        for k in range(2):
            c.execute(f"INSERT OR REPLACE INTO places ({main.PLACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                      (f"n77{k}", "Still here", "cafe", 12.91, 77.61, "q", "{}", cell, 0))
    keep = [{"id": "n1", "name": "Test Cafe", "kind": "cafe", "lat": PLACE[3], "lon": PLACE[4], "tags": {}}]

    async def fake_fetch(*a, **k):
        return keep
    monkeypatch.setattr(osm, "fetch_overpass", fake_fetch)
    asyncio.run(seed.seed_bbox("t", *box))
    with main.db() as c:  # 1 of 4 returned: treated as a partial answer, nothing hidden
        assert c.execute("SELECT COUNT(*) FROM places WHERE gone_at IS NOT NULL").fetchone()[0] == 0
    keep += [{"id": f"n77{k}", "name": "Still here", "kind": "cafe", "lat": 12.91, "lon": 77.61, "tags": {}} for k in range(2)]
    asyncio.run(seed.seed_bbox("t", *box))
    with main.db() as c:
        gone = {r[0] for r in c.execute("SELECT id FROM places WHERE gone_at IS NOT NULL")}
        assert c.execute("SELECT COUNT(*) FROM places WHERE id='n3'").fetchone()[0] == 1  # never deleted
    assert gone == {"n3"}
    ids = {f["properties"]["id"] for f in main.features_in(*box)}
    assert "n3" not in ids and "n1" in ids


# ------------------------------------------------------------------ static
@pytest.mark.parametrize("path,expect", [
    ("/app.js?v=24", "immutable"),
    ("/about.html", "no-cache"),
    ("/", "no-cache"),
    ("/carriers/jio.svg", "max-age=3600"),
])
def test_static_cache_headers(client, path, expect):
    r = client.get(path)
    assert r.status_code == 200
    assert expect in r.headers["cache-control"]


def test_backup_snapshot(client, monkeypatch):
    from app import backup
    monkeypatch.setattr(backup, "DB_PATH", main.DB_PATH)
    monkeypatch.setattr(backup, "OUT", os.path.join(TMP, "backups"))
    monkeypatch.setattr(sys, "argv", ["backup", "--keep", "2"])
    for _ in range(3):
        backup.main()
        time.sleep(1.1)
    snaps = sorted(os.listdir(os.path.join(TMP, "backups")))
    assert len(snaps) == 2
    c = sqlite3.connect(os.path.join(TMP, "backups", snaps[-1]))
    assert c.execute("SELECT COUNT(*) FROM tests").fetchone()[0] >= 1


# ------------------------------------------------------------------ homes & apartments
def test_unnamed_building_adopted_as_named_apartment_with_several_isps(client, fake_osm, monkeypatch):
    """n901 is an unnamed building=yes: the visitor names it, picks 'apartments', and residents on two
    different broadband ISPs test it. The card lists both providers, fastest first."""
    assert client.get("/api/places/n901").status_code == 404  # unnamed: the client asks for a name
    spot = {"name": "Lakeview Residences", "kind": "apartments", "lat": PLACE[3], "lon": PLACE[4]}
    hdr_act = {**fresh_ip(), "x-visitor-asn": "24309"}      # ACT Fibernet (fixed)
    hdr_airtel = {**fresh_ip(), "x-visitor-asn": "24560"}   # Airtel broadband (fixed)
    for hdr, down in ((hdr_act, 180.0), (hdr_airtel, 95.0)):
        s = start(client, "n901", hdr, spot=spot)
        assert s.status_code == 200, s.text
        body = {"token": s.json()["token"], "network": "wifi", "down_mbps": down, "up_mbps": 50, "ping_ms": 5,
                "lat": PLACE[3], "lon": PLACE[4]}
        assert client.post("/api/places/n901/tests", json=body, headers=hdr).status_code == 200
    d = client.get("/api/places/n901?fresh=1").json()
    assert d["name"] == "Lakeview Residences" and d["kind"] == "apartments" and d["residential"]
    assert [x["here"]["down_mbps"] for x in d["wifi_isps"]] == [180.0, 95.0]
    assert len({x["isp"] for x in d["wifi_isps"]}) == 2
    # Apartments get the same 50 m test range as a cafe: ~100 m away is refused.
    far = start(client, "n901", fresh_ip(), PLACE[3] + 0.0009, PLACE[4])
    assert far.status_code == 422 and "away" in far.json()["detail"]


def test_cafe_not_residential(client):
    assert client.get("/api/places/n1?fresh=2").json()["residential"] is False


def test_same_named_spot_on_a_big_complex_is_reused(client):
    base = {"name": "Prestige Lakeside Habitat", "kind": "apartments", "lat": PLACE[3] + 0.003, "lon": PLACE[4]}
    a = start(client, "new", fresh_ip(), lat=base["lat"], lon=base["lon"], spot=base)
    far = {**base, "name": "prestige lakeside  habitat", "lat": base["lat"] + 0.00035}  # ~40 m away, same name
    b = start(client, "new", fresh_ip(), lat=far["lat"], lon=far["lon"], spot=far)
    other = {**base, "name": "Some Other Block", "lat": base["lat"] + 0.00035}
    c = start(client, "new", fresh_ip(), lat=other["lat"], lon=other["lon"], spot=other)
    assert a.status_code == b.status_code == c.status_code == 200
    assert a.json()["id"] == b.json()["id"] != c.json()["id"]


# ------------------------------------------------------------------ Google places (only place_id is stored)
GID = "gChIJ_test_place_0123456789"


def test_google_place_preview_writes_nothing(client):
    n = count("SELECT COUNT(*) FROM places")
    assert client.get(f"/api/places/{GID}").status_code == 404  # untested + no position: nothing to show
    r = client.get(f"/api/places/{GID}", params={"lat": PLACE[3], "lon": PLACE[4]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["preview"] and d["google"] and d["name"] is None and d["id"] == GID
    assert count("SELECT COUNT(*) FROM places") == n
    assert client.get("/api/places/gshort").status_code == 404
    assert client.get("/api/places/gbad$chars_0123456789").status_code == 404


def test_google_place_stored_at_tester_position_without_google_data(client):
    hdr = fresh_ip()
    me = (PLACE[3] + 0.0002, PLACE[4])  # ~22 m from where Google puts it
    far = start(client, GID, hdr, lat=PLACE[3] + 0.01, lon=PLACE[4], spot={"lat": PLACE[3], "lon": PLACE[4]})
    assert far.status_code == 422  # must be at the place
    assert start(client, GID, fresh_ip(), lat=me[0], lon=me[1]).status_code == 422  # no position given
    s = start(client, GID, hdr, lat=me[0], lon=me[1], spot={"lat": PLACE[3], "lon": PLACE[4]})
    assert s.status_code == 200, s.text
    assert s.json()["id"] == GID
    with main.db() as c:
        row = c.execute("SELECT name, kind, lat, lon, tags FROM places WHERE id=?", (GID,)).fetchone()
    assert row["name"] is None and row["kind"] == "place" and row["tags"] == "{}"
    assert (row["lat"], row["lon"]) == me  # our measurement, not Google's coordinates
    body = {"token": s.json()["token"], "network": "wifi", "down_mbps": 55, "up_mbps": 12, "ping_ms": 20, "lat": me[0], "lon": me[1]}
    assert client.post(f"/api/places/{GID}/tests", json=body, headers=hdr).status_code == 200
    d = client.get(f"/api/places/{GID}?fresh=1").json()
    assert not d["preview"] and d["google"] and d["name"] is None and d["wifi"]["down_mbps"] == 55.0
    i, j = main.tile_of(*me).split(":")
    f = next(f for f in client.get(f"/api/places/tile/{i}/{j}?fresh=1").json()["features"] if f["properties"]["id"] == GID)
    assert f["properties"]["name"] is None


def test_index_injects_maps_key(client, monkeypatch):
    monkeypatch.setattr(main, "GOOGLE_MAPS_KEY", "AIza-test_KEY\"><script>")
    monkeypatch.setattr(main, "GOOGLE_MAP_ID", "abc123")
    main._index_html.cache_clear()
    try:
        for path in ("/", "/index.html"):
            html = client.get(path).text
            assert 'key: "AIza-test_KEYscript"' in html and 'data-map-id="abc123"' in html
            assert "__GOOGLE_MAPS_KEY__" not in html and "<script>\"" not in html
    finally:
        main._index_html.cache_clear()

"""isitfast.cloud — API + static site.

Data:
  * places      — cafes & coworking spaces from OpenStreetMap (Overpass), cached per grid cell
  * ookla.db    — Ookla Open Data quarterly zoom-16 tiles (mobile + fixed), see ookla_ingest.py
  * tests       — community speed tests run in-browser at a place (wifi or cellular, per carrier)
  * carriers.db — M-Lab city x carrier averages (mlab_ingest.py)
"""
import asyncio
import collections
import functools
import hashlib
import hmac
import html as htmlmod
import io
import ipaddress
import json
import math
import re
import secrets
import threading
import os
import sqlite3
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, metrics
from .geo import haversine_m, neighbour_quadkeys, quadkey

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
OOKLA_DB = os.path.join(DATA_DIR, "ookla.db")
WEB_DIR = os.environ.get("WEB_DIR", os.path.join(os.path.dirname(__file__), "..", "web"))
UA = "isitfast.cloud/1.0 (+https://isitfast.cloud; cafe internet speed map)"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
CELL = 0.05            # grid cell size in degrees for the OSM cache (~5.5 km); see seed.py
CELL_TTL = 14 * 86400  # refresh OSM data every two weeks
TILE = 0.25            # place snapshot tiles (~28 km); the map fetches the few that cover the view
MAX_BBOX_DEG = 1.0     # /api/places bbox: at most 1 degree on each side
MAX_TEST_DISTANCE_M = 50  # every place, homes and apartments included; must match TEST_RADIUS_M in web/app.js
# Laptops locate by WiFi/IP and often report ±100-150 m. A test counts when the place is within 50 m of the
# reported position plus its accuracy (capped here); tests with accuracy above PRECISE_ACC_M are labelled
# approximate. Must match MAX_ACC_SLACK_M / PRECISE_ACC_M in web/app.js.
MAX_ACC_SLACK_M = 150
PRECISE_ACC_M = 50
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://isitfast.cloud").rstrip("/")
# Every domain the site answers on is its own equal site (no cross-domain canonical): links, QR codes and
# link-preview URLs use the domain the visitor came in on. Anything else falls back to PUBLIC_URL.
SITE_HOSTS = {h.strip().lower() for h in os.environ.get(
    "SITE_HOSTS", "isitfast.cloud,www.isitfast.cloud,isitfast.space,www.isitfast.space").split(",") if h.strip()}


def site_base(request: Request | None) -> str:
    host = (request.headers.get("host", "") if request else "").split(":")[0].strip().lower()
    return f"https://{host}" if host in SITE_HOSTS else PUBLIC_URL
SALT = os.environ.get("IP_SALT", "")
WEAK_SALTS = {"", "isitfast", "change-me"}

# ASN -> carrier/ISP label (crowd tests). Unknown ASNs are shown as "AS<n>".
ASN_NAMES = {
    55836: "Jio",
    45609: "Airtel", 9498: "Airtel", 24560: "Airtel",
    38266: "Vi", 55410: "Vi", 45271: "Vi",
    9829: "BSNL", 17488: "Hathway", 24309: "ACT Fibernet", 4755: "Tata Communications",
    21928: "T-Mobile", 22394: "Verizon", 20057: "AT&T",
}
# Fixed-broadband ASNs: a "mobile data" test from these is re-labelled as WiFi.
FIXED_ASNS = {24560, 24309, 18209, 17488, 133982, 134674, 17665}

PLACE_COLS = "id, name, kind, lat, lon, qk, tags, cell, updated_at"
HTTP = httpx.Client(headers={"User-Agent": UA}, timeout=httpx.Timeout(8.0, connect=3.0), follow_redirects=True)


def db(autocommit: bool = False) -> sqlite3.Connection:
    """A connection to app.db. Use as `with db() as c:` (commits on exit). autocommit=True gives manual
    transaction control for BEGIN IMMEDIATE sections; the caller must close it."""
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None if autocommit else "")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


_tl = threading.local()


def ro(path: str) -> sqlite3.Connection | None:
    """Per-thread read-only connection to a reference DB (ookla.db, carriers.db). Reopened automatically when
    an ingest swaps the file in (new inode), so long-lived connections never pin an old snapshot."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    cache = getattr(_tl, "ro", None)
    if cache is None:
        cache = _tl.ro = {}
    stamp = (st.st_ino, st.st_mtime_ns)
    hit = cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    if hit:
        hit[1].close()
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    cache[path] = (stamp, c)
    return c


def ookla_db() -> sqlite3.Connection | None:
    return ro(OOKLA_DB)


def file_stamp(path: str) -> tuple:
    try:
        st = os.stat(path)
        return (st.st_ino, st.st_mtime_ns)
    except OSError:
        return (0, 0)


# ---------------------------------------------------------------- counters (see /api/metrics)
_counts: collections.Counter = collections.Counter()
_timings: dict[str, list[float]] = collections.defaultdict(list)


def metric(name: str, label: str = "", n: int = 1) -> None:
    _counts[(name, label)] += n


def timing(name: str, ms: float) -> None:
    buf = _timings[name]
    buf.append(ms)
    if len(buf) > 2000:
        del buf[:1000]


def outbound(name: str, fn):
    """Run an outbound HTTP call, counting results and latency per upstream."""
    t0 = time.perf_counter()
    try:
        r = fn()
        metric("outbound", f"{name}:{r.status_code}")
        return r
    except Exception:
        metric("outbound", f"{name}:error")
        raise
    finally:
        timing(f"outbound:{name}", (time.perf_counter() - t0) * 1000)


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as c:
        c.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS places (
              id TEXT PRIMARY KEY, name TEXT, kind TEXT, lat REAL, lon REAL,
              qk TEXT, tags TEXT, cell TEXT, updated_at INTEGER);
            CREATE INDEX IF NOT EXISTS places_latlon ON places(lat, lon);
            CREATE TABLE IF NOT EXISTS cells (cell TEXT PRIMARY KEY, fetched_at INTEGER, n INTEGER);
            CREATE TABLE IF NOT EXISTS tests (
              id INTEGER PRIMARY KEY AUTOINCREMENT, place_id TEXT NOT NULL,
              network TEXT NOT NULL, asn INTEGER, carrier TEXT, colo TEXT,
              down_mbps REAL, up_mbps REAL, ping_ms REAL, jitter_ms REAL,
              dist_m REAL, ip_hash TEXT, created_at INTEGER);
            CREATE INDEX IF NOT EXISTS tests_place ON tests(place_id, created_at);
            CREATE INDEX IF NOT EXISTS tests_ip ON tests(ip_hash, created_at);
            CREATE TABLE IF NOT EXISTS geocode_cache (q TEXT PRIMARY KEY, body TEXT, at INTEGER);
            CREATE TABLE IF NOT EXISTS tile_rev (tile TEXT PRIMARY KEY, rev INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (name TEXT PRIMARY KEY, at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS metrics (minute INTEGER, worker INTEGER, name TEXT, label TEXT, n INTEGER,
              p50 REAL, p95 REAL, mx REAL);
            CREATE INDEX IF NOT EXISTS metrics_minute ON metrics(minute);
            CREATE TABLE IF NOT EXISTS asn_cache (asn INTEGER PRIMARY KEY, name TEXT, ok INTEGER, at INTEGER);
            CREATE TABLE IF NOT EXISTS test_tokens (
              nonce TEXT PRIMARY KEY, place_id TEXT NOT NULL, ip_hash TEXT, issued_at INTEGER NOT NULL, used_at INTEGER);
            CREATE INDEX IF NOT EXISTS test_tokens_ip ON test_tokens(ip_hash, issued_at);
            """
        )
        c.executescript(analytics.SCHEMA)
        tcols = {r["name"] for r in c.execute("PRAGMA table_info(tests)")}
        if "verified" not in tcols:  # 1 = result bounded by signed receipts from our speed Worker
            try:
                c.execute("ALTER TABLE tests ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        if "acc_m" not in tcols:  # the device's reported location accuracy at submit (no coordinates kept)
            try:
                c.execute("ALTER TABLE tests ADD COLUMN acc_m INTEGER")
            except sqlite3.OperationalError:
                pass
        cols = {r["name"] for r in c.execute("PRAGMA table_info(places)")}
        if "gone_at" not in cols:  # set when a place disappears from OSM; it stays if it has tests
            try:
                c.execute("ALTER TABLE places ADD COLUMN gone_at INTEGER")
            except sqlite3.OperationalError:  # another worker added it first
                pass


def check_salt() -> None:
    """The IP hash is only private if the salt is secret: refuse to run with a known/default one."""
    if SALT in WEAK_SALTS and os.environ.get("ALLOW_WEAK_SALT") != "1":
        raise RuntimeError("IP_SALT must be set to a long random secret (set ALLOW_WEAK_SALT=1 only for local dev)")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_salt()
    init_db()
    task = asyncio.create_task(metrics.monitor(db, _counts, _timings, timing))
    yield
    task.cancel()
    metrics.flush(db, _counts, _timings)


app = FastAPI(title="isitfast.cloud", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------- Ookla lookups
def ookla_meta() -> dict:
    o = ookla_db()
    if not o:
        return {}
    with o:
        return {r["kind"]: dict(r) for r in o.execute("SELECT * FROM meta")}


def _row(r) -> dict:
    return {
        "down_mbps": round(r["d_kbps"] / 1000, 1),
        "up_mbps": round(r["u_kbps"] / 1000, 1),
        "latency_ms": r["lat_ms"],
        "tests": r["tests"],
        "devices": r["devices"],
    }


def ookla_lookup(lat: float, lon: float) -> dict:
    """Exact zoom-16 tile plus a tests-weighted 3x3 neighbourhood average (~1.8 km square)."""
    o = ookla_db()
    out = {}
    if not o:
        return out
    meta = ookla_meta()
    qk = quadkey(lat, lon)
    keys = neighbour_quadkeys(lat, lon, 1)
    with o:
        for kind in ("mobile", "fixed"):
            tile = o.execute("SELECT * FROM tiles WHERE kind=? AND quadkey=?", (kind, qk)).fetchone()
            rows = o.execute(
                f"SELECT * FROM tiles WHERE kind=? AND quadkey IN ({','.join('?' * len(keys))})", (kind, *keys)
            ).fetchall()
            area = None
            if rows:
                t = sum(r["tests"] for r in rows)
                area = {
                    "down_mbps": round(sum(r["d_kbps"] * r["tests"] for r in rows) / t / 1000, 1),
                    "up_mbps": round(sum(r["u_kbps"] * r["tests"] for r in rows) / t / 1000, 1),
                    "latency_ms": round(sum(r["lat_ms"] * r["tests"] for r in rows) / t),
                    "tests": t,
                    "devices": sum(r["devices"] for r in rows),
                    "tiles": len(rows),
                }
            m = meta.get(kind, {})
            out[kind] = {
                "tile": _row(tile) if tile else None,
                "area": area,
                "period": f"{m['year']} Q{m['quarter']}" if m else None,
                "published": m.get("published"),
                "source": "Speedtest® by Ookla® Global Fixed and Mobile Network Performance Maps",
            }
    return out


# ---------------------------------------------------------------- per-carrier (M-Lab city x ASN)
CARRIERS_DB = os.path.join(DATA_DIR, "carriers.db")
CARRIER_ORDER = ["Jio", "Airtel", "Vi"]


def city_carrier_lookup(lat: float, lon: float) -> dict:
    """Nearest M-Lab city with enough tests, per carrier (IP-geolocated, so city-level at best)."""
    c = ro(CARRIERS_DB)
    if not c:
        return {}
    meta = {r["k"]: r["v"] for r in c.execute("SELECT k, v FROM meta")}
    rows = c.execute(
        "SELECT * FROM city_carrier WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (lat - 0.8, lat + 0.8, lon - 0.8, lon + 0.8),
    ).fetchall()
    out = {}
    for carrier in CARRIER_ORDER:
        cands = [(haversine_m(lat, lon, r["lat"], r["lon"]), r) for r in rows if r["carrier"] == carrier]
        pick = None
        for radius, min_tests in ((30_000, 500), (80_000, 200)):
            ok = [x for x in cands if x[0] <= radius and x[1]["tests"] >= min_tests]
            if ok:
                pick = min(ok, key=lambda x: x[0])
                break
        if pick:
            d, r = pick
            out[carrier] = {
                "down_mbps": r["down_mbps"], "up_mbps": r["up_mbps"], "latency_ms": r["latency_ms"],
                "tests": r["tests"], "city": r["city"], "distance_km": round(d / 1000, 1),
                "period_start": meta.get("period_start"), "period_end_month": meta.get("period_end_month"),
            }
    return out


# ---------------------------------------------------------------- optional private per-carrier overlay
# app/local_carrier.py is git-ignored and may be absent. When present, its lookup(lat, lon) returns
# {carrier: {down_mbps, up_mbps, tests, tip, ...}} and takes priority over the M-Lab city average.
try:
    from .local_carrier import lookup as local_carrier_lookup
except ModuleNotFoundError as ex:
    if not (ex.name or "").endswith("local_carrier"):
        raise

    def local_carrier_lookup(lat: float, lon: float) -> dict:
        return {}


@functools.lru_cache(maxsize=8192)
def _baseline(lat: float, lon: float, _stamp: tuple) -> dict:
    return {"ookla": ookla_lookup(lat, lon), "city": city_carrier_lookup(lat, lon), "local": local_carrier_lookup(lat, lon)}


def baseline(lat: float, lon: float) -> dict:
    """Published figures for a location (Ookla tiles, M-Lab city, optional overlay). They only change when an
    ingest swaps a file in, so they are computed once per location per data version. Treat as read-only."""
    stamp = (file_stamp(OOKLA_DB), file_stamp(CARRIERS_DB), file_stamp(os.path.join(DATA_DIR, "anubhav.db")))
    return _baseline(round(lat, 6), round(lon, 6), stamp)


def mobile_down_for(qks: list[str]) -> dict[str, float]:
    o = ookla_db()
    if not o or not qks:
        return {}
    res = {}
    with o:
        uniq = list(set(qks))
        for i in range(0, len(uniq), 500):
            chunk = uniq[i : i + 500]
            for r in o.execute(
                f"SELECT quadkey, d_kbps FROM tiles WHERE kind='mobile' AND quadkey IN ({','.join('?' * len(chunk))})", chunk
            ):
                res[r["quadkey"]] = round(r["d_kbps"] / 1000, 1)
    return res


# ---------------------------------------------------------------- places
from . import basemap, osm  # noqa: E402


def tile_of(lat: float, lon: float) -> str:
    return f"{math.floor(lat / TILE)}:{math.floor(lon / TILE)}"


def bump_tiles(c: sqlite3.Connection, tiles: set[str] | list[str]) -> None:
    """Invalidate place snapshot tiles after a write (new test, refreshed OSM data)."""
    now_ns = time.time_ns()
    c.executemany(
        "INSERT INTO tile_rev VALUES (?, ?) ON CONFLICT(tile) DO UPDATE SET rev = excluded.rev",
        [(t, now_ns) for t in tiles],
    )


def wifi_summary(c: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not ids:
        return out
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        rows = c.execute(
            f"SELECT place_id, down_mbps FROM tests WHERE network='wifi' AND place_id IN ({','.join('?' * len(chunk))}) "
            "ORDER BY created_at",
            chunk,
        ).fetchall()
        for r in rows:  # ascending, so the last write per place is its latest test
            n = out.get(r["place_id"], {}).get("n", 0) + 1
            out[r["place_id"]] = {"latest_down": round(r["down_mbps"], 1), "n": n}
    return out


def features_in(s: float, w: float, n: float, e: float, limit: int = 30000) -> list[dict]:
    """Every listed place in the box (cafes, coworking, restaurants, offices, apartments from OSM, plus any
    tested place of any kind). Places that dropped out of OSM stay only if someone tested them. `t` marks
    places with at least one test; the map draws those as labelled pins and the rest as small dots."""
    with db() as c:
        rows = c.execute(
            "SELECT id, name, kind, lat, lon, qk FROM places WHERE lat >= ? AND lat < ? AND lon >= ? AND lon < ? "
            "AND (gone_at IS NULL OR id IN (SELECT place_id FROM tests)) LIMIT ?",
            (s, n, w, e, limit),
        ).fetchall()
        ids = [r["id"] for r in rows]
        wifi = wifi_summary(c, ids)
        tested: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            tested |= {r[0] for r in c.execute(
                f"SELECT DISTINCT place_id FROM tests WHERE place_id IN ({','.join('?' * len(chunk))})", chunk)}
    mob = mobile_down_for([r["qk"] for r in rows])
    feats = []
    for r in rows:
        props = {"id": r["id"], "name": r["name"], "kind": r["kind"], "mobile": mob.get(r["qk"]),
                 "wifi": wifi.get(r["id"], {}).get("latest_down")}
        if r["id"] in tested:
            props["t"] = 1
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(r["lon"], 5), round(r["lat"], 5)]},
            "properties": props,
        })
    return feats


def parse_bbox(bbox: str) -> tuple[float, float, float, float]:
    """Validate before doing any work: finite numbers, ordered, within the world, at most MAX_BBOX_DEG wide."""
    try:
        s, w, n, e = (float(x) for x in bbox.split(","))
    except ValueError:
        raise HTTPException(400, "bbox must be south,west,north,east")
    if not all(math.isfinite(v) for v in (s, w, n, e)) or not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        raise HTTPException(400, "bbox must be south,west,north,east with south<north and west<east")
    if n - s > MAX_BBOX_DEG or e - w > MAX_BBOX_DEG:
        raise HTTPException(413, "Area too large — zoom in")
    return s, w, n, e


@app.get("/api/places")
def places(bbox: str = Query(..., description="south,west,north,east (max 1 degree each way)")):
    """Places in a bounding box, straight from the local cache (OSM data is refreshed by a background job,
    never during a request). The map itself uses the cacheable /api/places/tile/{i}/{j} snapshots."""
    s, w, n, e = parse_bbox(bbox)
    return {"type": "FeatureCollection", "features": features_in(s, w, n, e, limit=3000), "pending": 0}


_tile_cache: dict[str, tuple[str, bytes]] = {}  # tile -> (etag, gzip-able JSON body)
TILE_CACHE_MAX = 512


def _ookla_stamp() -> int:
    try:
        return int(os.path.getmtime(OOKLA_DB))
    except OSError:
        return 0


# Bump when what a tile contains changes (not just its data), so cached copies and 304s don't outlive it.
# g1: only tested places (Google basemap switch). g2: every listed place again, `t` flags tested ones.
TILE_FORMAT = "g2"


@app.get("/api/places/tile/{i}/{j}")
def places_tile(i: int, j: int, req: Request):
    """All places in one TILE x TILE degree cell as GeoJSON. Identical for every visitor, so browsers and the
    CDN can cache it; the ETag changes whenever a test is added in the tile or the data is refreshed."""
    if not (-360 <= i < 360 and -720 <= j < 720):
        raise HTTPException(400, "tile out of range")
    key = f"{i}:{j}"
    with db() as c:
        r = c.execute("SELECT rev FROM tile_rev WHERE tile=?", (key,)).fetchone()
    etag = f'W/"{TILE_FORMAT}-{key}-{r["rev"] if r else 0}-{_ookla_stamp()}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=60, stale-while-revalidate=600"}
    if req.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    hit = _tile_cache.get(key)
    if not hit or hit[0] != etag:
        feats = features_in(i * TILE, j * TILE, (i + 1) * TILE, (j + 1) * TILE)
        body = json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")).encode()
        if len(_tile_cache) >= TILE_CACHE_MAX:
            _tile_cache.pop(next(iter(_tile_cache)))
        hit = _tile_cache[key] = (etag, body)
    return Response(hit[1], media_type="application/json", headers=headers)


def _iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@app.get("/api/places/{pid}")
def place_detail(pid: str, req: Request, lat: float | None = Query(default=None, ge=-90, le=90),
                 lon: float | None = Query(default=None, ge=-180, le=180)):
    """Place card data. Listed places come from the cache; any other OSM element is previewed read-only
    (nothing is stored until someone actually starts a speed test there). An untested Google place is
    previewed at the lat/lon the client passes; its name/details come from Google in the browser."""
    if not PID_RE.match(pid):
        raise HTTPException(404, "unknown place")
    with db() as c:
        p = c.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
        tests = c.execute(
            "SELECT network, carrier, asn, down_mbps, up_mbps, ping_ms, jitter_ms, created_at, acc_m FROM tests "
            "WHERE place_id=? ORDER BY created_at DESC LIMIT 200",
            (pid,),
        ).fetchall() if p else []
    if p:
        doc = {"id": p["id"], "name": p["name"], "kind": p["kind"], "lat": p["lat"], "lon": p["lon"],
               "tags": json.loads(p["tags"] or "{}"), "adopted": p["cell"] == ADOPTED, "preview": False}
    elif OSM_ID_RE.match(pid):
        el = osm_element(pid)
        if not el or el["lat"] is None:
            raise HTTPException(404, "unknown place")
        tags = el["tags"]
        name = tags.get("name") or tags.get("name:en") or tags.get("brand")
        if not name:
            raise HTTPException(404, "unnamed place")
        doc = {"id": pid, "name": name[:80], "kind": kind_from_tags(tags) or "place", "lat": el["lat"], "lon": el["lon"],
               "tags": osm.public_tags(tags), "adopted": True, "preview": True}
    elif G_ID_RE.match(pid) and lat is not None and lon is not None:
        doc = {"id": pid, "name": None, "kind": "place", "lat": lat, "lon": lon, "tags": {}, "adopted": True, "preview": True}
    else:
        raise HTTPException(404, "unknown place")
    body = place_doc(doc, tests)
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=30, stale-while-revalidate=120"})


@app.get("/api/spot")
def spot_preview(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
                 name: str = Query(min_length=1, max_length=80), kind: str | None = Query(default=None, pattern=r"^[a-z_]{1,32}$")):
    """Preview for a place a visitor named themselves (an unnamed building). Not stored until a test starts."""
    doc = {"id": None, "name": name.strip()[:80], "kind": kind or "place", "lat": lat, "lon": lon, "tags": {},
           "adopted": True, "preview": True}
    return place_doc(doc, [])


def place_doc(p: dict, tests: list) -> dict:
    wifi = [t for t in tests if t["network"] == "wifi"]
    cell = [t for t in tests if t["network"] == "cellular"]

    def agg(rows):
        # Show the most recent on-site test (rows are newest first), not a median: networks change,
        # and the latest measurement is what a visitor will get. Published city/tile figures are only
        # a fallback when nobody has tested here.
        if not rows:
            return None
        last = rows[0]
        return {
            "down_mbps": round(last["down_mbps"], 1),
            "up_mbps": round(last["up_mbps"], 1),
            "ping_ms": round(last["ping_ms"], 1),
            "tests": len(rows),
            "latest": _iso(last["created_at"]),
            "isp": last["carrier"],
        }

    carriers: dict[str, list] = {}
    for t in cell:
        carriers.setdefault(t["carrier"], []).append(t)
    base = baseline(p["lat"], p["lon"])
    city, local = base["city"], base["local"]
    merged = []
    for name in CARRIER_ORDER + [k for k in carriers if k not in CARRIER_ORDER]:
        here = agg(carriers.get(name, []))
        # Fallback figure: the optional overlay when it has one, else the M-Lab city average.
        pub = local.get(name) or ({**city[name], "source": "mlab"} if name in city else None)
        if not here and not pub:
            continue
        merged.append({"carrier": name, "source": "here" if here else "city", "here": here, "city": pub})
    # WiFi grouped by provider: an apartment block (or a mall, a coworking floor) can have several ISPs.
    # Ordered fastest-latest first; "wifi" stays the latest test overall (map dot, older clients).
    by_isp: dict[str, list] = {}
    for t in wifi:
        by_isp.setdefault(t["carrier"] or "Unknown", []).append(t)
    wifi_isps = sorted(({"isp": k, "here": agg(v)} for k, v in by_isp.items()), key=lambda x: -x["here"]["down_mbps"])
    return {
        **p,
        "residential": p.get("kind") in RESIDENTIAL_KINDS,
        "osm_url": osm_url(p["id"]) if p["id"] else None,
        "google": bool(p["id"] and G_ID_RE.match(p["id"])),
        "wifi": agg(wifi),
        "wifi_isps": wifi_isps,
        "carriers": merged,
        "ookla": base["ookla"],
        "recent_tests": [
            {"network": t["network"], "carrier": t["carrier"], "down_mbps": t["down_mbps"], "up_mbps": t["up_mbps"],
             "ping_ms": t["ping_ms"], "at": _iso(t["created_at"]),
             "approx": bool(t["acc_m"] and t["acc_m"] > PRECISE_ACC_M)}
            for t in tests[:15]
        ],
    }


def osm_url(pid: str) -> str | None:
    t = {"n": "node", "w": "way", "r": "relation"}.get(pid[:1])
    return f"https://www.openstreetmap.org/{t}/{pid[1:]}" if t and pid[1:].isdigit() else None


# ---------------------------------------------------------------- any place is testable
# Any POI on the basemap (restaurant, shop, office, hotel...) or any building can be speed-tested, not
# just OSM cafes/coworking. Browsing one is read-only; the place row is created when a visitor who is
# actually there starts a test (POST /api/places/{pid}/test-start).
ADOPTED = "adopted"
RESIDENTIAL_KINDS = {"apartments", "apartment", "residential", "house", "detached", "semidetached_house", "terrace",
                     "bungalow", "dormitory", "home", "villa"}
ADOPT_DEDUPE_M = 15
ADOPT_SAME_NAME_M = 50  # same name within the 50 m test range = the same place (e.g. two towers of one complex)


def _norm_name(n: str) -> str:
    return "".join(ch for ch in (n or "").lower() if ch.isalnum())
KIND_KEYS = ("amenity", "shop", "office", "craft", "tourism", "leisure", "healthcare", "building")
# Google place: "g" + Google place_id. Google Maps Platform terms allow storing only the place_id, so these rows
# keep no name or Google coordinates: name/kind/address are fetched live in the browser, and lat/lon is the
# position of the first visitor who tested there (our own measurement).
G_ID_RE = re.compile(r"^g[A-Za-z0-9_-]{10,300}$")
PID_RE = re.compile(r"^([nwr]\d{1,12}|s[0-9a-f]{11}|g[A-Za-z0-9_-]{10,300})$")
OSM_ID_RE = re.compile(r"^[nwr]\d{1,12}$")
_osm_cache: dict[str, tuple[float, dict | None]] = {}
OSM_CACHE_TTL = 86400
OSM_CACHE_MAX = 5000


def osm_element(oid: str) -> dict | None:
    """{name, lat, lon, tags} for an OSM element via the main API (~0.3 s), memoised per worker for a day."""
    hit = _osm_cache.get(oid)
    if hit and hit[0] > time.time() - OSM_CACHE_TTL:
        return hit[1]
    t, num = {"n": "node", "w": "way", "r": "relation"}[oid[0]], oid[1:]
    url = f"https://api.openstreetmap.org/api/0.6/{t}/{num}{'/full' if t == 'way' else ''}.json"
    res = None
    try:
        r = outbound("osm_api", lambda: HTTP.get(url, timeout=6))
        els = r.json().get("elements", []) if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None  # transient: don't cache
    me = next((e for e in els if e.get("type") == t and str(e.get("id")) == num), None)
    if me:
        if t == "node":
            lat, lon = me.get("lat"), me.get("lon")
        else:  # way: centroid of its nodes; relation: no cheap geometry
            pts = [(e["lat"], e["lon"]) for e in els if e.get("type") == "node" and "lat" in e]
            lat, lon = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) if pts else (None, None)
        res = {"tags": me.get("tags") or {}, "lat": lat, "lon": lon}
    if len(_osm_cache) >= OSM_CACHE_MAX:
        _osm_cache.pop(next(iter(_osm_cache)))
    _osm_cache[oid] = (time.time(), res)
    return res


def kind_from_tags(tags: dict) -> str | None:
    if tags.get("amenity") == "coworking_space" or tags.get("office") == "coworking":
        return "coworking"
    for k in KIND_KEYS:
        v = tags.get(k)
        if v and v not in ("yes", "no"):
            return "".join(ch for ch in v.lower() if ch.isalnum() or ch == "_")[:32] or None
        if v == "yes":
            return {"building": "building", "shop": "shop", "office": "office"}.get(k)
    return None


def adopt(c: sqlite3.Connection, pid: str, spot: "SpotIn | None", at: tuple[float, float] | None = None) -> sqlite3.Row:
    """Create the place row for an OSM element, a Google place or a visitor-named spot; returns the stored row.
    `at` is the tester's own position (used as a Google place's location)."""
    now = int(time.time())
    if G_ID_RE.match(pid):
        if not spot or not at:
            raise HTTPException(422, "Couldn't locate this place — reopen it and try again.")
        row = (pid, None, "place", at[0], at[1], quadkey(at[0], at[1]), "{}", ADOPTED, now)
    elif OSM_ID_RE.match(pid):
        el = osm_element(pid)
        if not el or el["lat"] is None:
            raise HTTPException(404, "unknown place")
        tags = el["tags"]
        name = tags.get("name") or tags.get("name:en") or tags.get("brand") or (spot.name if spot else "")
        if not name:
            raise HTTPException(422, "Give this place a name first.")
        kind = kind_from_tags(tags)
        if spot and spot.kind and kind in (None, "building", "place", "yes"):
            kind = spot.kind  # e.g. an unnamed building=yes the visitor called an apartment block
        row = (pid, name[:80], kind or "place", el["lat"], el["lon"], quadkey(el["lat"], el["lon"]),
               json.dumps(osm.public_tags(tags)), ADOPTED, now)
    else:
        if not spot or not (spot.name or "").strip():
            raise HTTPException(422, "Give this place a name first.")
        near = c.execute(
            "SELECT * FROM places WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND id LIKE 's%'",
            (spot.lat - 0.0015, spot.lat + 0.0015, spot.lon - 0.0015, spot.lon + 0.0015),
        ).fetchall()
        # Reuse a visitor-named spot instead of duplicating it: the same name anywhere on a big complex
        # first, else whatever is right next to it (typos, "Tower B" vs "Lakeview").
        want = _norm_name(spot.name)
        dist = sorted((haversine_m(spot.lat, spot.lon, r["lat"], r["lon"]), r) for r in near)
        for d, r in dist:
            if d <= ADOPT_SAME_NAME_M and _norm_name(r["name"]) == want:
                return r
        for d, r in dist:
            if d <= ADOPT_DEDUPE_M:
                return r
        pid = "s" + hashlib.sha1(f"{spot.lat:.5f},{spot.lon:.5f}".encode()).hexdigest()[:11]
        row = (pid, spot.name.strip()[:80], spot.kind or "place", spot.lat, spot.lon, quadkey(spot.lat, spot.lon),
               "{}", ADOPTED, now)
    c.execute(f"INSERT OR IGNORE INTO places ({PLACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)", row)
    return c.execute("SELECT * FROM places WHERE id=?", (row[0],)).fetchone()


# ---------------------------------------------------------------- speed tests
# A test is a two-step exchange: test-start (at the place, rate limited) issues a short-lived single-use
# token; the result is only accepted with that token, for that place, after a plausible test duration.
RECEIPT_KEY = os.environ.get("RECEIPT_KEY", "")
RECEIPT_SLACK = 2.0      # a trimmed-mean result can exceed the whole-window average (warm-up included)
MIN_TEST_S = 3          # a capped test on a gigabit link finishes in ~3 s; receipts (phase 3) prove the bytes
TOKEN_TTL_S = 15 * 60
# Per connection (salted IP hash). A busy coworking space puts dozens of people behind one NAT address, so
# these are sized for a venue's worth of honest testers, not one person.
STARTS_PER_HOUR = 60
TESTS_PER_HOUR = 30
TESTS_PER_PLACE_PER_HOUR = 20


class SpotIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)  # None for Google places (never sent)
    kind: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class StartIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    spot: SpotIn | None = None  # pid "new": a place the visitor named; Google pid: where the place is (not stored)


class TestIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    receipts: list[str] | None = Field(default=None, max_length=400)  # from speed.isitfast.cloud
    network: str = Field(pattern="^(wifi|cellular)$")
    down_mbps: float = Field(gt=0, lt=20000)
    up_mbps: float = Field(ge=0, lt=20000)
    ping_ms: float = Field(ge=0, lt=5000)
    jitter_ms: float | None = Field(default=None, ge=0, lt=5000)
    asn: int | None = Field(default=None, ge=0, lt=5_000_000)  # ignored: the edge header is authoritative
    colo: str | None = Field(default=None, max_length=8)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)


def _token_key() -> bytes:
    return hashlib.sha256(b"isitfast-test-token:" + SALT.encode()).digest()


def sign_token(pid: str, nonce: str, iat: int) -> str:
    payload = f"{pid}.{iat}.{nonce}"
    sig = hmac.new(_token_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def read_token(token: str) -> tuple[str, int, str] | None:
    parts = token.split(".")
    if len(parts) != 4:
        return None
    pid, iat, nonce, sig = parts
    good = hmac.new(_token_key(), f"{pid}.{iat}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, good) or not iat.isdigit():
        return None
    return pid, int(iat), nonce


def check_distance(lat: float, lon: float, acc: float | None, p) -> float:
    """In range = the place is within 50 m of the reported position, widened by its accuracy (capped)."""
    dist = haversine_m(lat, lon, p["lat"], p["lon"])
    slack = min(acc or 0, MAX_ACC_SLACK_M)
    if dist > MAX_TEST_DISTANCE_M + slack:
        rough = f" (your location is only accurate to ±{acc:.0f} m)" if acc and acc > PRECISE_ACC_M else ""
        raise HTTPException(422, f"You seem to be {dist:.0f} m away{rough} — tests must be run at the place.")
    return dist


# sid|dir|bytes|t0|t1[|rid]|sig — rid is a per-response id (receipts without it are from the first Worker
# version, where parallel responses could share a t0 because Workers' clock only advances on I/O).
RECEIPT_RE = re.compile(r"^([0-9a-f]{16})\|([du])\|(\d{1,10})\|(\d{13})\|(\d{13})(?:\|([0-9a-f]{8}))?\|([0-9a-f]{32})$")


def check_receipts(receipts: list[str], nonce: str, iat: int, down: float, up: float) -> dict | None:
    """Bound a claimed result by what our speed Worker actually served/received (signed receipts).

    Download: receipts are cumulative per response (grouped by rid), so keep the largest per response and
    sum; window = [first download t0, last download receipt t1] on the Worker clock. Upload receipts are
    timed as bytes arrive, so [min t0, max t1] is real.
    Raises 403 for forged/tampered receipts or a claim above RECEIPT_SLACK x the proven rate (+2 Mbps for
    tiny transfers); returns None (unverified, accepted) when a direction has no receipts at all."""
    key = RECEIPT_KEY.encode()
    downs: dict[str, int] = {}
    starts: list[int] = []
    ends: list[int] = []

    def reject(why: str):
        metric("receipt_fail", why)
        raise HTTPException(403, "Couldn't verify this test — please run it again.")

    ups: list[tuple[int, int, int]] = []
    lo, hi = iat * 1000 - 60_000, int(time.time() * 1000) + 60_000
    for r in receipts:
        m = RECEIPT_RE.match(r.strip())
        if not m:
            reject("format")
        sid, d, b, t0, t1, rid, sig = m.groups()
        msg = f"{sid}|{d}|{b}|{t0}|{t1}" + (f"|{rid}" if rid else "")
        good = hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            reject("signature")
        if sid != nonce:
            reject("session")
        if not (lo <= int(t0) <= int(t1) <= hi):
            reject("clock")
        if d == "d":
            k = rid or t0  # cumulative receipts of one response share its id: keep the largest
            downs[k] = max(downs.get(k, 0), int(b))
            starts.append(int(t0))
            ends.append(int(t1))
        else:
            ups.append((int(b), int(t0), int(t1)))
    down_bytes, up_bytes = sum(downs.values()), sum(u[0] for u in ups)
    if not downs or not ups:
        # Missing is not forged: a slow uplink can end the test before any upload chunk completes. The
        # result is kept, just not marked verified (same as a test on the fallback endpoint).
        metric("receipt_missing", "down" if not downs else "up")
        print(f"[receipts] missing {'download' if not downs else 'upload'} receipts ({len(receipts)} received)", flush=True)
        return None
    up_start = min(u[1] for u in ups)
    # Download window: first byte of the first response to the last receipt the Worker emitted. (Ending
    # it at the first upload receipt over-counts: on slow uplinks the upload's first bytes reach the
    # Worker seconds later, which read as a slow download and rejected genuine phone tests.) The Worker
    # emits slightly ahead of delivery, so any error here is lenient, never a false rejection.
    down_win = max(min(max(ends), up_start) - min(starts), 300)
    up_win = max(max(u[2] for u in ups) - up_start, 200)
    down_bound = down_bytes * 8 / down_win / 1000  # Mbps (bytes*8 / ms / 1000)
    up_bound = up_bytes * 8 / up_win / 1000
    if down > down_bound * RECEIPT_SLACK + 2 or up > up_bound * RECEIPT_SLACK + 2:
        metric("receipt_fail", "bound_down" if down > down_bound * RECEIPT_SLACK + 2 else "bound_up")
        print(f"[receipts] over bound: claimed {down:.1f}/{up:.1f} Mbps, proven {down_bound:.1f}/{up_bound:.1f} "
              f"({down_bytes}/{up_bytes} B, windows {down_win}/{up_win} ms, {len(downs)} down responses)", flush=True)
        raise HTTPException(403, "This result doesn't match the data transferred — please run it again.")
    return {"down_bytes": down_bytes, "up_bytes": up_bytes, "down_bound": round(down_bound, 1), "up_bound": round(up_bound, 1)}


@app.post("/api/places/{pid}/test-start")
def test_start(pid: str, s: StartIn, req: Request):
    if pid != "new" and not PID_RE.match(pid):
        raise HTTPException(404, "unknown place")
    if pid == "new" and not s.spot:
        raise HTTPException(422, "Give this place a name first.")
    iph, now = ip_key(req), int(time.time())
    c = db(autocommit=True)
    try:
        c.execute("BEGIN IMMEDIATE")
        n = c.execute("SELECT COUNT(*) FROM test_tokens WHERE ip_hash=? AND issued_at > ?", (iph, now - 3600)).fetchone()[0]
        if n >= STARTS_PER_HOUR:
            raise HTTPException(429, "Too many tests from your connection this hour.")
        p = None if pid == "new" else c.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
        if p:
            check_distance(s.lat, s.lon, s.accuracy_m, p)
        else:
            if G_ID_RE.match(pid) and (s.accuracy_m or 0) > PRECISE_ACC_M:
                # The first test pins a Google place where the tester stands, so it needs a precise fix.
                metric("test_reject", "coarse_new_place")
                raise HTTPException(422, f"Your location is only accurate to ±{s.accuracy_m:.0f} m — too rough to put "
                                         "a new place on the map. Run this first test from your phone.")
            if s.spot:
                check_distance(s.lat, s.lon, s.accuracy_m, {"lat": s.spot.lat, "lon": s.spot.lon, "kind": s.spot.kind})
            p = adopt(c, pid, s.spot, (s.lat, s.lon))
            check_distance(s.lat, s.lon, s.accuracy_m, p)
        nonce = secrets.token_hex(8)
        c.execute("INSERT INTO test_tokens VALUES (?,?,?,?,NULL)", (nonce, p["id"], iph, now))
        c.execute("DELETE FROM test_tokens WHERE issued_at < ?", (now - 86400,))
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    metric("test_start")
    return {"id": p["id"], "token": sign_token(p["id"], nonce, now), "sid": nonce, "min_s": MIN_TEST_S}


@app.post("/api/places/{pid}/tests")
def add_test(pid: str, t: TestIn, req: Request):
    # Only trust the ASN Cloudflare saw for this request (a transform rule sets x-visitor-asn). Never fall
    # back to the client-supplied value: without the edge header the network can't be verified.
    try:
        asn = int(req.headers.get("x-visitor-asn", "")) or None
    except ValueError:
        asn = None
    asn = asn or int(os.environ.get("DEV_ASN", "0")) or None  # local development only; unset in production
    if not asn:
        metric("test_reject", "no_asn")
        raise HTTPException(422, "Couldn't identify your network — please try again.")
    tok = read_token(t.token)
    now = int(time.time())
    if not tok or tok[0] != pid:
        metric("test_reject", "bad_token")
        raise HTTPException(403, "This test wasn't started here — please run it again.")
    _, iat, nonce = tok
    if now - iat < MIN_TEST_S or now - iat > TOKEN_TTL_S:
        metric("test_reject", "timing")
        raise HTTPException(403, "This test took too long or too little time — please run it again.")
    verified = 0
    if t.receipts and RECEIPT_KEY:
        try:
            proof = check_receipts(t.receipts, nonce, iat, t.down_mbps, t.up_mbps)
        except HTTPException:
            metric("test_reject", "receipts")
            raise
        verified = 1 if proof else 0
    carrier = asn_name(asn) or "Unknown"
    iph = ip_key(req)
    c = db(autocommit=True)
    try:
        c.execute("BEGIN IMMEDIATE")  # rate checks and the insert are one atomic step
        if c.execute("UPDATE test_tokens SET used_at=? WHERE nonce=? AND place_id=? AND used_at IS NULL",
                     (now, nonce, pid)).rowcount != 1:
            metric("test_reject", "token_used")
            raise HTTPException(403, "This test result was already saved.")
        p = c.execute("SELECT lat, lon, kind FROM places WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "unknown place")
        try:
            dist = check_distance(t.lat, t.lon, t.accuracy_m, p)
        except HTTPException:
            metric("test_reject", "distance")
            raise
        recent = c.execute("SELECT COUNT(*), SUM(place_id=?) FROM tests WHERE ip_hash=? AND created_at > ?",
                           (pid, iph, now - 3600)).fetchone()
        if recent[0] >= TESTS_PER_HOUR or (recent[1] or 0) >= TESTS_PER_PLACE_PER_HOUR:
            metric("test_reject", "rate")
            raise HTTPException(429, "Too many tests from your connection this hour.")
        network = t.network
        if network == "cellular" and asn in FIXED_ASNS:
            network = "wifi"  # user picked "mobile data" but the connection is fixed broadband
        c.execute(
            "INSERT INTO tests (place_id, network, asn, carrier, colo, down_mbps, up_mbps, ping_ms, jitter_ms, dist_m, ip_hash, created_at, verified, acc_m) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, network, asn, carrier, t.colo, round(t.down_mbps, 2), round(t.up_mbps, 2), round(t.ping_ms, 1),
             t.jitter_ms, round(dist), iph, now, verified, round(t.accuracy_m) if t.accuracy_m is not None else None),
        )
        bump_tiles(c, [tile_of(p["lat"], p["lon"])])
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    metric("test_saved", f"{network}:{'verified' if verified else 'unverified'}")
    approx = bool(t.accuracy_m and t.accuracy_m > PRECISE_ACC_M)
    return {"ok": True, "carrier": carrier, "network": network, "distance_m": round(dist), "verified": bool(verified),
            "approx": approx}


# ---------------------------------------------------------------- ASN names (shared across workers)
ASN_TTL_OK = 30 * 86400
ASN_TTL_FAIL = 3600


def asn_name(asn: int | None) -> str | None:
    """Friendly carrier/ISP name for an ASN: curated map first, then Team Cymru's DNS (via DoH), cached in
    SQLite (30 days; failures retried after an hour)."""
    if not asn:
        return None
    if asn in ASN_NAMES:
        return ASN_NAMES[asn]
    now = int(time.time())
    with db() as c:
        r = c.execute("SELECT name, ok, at FROM asn_cache WHERE asn=?", (asn,)).fetchone()
    if r and r["at"] > now - (ASN_TTL_OK if r["ok"] else ASN_TTL_FAIL):
        return r["name"]
    name, ok = f"AS{asn}", 0
    try:
        resp = outbound("doh", lambda: HTTP.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": f"AS{asn}.asn.cymru.com", "type": "TXT"},
            headers={"accept": "application/dns-json"}, timeout=4,
        ))
        # e.g. "55836 | IN | apnic | 2015-07-27 | RELIANCEJIO-IN Reliance Jio Infocomm Limited, IN"
        txt = resp.json()["Answer"][0]["data"].strip('"')
        org = txt.split("|")[-1].strip().rsplit(",", 1)[0]
        parts = org.split(" ", 1)  # "HANDLE Org name" or "HANDLE - Org name" -> "Org name"
        name = (parts[1] if len(parts) > 1 else parts[0]).lstrip("- ")[:40].strip() or name
        ok = 1
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        pass
    with db() as c:
        c.execute("INSERT OR REPLACE INTO asn_cache VALUES (?,?,?,?)", (asn, name, ok, now))
    return name


def client_ip(req: Request) -> str:
    """The visitor's IP as seen by Cloudflare. X-Forwarded-For's first entry is client-controlled, so it is
    only used as a last resort via its right-most hop (the one our own proxy appended)."""
    cf = req.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = [x.strip() for x in req.headers.get("x-forwarded-for", "").split(",") if x.strip()]
    return xff[-1] if xff else (req.client.host if req.client else "?")


def ip_key(req: Request) -> str:
    """Salted hash of the client network: a whole IPv6 /64 counts as one client (one device owns a /64)."""
    ip = client_ip(req)
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6:
            ip = str(ipaddress.ip_network(f"{a}/64", strict=False).network_address) + "/64"
    except ValueError:
        pass
    return hashlib.sha256((SALT + ip).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- search
GEOCODE_TTL = 30 * 86400
NOMINATIM_GAP_MS = 1100  # Nominatim policy: at most 1 request/second for the whole app


def nominatim_slot() -> None:
    """Reserve the next Nominatim slot across all workers (SQLite), waiting up to 2 s; else 429."""
    now_ms = int(time.time() * 1000)
    c = db(autocommit=True)
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT at FROM jobs WHERE name='nominatim_ms'").fetchone()
        slot = max(now_ms, (r["at"] if r else 0) + NOMINATIM_GAP_MS)
        if slot - now_ms > 2000:
            c.execute("ROLLBACK")
            raise HTTPException(429, "Search is busy — try again in a moment.")
        c.execute("INSERT OR REPLACE INTO jobs VALUES ('nominatim_ms', ?)", (slot,))
        c.execute("COMMIT")
    finally:
        c.close()
    if slot > now_ms:
        time.sleep((slot - now_ms) / 1000)


@app.get("/api/geocode")
def geocode(q: str = Query(..., min_length=2, max_length=120)):
    """Places we know by name first (no external call), then Nominatim (India only, 1 req/s, cached 30 days)."""
    key = " ".join(q.strip().lower().split())
    like = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with db() as c:
        local = [
            {"name": f"{r['name']} · {r['kind'].replace('_', ' ')}", "lat": r["lat"], "lon": r["lon"], "bbox": None, "place": r["id"]}
            for r in c.execute(
                "SELECT id, name, kind, lat, lon FROM places WHERE name LIKE ? ESCAPE '\\' AND gone_at IS NULL "
                "ORDER BY (name LIKE ? ESCAPE '\\') DESC, length(name) LIMIT 3",
                (f"%{like}%", f"{like}%"),
            )
        ]
        r = c.execute("SELECT body FROM geocode_cache WHERE q=? AND at > ?", (key, int(time.time()) - GEOCODE_TTL)).fetchone()
    if r:
        return local + json.loads(r["body"])
    nominatim_slot()
    try:
        resp = outbound("nominatim", lambda: HTTP.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 5, "countrycodes": "in"}, timeout=10,
        ))
    except httpx.HTTPError:
        resp = None
    if resp is None or resp.status_code != 200:
        if local:
            return local
        raise HTTPException(502, "geocoder unavailable")
    body = [{"name": x["display_name"], "lat": float(x["lat"]), "lon": float(x["lon"]), "bbox": x.get("boundingbox")} for x in resp.json()]
    with db() as c:
        c.execute("INSERT OR REPLACE INTO geocode_cache VALUES (?,?,?)", (key, json.dumps(body), int(time.time())))
    return local + body


# ---------------------------------------------------------------- instrumentation
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")


class TelemetryIn(BaseModel):
    """Client beacon after a speed test (no identifiers): which endpoint ran it and the data it used."""
    endpoint: str = Field(pattern="^(own|fallback)$")
    outcome: str = Field(pattern="^(saved|rejected|failed|cancelled)$")
    network: str = Field(pattern="^(wifi|cellular)$")
    down_mb: float = Field(ge=0, le=5000)
    up_mb: float = Field(ge=0, le=5000)
    own_failed: bool = False


@app.post("/api/telemetry", status_code=204)
def telemetry(t: TelemetryIn):
    metric("telemetry", f"endpoint:{t.endpoint}")
    metric("telemetry", f"outcome:{t.outcome}")
    if t.own_failed:
        metric("telemetry", "own_endpoint_failed")
    timing(f"test_mb:{t.network}", t.down_mb + t.up_mb)
    return Response(status_code=204)


@app.get("/api/metrics")
def metrics_view(req: Request, minutes: int = Query(60, ge=1, le=14 * 1440)):
    """Operational metrics for the founder (token-protected; 404 when METRICS_TOKEN is unset)."""
    if not METRICS_TOKEN or not hmac.compare_digest(req.headers.get("x-metrics-token", ""), METRICS_TOKEN):
        raise HTTPException(404, "Not Found")
    metrics.flush(db, _counts, _timings)  # include this worker's latest numbers
    out = metrics.summary(db, minutes)
    with db() as c:
        out["data"] = {
            "tests": c.execute("SELECT COUNT(*) FROM tests").fetchone()[0],
            "tests_verified": c.execute("SELECT COUNT(*) FROM tests WHERE verified=1").fetchone()[0],
            "tests_24h": c.execute("SELECT COUNT(*) FROM tests WHERE created_at > ?", (int(time.time()) - 86400,)).fetchone()[0],
            "places": c.execute("SELECT COUNT(*) FROM places").fetchone()[0],
            "places_adopted": c.execute("SELECT COUNT(*) FROM places WHERE cell=?", (ADOPTED,)).fetchone()[0],
            "places_gone": c.execute("SELECT COUNT(*) FROM places WHERE gone_at IS NOT NULL").fetchone()[0],
            "test_tokens": c.execute("SELECT COUNT(*) FROM test_tokens").fetchone()[0],
            "jobs": {r["name"]: r["at"] for r in c.execute("SELECT name, at FROM jobs")},
        }
    sizes = {}
    for f in ("app.db", "app.db-wal", "ookla.db", "carriers.db", "basemap/india.pmtiles"):
        try:
            sizes[f] = round(os.path.getsize(os.path.join(DATA_DIR, f)) / 1e6, 1)
        except OSError:
            pass
    out["data"]["file_mb"] = sizes
    backups = sorted(os.listdir(os.path.join(DATA_DIR, "backups"))) if os.path.isdir(os.path.join(DATA_DIR, "backups")) else []
    out["data"]["last_backup"] = backups[-1] if backups else None
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- product analytics (see app/analytics.py)
class EventIn(BaseModel):
    n: str = Field(min_length=1, max_length=32)
    r: str | None = Field(default=None, max_length=40)
    p: dict[str, str | int | float | bool] | None = Field(default=None, max_length=12)


@app.post("/api/e", status_code=204)
def event(e: EventIn, req: Request):
    """Cookieless funnel event (navigator.sendBeacon). Always 204, so it can't be used to probe anything."""
    try:
        with db() as c:
            if analytics.record(c, client_ip(req), req.headers.get("user-agent", "")[:300], e.n, e.r, e.p):
                metric("event", e.n)
    except sqlite3.OperationalError:
        metric("event", "dropped")
    return Response(status_code=204)


def require_metrics_token(req: Request) -> None:
    if not METRICS_TOKEN or not hmac.compare_digest(req.headers.get("x-metrics-token", ""), METRICS_TOKEN):
        raise HTTPException(404, "Not Found")


@app.get("/api/stats")
def stats_view(req: Request, days: int = Query(7, ge=1, le=120)):
    """Launch funnel by day and by source (token-protected like /api/metrics; web/stats.html renders it)."""
    require_metrics_token(req)
    with db() as c:
        return JSONResponse(analytics.summary(c, days), headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- first view: the densest tested cluster
HOME_BBOX = (12.70, 77.30, 13.25, 77.90)  # Bengaluru (launch city); falls back to anywhere, then the default
HOTSPOT_CELL = 0.02                       # ~2.2 km grid
_hotspot: tuple[float, dict] = (0.0, {})


@app.get("/api/hotspot")
def hotspot():
    """Where a first-time visitor's map should open: the ~2 km cell with the most tested places, so the first
    screen shows real results instead of an empty map. Cached for 5 minutes per worker."""
    global _hotspot
    if time.time() - _hotspot[0] < 300 and _hotspot[1]:
        body = _hotspot[1]
    else:
        with db() as c:
            rows = c.execute("SELECT p.lat, p.lon FROM places p WHERE p.id IN (SELECT DISTINCT place_id FROM tests)").fetchall()
        s, w, n, e = HOME_BBOX
        home = [r for r in rows if s <= r["lat"] <= n and w <= r["lon"] <= e] or rows
        cells: dict[tuple[int, int], list] = {}
        for r in home:
            cells.setdefault((math.floor(r["lat"] / HOTSPOT_CELL), math.floor(r["lon"] / HOTSPOT_CELL)), []).append(r)
        if cells:
            best = max(cells.values(), key=len)
            body = {"lat": round(sum(r["lat"] for r in best) / len(best), 5),
                    "lon": round(sum(r["lon"] for r in best) / len(best), 5), "zoom": 15, "tested": len(best)}
        else:
            body = {"lat": None, "lon": None, "zoom": None, "tested": 0}
        _hotspot = (time.time(), body)
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})


# ---------------------------------------------------------------- QR codes (desktop -> phone hand-off, table cards)
@app.get("/qr.svg")
def qr_svg(request: Request, p: str | None = Query(default=None, max_length=310), r: str = Query(default="qr", max_length=32)):
    """QR for a place link (/p/<id>?r=..) or, without p, for the "test where I am" flow on the phone."""
    import segno  # noqa: PLC0415 — only this route needs it

    ref = analytics.clean_ref(r) or "qr"
    if p is not None and not PID_RE.match(p):
        raise HTTPException(404, "unknown place")
    base = site_base(request)
    url = f"{base}/p/{p}?r={ref}" if p else f"{base}/?go=here&r={ref}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=4, border=2, dark="#1B1A17", light="#FFFFFF", xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.exception_handler(sqlite3.OperationalError)
def sqlite_error(_req: Request, exc: sqlite3.OperationalError):
    kind = "locked" if "locked" in str(exc) else "busy" if "busy" in str(exc) else "other"
    metric("db_error", kind)
    return JSONResponse({"detail": "Busy — please try again in a moment."}, status_code=503)


@app.get("/api/meta")
def meta():
    with db() as c:
        n_places = c.execute("SELECT COUNT(*) FROM places").fetchone()[0]
        n_tests = c.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    return {"ookla": ookla_meta(), "places_cached": n_places, "community_tests": n_tests}


@app.get("/api/whereami")
def whereami(req: Request):
    """Rough IP-based location from Cloudflare visitor-location headers (fallback when GPS is denied).
    `country` (ISO 3166-1 alpha-2, from CF-IPCountry) lets the page tell visitors outside India that we
    have no data for them yet; null when unknown (XX = unknown, T1 = Tor)."""
    cc = (req.headers.get("cf-ipcountry") or "").upper()
    country = cc if len(cc) == 2 and cc.isalpha() and cc not in ("XX", "T1") else None
    try:
        lat = float(req.headers.get("cf-iplatitude", ""))
        lon = float(req.headers.get("cf-iplongitude", ""))
    except ValueError:
        return {"lat": None, "lon": None, "country": country}
    return {"lat": lat, "lon": lon, "city": req.headers.get("cf-ipcity"), "country": country}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.middleware("http")
async def route_timing(request: Request, call_next):
    """Latency + count per route template (static files grouped as one)."""
    t0 = time.perf_counter()
    resp = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", None) or ("static" if request.url.path.startswith(("/vendor", "/basemap/fonts", "/basemap/sprites")) or "." in request.url.path else "other")
    key = f"{request.method} {path}"
    timing(f"route:{key}", (time.perf_counter() - t0) * 1000)
    metric("status", f"{resp.status_code // 100}xx")
    return resp


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    """Versioned assets (?v=N, /vendor/*) are immutable for a year; HTML is revalidated every time;
    everything else static is cached for an hour. API routes set their own headers."""
    resp = await call_next(request)
    path = request.url.path
    if request.method == "GET" and resp.status_code == 200 and not path.startswith("/api/") and "cache-control" not in resp.headers:
        if request.query_params.get("v") or path.startswith("/vendor/"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith(("/basemap/fonts/", "/basemap/sprites/")):
            resp.headers["Cache-Control"] = "public, max-age=2592000"
        elif path == "/" or path.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        else:
            resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# Origin lock: when ORIGIN_SECRET is set, only requests carrying it in X-Origin-Auth are served. A Cloudflare
# transform rule adds the header at the edge, so traffic sent straight to the server's IP (which could forge
# cf-connecting-ip / x-visitor-asn) gets a 403. Unset = no check (local dev, tests).
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")


@app.middleware("http")
async def origin_lock(request: Request, call_next):
    if ORIGIN_SECRET and request.url.path != "/healthz" and not hmac.compare_digest(
            request.headers.get("x-origin-auth", "").encode("latin-1", "replace"), ORIGIN_SECRET.encode()):
        metric("origin_lock", "rejected")
        return Response("Forbidden", status_code=403, media_type="text/plain")
    return await call_next(request)


app.add_middleware(GZipMiddleware, minimum_size=1024)


# Google Maps JS key: browser-side by design, locked to our domains by an HTTP-referrer restriction and capped
# by per-API quotas in Google Cloud. Kept out of git; injected into index.html at request time.
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")
GOOGLE_MAP_ID = os.environ.get("GOOGLE_MAP_ID", "") or "DEMO_MAP_ID"


@functools.lru_cache(maxsize=4)
def _index_html(_stamp: tuple) -> str:
    with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    safe = lambda v: re.sub(r"[^A-Za-z0-9_-]", "", v)  # noqa: E731 — keys/map ids are [A-Za-z0-9_-]
    return (html.replace("__GOOGLE_MAPS_KEY__", safe(GOOGLE_MAPS_KEY)).replace("__GOOGLE_MAP_ID__", safe(GOOGLE_MAP_ID))
            )


SITE_TITLE = "isitfast — is the cafe's WiFi fast enough to work from?"
SITE_DESC = ("Real WiFi and Jio/Airtel/Vi speeds at Bengaluru cafes and coworking spaces, tested by people who were "
             "sitting there. Know if the WiFi can survive your standup, before you order. Free, no app, no login.")


def render_index(request: Request | None, title: str = SITE_TITLE, desc: str = SITE_DESC, path: str = "/") -> Response:
    """index.html with its title, description and canonical/OG URL filled in (link previews read these)."""
    html = _index_html(file_stamp(os.path.join(WEB_DIR, "index.html")))
    esc = lambda v: htmlmod.escape(v, quote=True)  # noqa: E731
    base = site_base(request)
    html = (html.replace("__TITLE__", esc(title)).replace("__DESC__", esc(desc)).replace("__URL__", esc(base + path))
            .replace("__BASE__", esc(base)).replace("__HOST__", esc(base.removeprefix("https://"))))
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


def _ago(ts: int) -> str:
    d = max(0, int(time.time()) - ts)
    return "today" if d < 86400 else "yesterday" if d < 2 * 86400 else f"{d // 86400} days ago"


def place_preview_text(pid: str) -> tuple[str, str] | None:
    """Title + description for a shared /p/<id> link, from our own data. Google places keep no name (only the
    place_id may be stored), so their previews use a generic title."""
    with db() as c:
        p = c.execute("SELECT name FROM places WHERE id=?", (pid,)).fetchone()
        if not p:
            return None
        tests = c.execute("SELECT network, carrier, down_mbps, up_mbps, created_at FROM tests WHERE place_id=? "
                          "ORDER BY created_at DESC LIMIT 200", (pid,)).fetchall()
    name = p["name"] or "This place"
    title = f"{name} — WiFi & mobile data speeds · isitfast" if p["name"] else "WiFi & mobile data speeds here · isitfast"
    wifi = next((t for t in tests if t["network"] == "wifi"), None)
    latest_cell: dict[str, sqlite3.Row] = {}
    for t in tests:
        if t["network"] == "cellular":
            latest_cell.setdefault(t["carrier"], t)
    parts = []
    if wifi:
        parts.append(f"WiFi {wifi['down_mbps']:.0f}↓ / {wifi['up_mbps']:.0f}↑ Mbps, tested {_ago(wifi['created_at'])}")
    if latest_cell:
        best = max(latest_cell.values(), key=lambda t: t["down_mbps"])
        parts.append(f"{best['carrier']} mobile data {best['down_mbps']:.0f}↓ Mbps")
    desc = (". ".join(parts) + ". Tested on site by people who were there." if parts
            else "No one has tested here yet. Be the first: a 20-second speed test, run at the place.")
    return title, desc


@app.get("/")
@app.get("/index.html")
def index(request: Request):
    return render_index(request)


@app.get("/p/{pid}")
def place_page(pid: str, request: Request):
    """Shareable place URL. Same app as /, with link-preview tags for the place; the client opens it."""
    if not PID_RE.match(pid):
        raise HTTPException(404, "unknown place")
    t = place_preview_text(pid)
    return render_index(request, *t, path=f"/p/{pid}") if t else render_index(request, path=f"/p/{pid}")


# Self-hosted basemap (vector tiles, style, fonts, sprites); see app/basemap.py
app.include_router(basemap.router)
os.makedirs(os.path.join(basemap.BASE, "fonts"), exist_ok=True)
os.makedirs(os.path.join(basemap.BASE, "sprites"), exist_ok=True)
app.mount("/basemap/fonts", StaticFiles(directory=os.path.join(basemap.BASE, "fonts")), name="fonts")
app.mount("/basemap/sprites", StaticFiles(directory=os.path.join(basemap.BASE, "sprites")), name="sprites")
app.mount("/", StaticFiles(directory=WEB_DIR), name="web")

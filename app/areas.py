"""Neighbourhoods ("areas") for the seeded cities: OpenStreetMap place=suburb nodes, used to group cafes into
pages people actually search for ("cafes to work from in Koramangala").

Each place belongs to the nearest area node within AREA_RADIUS_M. Areas are refreshed with the weekly OSM seed
(app/seed.py); `python -m app.areas [city ...]` refreshes them on demand.
"""
import asyncio
import math
import re
import sqlite3
import time
import unicodedata

import httpx

AREA_RADIUS_M = 3000     # a place further than this from every area node belongs to none
MIN_SUBURBS = 10         # a city mapped with fewer suburb nodes also uses place=quarter
SCHEMA = """
CREATE TABLE IF NOT EXISTS areas (city TEXT NOT NULL, slug TEXT NOT NULL, name TEXT NOT NULL, lat REAL NOT NULL,
  lon REAL NOT NULL, osm_id TEXT, updated_at INTEGER, PRIMARY KEY (city, slug));
"""


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:60]


def query(s: float, w: float, n: float, e: float) -> str:
    return f'[out:json][timeout:90];node["place"~"^(suburb|quarter)$"]["name"]({s},{w},{n},{e});out body;'


def pick(elements: list[dict]) -> list[dict]:
    """Suburb nodes, plus quarters when the city has too few suburbs mapped. English name when tagged."""
    nodes = [e for e in elements if e.get("type") == "node" and e.get("tags", {}).get("name")]
    sub = [e for e in nodes if e["tags"]["place"] == "suburb"]
    use = sub if len(sub) >= MIN_SUBURBS else nodes
    out, seen = [], set()
    for e in sorted(use, key=lambda e: (e["tags"]["place"] != "suburb", e["id"])):
        name = (e["tags"].get("name:en") or e["tags"]["name"]).strip()
        slug = slugify(name)
        if not slug or slug in seen:  # first one wins: suburbs before quarters, then lowest OSM id
            continue
        seen.add(slug)
        out.append({"slug": slug, "name": name, "lat": e["lat"], "lon": e["lon"], "osm_id": f"n{e['id']}"})
    return out


async def fetch(endpoints: list[str], ua: str, bbox: tuple) -> list[dict]:
    last = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), headers={"User-Agent": ua}) as client:
        for ep in endpoints:
            for attempt in range(3):
                try:
                    r = await client.post(ep, data={"data": query(*bbox)})
                    if r.status_code == 200:
                        body = r.json()
                        if body.get("remark"):  # Overpass timed out or ran out of memory: elements are partial
                            last = f"{ep} -> remark {body['remark'][:80]!r}"
                            break
                        return pick(body.get("elements", []))
                    last = f"{ep} -> {r.status_code}"
                    if r.status_code in (429, 504):  # busy: back off and retry the same mirror
                        await asyncio.sleep(10 + attempt * 20)
                        continue
                    break
                except (httpx.HTTPError, ValueError) as ex:
                    last = f"{ep} -> {ex!r}"
                    break
    raise RuntimeError(f"overpass areas failed: {last}")


def store(c: sqlite3.Connection, city: str, areas: list[dict]) -> None:
    """Replace one city's areas in one transaction (readers see the old or the new set, never a mix)."""
    prev = c.execute("SELECT COUNT(*) FROM areas WHERE city=?", (city,)).fetchone()[0]
    if not areas or len(areas) < 0.7 * prev:  # an empty or truncated Overpass answer must not wipe the city's pages
        print(f"[areas] {city}: kept {prev} (got {len(areas)})", flush=True)
        return
    now = int(time.time())
    c.execute("BEGIN")
    c.execute("DELETE FROM areas WHERE city=?", (city,))
    c.executemany("INSERT INTO areas VALUES (?,?,?,?,?,?,?)",
                  [(city, a["slug"], a["name"], a["lat"], a["lon"], a["osm_id"], now) for a in areas])
    c.execute("COMMIT")


def _m(lat1, lon1, lat2, lon2) -> float:
    """Equirectangular distance in metres; exact enough inside one city."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 6371000 * math.hypot(x, math.radians(lat2 - lat1))


def nearest(areas: list[dict], lat: float, lon: float) -> dict | None:
    best = min(areas, key=lambda a: _m(lat, lon, a["lat"], a["lon"]), default=None)
    return best if best and _m(lat, lon, best["lat"], best["lon"]) <= AREA_RADIUS_M else None


async def refresh(cities: list[str] | None = None) -> None:
    from .main import OVERPASS, UA, db, init_db  # noqa: PLC0415 — main imports this module
    from .seed import CITIES  # noqa: PLC0415
    init_db()
    for city in cities or CITIES:
        try:
            areas = await fetch(OVERPASS, UA, CITIES[city])
            c = db(autocommit=True)
            try:
                store(c, city, areas)
            finally:
                c.close()
            print(f"[areas] {city}: {len(areas)}", flush=True)
        except Exception as ex:  # keep going city by city
            print(f"[areas] {city} failed: {ex}", flush=True)
        await asyncio.sleep(5)


if __name__ == "__main__":
    import sys
    asyncio.run(refresh(sys.argv[1:] or None))

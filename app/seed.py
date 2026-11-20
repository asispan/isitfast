"""Pre-seed / refresh the places cache for whole metro areas with one Overpass query each.

This is the only code path that talks to Overpass: visitor requests never do. The container runs it
weekly (see entrypoint.sh); a city that fails is simply retried next week, and places that drop out of
OSM are never deleted here, so tested places keep their history.

Usage: python -m app.seed [city ...]              (default: all cities below)
       python -m app.seed --bbox s,w,n,e
       python -m app.seed --if-older-days 6       (no-op if the last full run was more recent)
"""
import argparse
import asyncio
import json
import time

from . import areas, osm
from .geo import quadkey
from .main import CELL, OVERPASS, PLACE_COLS, UA, bump_tiles, db, init_db, tile_of

CITIES = {
    "bengaluru": (12.80, 77.45, 13.15, 77.80),
    "mumbai": (18.88, 72.77, 19.30, 73.10),
    "delhi-ncr": (28.38, 76.95, 28.80, 77.40),
    "hyderabad": (17.30, 78.30, 17.55, 78.60),
    "chennai": (12.90, 80.10, 13.20, 80.32),
    "pune": (18.42, 73.72, 18.65, 73.98),
    "kolkata": (22.45, 88.25, 22.68, 88.48),
    "goa": (15.20, 73.70, 15.80, 74.00),
}
JOB = "seed"


async def seed_bbox(name: str, s: float, w: float, n: float, e: float) -> None:
    t0 = time.time()
    places = await osm.fetch_overpass(OVERPASS, UA, s, w, n, e)
    now = int(time.time())
    cells = osm.cells_for_bbox(s, w, n, e, CELL)
    inside = {
        osm.cell_key(i, j)
        for i, j in cells
        if i * CELL >= s and (i + 1) * CELL <= n and j * CELL >= w and (j + 1) * CELL <= e
    }
    counts: dict[str, int] = {}
    with db() as c:
        rows = []
        for p in places:
            i, j = int(p["lat"] // CELL), int(p["lon"] // CELL)
            key = osm.cell_key(i, j)
            counts[key] = counts.get(key, 0) + 1
            rows.append((p["id"], p["name"], p["kind"], p["lat"], p["lon"], quadkey(p["lat"], p["lon"]),
                         json.dumps(osm.public_tags(p["tags"])), key, now))
        # Places that vanished from OSM are hidden (gone_at), never deleted: their tests stay. Skipped if
        # Overpass returned suspiciously little (a partial response must not hide a whole city).
        have = c.execute(
            f"SELECT id, lat, lon FROM places WHERE cell IN ({','.join('?' * len(inside))}) AND gone_at IS NULL",
            list(inside),
        ).fetchall() if inside else []
        seen = {p["id"] for p in places}
        gone = [r for r in have if r["id"] not in seen]
        if len(places) >= 0.7 * len(have):
            c.executemany("UPDATE places SET gone_at=? WHERE id=?", [(now, r["id"]) for r in gone])
        else:
            print(f"[{name}] only {len(places)} of {len(have)} places returned — not marking any as gone", flush=True)
            gone = []
        c.executemany(f"INSERT OR REPLACE INTO places ({PLACE_COLS}) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        c.executemany("INSERT OR REPLACE INTO cells VALUES (?,?,?)", [(k, now, counts.get(k, 0)) for k in inside])
        bump_tiles(c, {tile_of(p["lat"], p["lon"]) for p in places} | {tile_of(r["lat"], r["lon"]) for r in gone})
    print(f"[{name}] {len(places)} places ({len(gone)} gone), {len(inside)} cells, {time.time()-t0:.0f}s", flush=True)


async def seed_split(name: str, s: float, w: float, n: float, e: float, depth: int = 0) -> None:
    """Seed a box; if Overpass times out (big metros now that restaurants/offices/apartments are included),
    split it into north/south halves and retry each, up to 3 levels deep."""
    try:
        await seed_bbox(name, s, w, n, e)
    except RuntimeError as ex:
        if depth >= 3:
            raise
        print(f"[{name}] {ex} — splitting", flush=True)
        mid = round((s + n) / 2 / CELL) * CELL  # on a cell boundary, so every cell lies wholly in one half
        if not s < mid < n:
            mid = (s + n) / 2
        for k, (a, b) in enumerate(((s, mid), (mid, n))):
            await asyncio.sleep(10)
            await seed_split(f"{name}/{k}", a, w, b, e, depth + 1)


def last_run() -> int:
    with db() as c:
        r = c.execute("SELECT at FROM jobs WHERE name=?", (JOB,)).fetchone()
    return r["at"] if r else 0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cities", nargs="*")
    ap.add_argument("--bbox")
    ap.add_argument("--if-older-days", type=float)
    a = ap.parse_args()
    init_db()
    if a.bbox:
        await seed_split("bbox", *[float(x) for x in a.bbox.split(",")])
        return
    if a.if_older_days is not None and time.time() - last_run() < a.if_older_days * 86400:
        print(f"[seed] last run {(time.time() - last_run()) / 86400:.1f} days ago — skipping", flush=True)
        return
    for name in a.cities or CITIES:
        try:
            await seed_split(name, *CITIES[name])
        except Exception as ex:  # keep going city by city
            print(f"[{name}] failed: {ex}", flush=True)
        await asyncio.sleep(5)
    await areas.refresh(a.cities or None)  # neighbourhood pages (app/areas.py)
    if not a.cities:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (JOB, int(time.time())))


if __name__ == "__main__":
    asyncio.run(main())

"""Self-hosted basemap: vector tiles from a local PMTiles archive + style, fonts and sprites.

The archive (OpenMapTiles schema, built with Planetiler — same schema as OpenFreeMap, so the map style
and the "tap any POI" OSM-id decoding keep working) lives at $DATA_DIR/basemap/india.pmtiles.
Tiles are served as ordinary /basemap/tiles/{z}/{x}/{y}.pbf URLs: small, identical for every visitor and
cached at the CDN, instead of browser range requests into a 2 GB file the CDN cannot cache.

    python -m app.basemap fetch-assets   # one-off: style + fonts + sprites into $DATA_DIR/basemap
"""
import functools
import json
import os
import sys

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

DATA_DIR = os.environ.get("DATA_DIR", "/data")
BASE = os.path.join(DATA_DIR, "basemap")
ARCHIVE = os.path.join(BASE, "india.pmtiles")
UPSTREAM = "https://tiles.openfreemap.org"
STYLE_SRC = f"{UPSTREAM}/styles/liberty"
FONTS = ["Noto Sans Regular", "Noto Sans Bold", "Noto Sans Italic"]
TILE_CACHE = "public, max-age=86400, s-maxage=2592000, stale-while-revalidate=604800"
ATTRIBUTION = ('<a href="https://openmaptiles.org/" target="_blank">&copy; OpenMapTiles</a> '
               '<a href="https://www.openstreetmap.org/copyright" target="_blank">&copy; OpenStreetMap contributors</a>')

router = APIRouter()


@functools.lru_cache(maxsize=1)
def _reader(stamp: tuple):
    from pmtiles.reader import MmapSource, Reader
    f = open(ARCHIVE, "rb")
    return Reader(MmapSource(f))


def reader():
    try:
        st = os.stat(ARCHIVE)
    except FileNotFoundError:
        return None
    return _reader((st.st_ino, st.st_mtime_ns))  # a rebuilt archive swapped in with os.replace is picked up


@functools.lru_cache(maxsize=4096)
def _tile(stamp: tuple, z: int, x: int, y: int) -> bytes | None:
    return reader().get(z, x, y)


@router.get("/basemap/tiles/{z}/{x}/{y}.pbf")
def tile(z: int, x: int, y: int):
    r = reader()
    if r is None:
        raise HTTPException(404, "basemap not installed")
    h = r.header()
    if not (0 <= z <= h["max_zoom"]) or not (0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(404, "no such tile")
    st = os.stat(ARCHIVE)
    data = _tile((st.st_ino, st.st_mtime_ns), z, x, y)
    if data is None:  # outside the archive (sea, other countries): an empty tile, cacheable
        return Response(b"", media_type="application/x-protobuf", headers={"Cache-Control": TILE_CACHE})
    headers = {"Cache-Control": TILE_CACHE}
    if h["tile_compression"].value == 2:  # gzip
        headers["Content-Encoding"] = "gzip"
    return Response(data, media_type="application/x-protobuf", headers=headers)


@router.get("/basemap/tiles.json")
def tilejson(req: Request):
    r = reader()
    if r is None:
        raise HTTPException(404, "basemap not installed")
    h, meta = r.header(), r.metadata()
    origin = f"{req.url.scheme}://{req.url.netloc}"
    return JSONResponse({
        "tilejson": "3.0.0",
        "tiles": [f"{origin}/basemap/tiles/{{z}}/{{x}}/{{y}}.pbf"],
        "minzoom": h["min_zoom"], "maxzoom": h["max_zoom"],
        "bounds": [h["min_lon_e7"] / 1e7, h["min_lat_e7"] / 1e7, h["max_lon_e7"] / 1e7, h["max_lat_e7"] / 1e7],
        "attribution": ATTRIBUTION,
        "vector_layers": meta.get("vector_layers", []),
    }, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/basemap/style.json")
def style(req: Request):
    path = os.path.join(BASE, "style.json")
    if not os.path.exists(path) or reader() is None:
        raise HTTPException(404, "basemap not installed")
    origin = f"{req.url.scheme}://{req.url.netloc}"
    s = json.load(open(path))
    s["sources"]["openmaptiles"] = {"type": "vector", "url": f"{origin}/basemap/tiles.json"}
    s["glyphs"] = f"{origin}/basemap/fonts/{{fontstack}}/{{range}}.pbf"
    s["sprite"] = f"{origin}/basemap/sprites/ofm"
    return JSONResponse(s, headers={"Cache-Control": "public, max-age=3600"})


def fetch_assets() -> None:
    """Style (minus the third-party raster hillshade), fonts and sprites, mirrored once."""
    os.makedirs(BASE, exist_ok=True)
    c = httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": "isitfast.cloud basemap mirror"})
    s = c.get(STYLE_SRC).json()
    raster = {k for k, v in s["sources"].items() if v.get("type") == "raster"}
    s["sources"] = {k: v for k, v in s["sources"].items() if k not in raster}
    s["layers"] = [l for l in s["layers"] if l.get("source") not in raster]
    sprite_src = s["sprite"]
    os.makedirs(os.path.join(BASE, "sprites"), exist_ok=True)
    for suffix in ("json", "png"):
        for scale in ("", "@2x"):
            r = c.get(f"{sprite_src}{scale}.{suffix}")
            r.raise_for_status()
            open(os.path.join(BASE, "sprites", f"ofm{scale}.{suffix}"), "wb").write(r.content)
    n = 0
    for font in FONTS:
        d = os.path.join(BASE, "fonts", font)
        os.makedirs(d, exist_ok=True)
        for start in range(0, 65536, 256):
            rng = f"{start}-{start + 255}"
            dest = os.path.join(d, f"{rng}.pbf")
            if os.path.exists(dest):
                continue
            r = c.get(f"{UPSTREAM}/fonts/{font}/{rng}.pbf")
            if r.status_code == 200:
                open(dest, "wb").write(r.content)
                n += 1
    # style.json last: its presence means "assets complete" (the style endpoint 404s until then)
    tmp = os.path.join(BASE, "style.json.tmp")
    json.dump(s, open(tmp, "w"))
    os.replace(tmp, os.path.join(BASE, "style.json"))
    print(f"[basemap] style: {len(s['layers'])} layers (dropped raster {sorted(raster)}); sprites ok; {n} glyph files", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch-assets"]:
        fetch_assets()
    else:
        print(__doc__)

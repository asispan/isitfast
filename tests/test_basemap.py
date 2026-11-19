"""Basemap endpoints serve tiles out of a PMTiles archive with CDN-friendly headers."""
import gzip
import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="isitfast-basemap-")
os.environ["DATA_DIR"] = TMP
os.environ.setdefault("IP_SALT", "test-salt-0123456789")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pmtiles.tile import Compression, TileType, zxy_to_tileid  # noqa: E402
from pmtiles.writer import Writer  # noqa: E402

from app import basemap  # noqa: E402


def build_archive(path):
    with open(path, "wb") as f:
        w = Writer(f)
        w.write_tile(zxy_to_tileid(0, 0, 0), gzip.compress(b"tile-0"))
        w.write_tile(zxy_to_tileid(14, 11723, 7596), gzip.compress(b"blr-tile"))
        w.finalize(
            {"tile_type": TileType.MVT, "tile_compression": Compression.GZIP, "min_zoom": 0, "max_zoom": 14,
             "min_lon_e7": 774500000, "min_lat_e7": 128000000, "max_lon_e7": 778000000, "max_lat_e7": 131500000,
             "center_zoom": 12, "center_lon_e7": 776000000, "center_lat_e7": 129700000},
            {"vector_layers": [{"id": "poi"}]},
        )


def client():
    os.makedirs(basemap.BASE, exist_ok=True)
    build_archive(basemap.ARCHIVE)
    json.dump({"version": 8, "sources": {"openmaptiles": {"type": "vector", "url": "x"}}, "layers": [],
               "glyphs": "x", "sprite": "x"}, open(os.path.join(basemap.BASE, "style.json"), "w"))
    app = FastAPI()
    app.include_router(basemap.router)
    return TestClient(app)


def test_tiles_style_and_tilejson():
    c = client()
    r = c.get("/basemap/tiles/14/11723/7596.pbf")
    assert r.status_code == 200 and r.content == b"blr-tile"  # TestClient transparently gunzips
    assert r.headers["content-encoding"] == "gzip" and "s-maxage" in r.headers["cache-control"]
    empty = c.get("/basemap/tiles/14/0/0.pbf")
    assert empty.status_code == 200 and empty.content == b""
    assert c.get("/basemap/tiles/15/0/0.pbf").status_code == 404   # beyond max zoom
    assert c.get("/basemap/tiles/3/9/0.pbf").status_code == 404    # x out of range
    tj = c.get("/basemap/tiles.json").json()
    assert tj["maxzoom"] == 14 and tj["tiles"][0].endswith("/basemap/tiles/{z}/{x}/{y}.pbf")
    st = c.get("/basemap/style.json").json()
    assert st["sources"]["openmaptiles"]["url"].endswith("/basemap/tiles.json")
    assert st["glyphs"].endswith("/basemap/fonts/{fontstack}/{range}.pbf")

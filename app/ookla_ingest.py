"""Ingest Ookla Open Data (Speedtest by Ookla Global Fixed and Mobile Network Performance Maps)
into a compact SQLite lookup table keyed by zoom-16 quadkey.

Source: s3://ookla-open-data (public, no auth). License: CC BY-NC-SA 4.0 — attribution required.

The live ookla.db is never written in place: each run builds a complete new database next to it
(copying the other kind's rows across) and swaps it in with an atomic os.replace(), so readers never
see a half-written table and the app never blocks on the ingest's write lock.

Only tiles inside INGEST_BBOX (south,west,north,east; default: India) are kept.

Usage: python -m app.ookla_ingest [--kind mobile|fixed|all] [--year 2026 --quarter 3]
With no year/quarter it probes for the most recent published quarter.
"""
import argparse
import datetime as dt
import email.utils
import os
import sqlite3
import sys
import time

import duckdb
import httpx

BASE = "https://ookla-open-data.s3.amazonaws.com/parquet/performance"
DATA_DIR = os.environ.get("DATA_DIR", "/data")
OOKLA_DB = os.path.join(DATA_DIR, "ookla.db")
QUARTER_START = {1: "01-01", 2: "04-01", 3: "07-01", 4: "10-01"}
INDIA = (6.0, 68.0, 37.5, 97.5)


def ingest_bbox() -> tuple[float, float, float, float]:
    raw = os.environ.get("INGEST_BBOX")
    return tuple(float(x) for x in raw.split(",")) if raw else INDIA  # type: ignore[return-value]


def url_for(kind: str, year: int, q: int) -> str:
    return f"{BASE}/type={kind}/year={year}/quarter={q}/{year}-{QUARTER_START[q]}_performance_{kind}_tiles.parquet"


def latest_quarter(kind: str) -> tuple[int, int, str, str]:
    today = dt.date.today()
    year, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(8):
        u = url_for(kind, year, q)
        r = httpx.head(u, timeout=30)
        if r.status_code == 200:
            return year, q, u, r.headers.get("last-modified", "")
        q -= 1
        if q == 0:
            year, q = year - 1, 4
    raise SystemExit(f"no recent {kind} quarter found")


SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
  kind TEXT NOT NULL, quadkey TEXT NOT NULL,
  d_kbps INTEGER, u_kbps INTEGER, lat_ms INTEGER, tests INTEGER, devices INTEGER,
  PRIMARY KEY (kind, quadkey)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (
  kind TEXT PRIMARY KEY, year INTEGER, quarter INTEGER, source_url TEXT,
  published TEXT, ingested_at TEXT, rows INTEGER);
"""


def current(kind: str) -> tuple | None:
    if not os.path.exists(OOKLA_DB):
        return None
    c = sqlite3.connect(f"file:{OOKLA_DB}?mode=ro", uri=True)
    try:
        return c.execute("SELECT year, quarter FROM meta WHERE kind=?", (kind,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        c.close()


def ingest(kind: str, year: int | None, quarter: int | None, force: bool = False) -> None:
    if year and quarter:
        u = url_for(kind, year, quarter)
        lm = httpx.head(u, timeout=30).headers.get("last-modified", "")
    else:
        year, quarter, u, lm = latest_quarter(kind)
    published = email.utils.parsedate_to_datetime(lm).date().isoformat() if lm else ""

    os.makedirs(DATA_DIR, exist_ok=True)
    if current(kind) == (year, quarter) and not force:
        print(f"[{kind}] {year}Q{quarter} already ingested — skipping")
        return

    tmp = os.path.join(DATA_DIR, f".{kind}-{year}q{quarter}.parquet")
    print(f"[{kind}] downloading {u}", flush=True)
    t0 = time.time()
    with httpx.stream("GET", u, timeout=None, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    print(f"[{kind}] downloaded {os.path.getsize(tmp)/1e6:.0f} MB in {time.time()-t0:.0f}s", flush=True)

    build = OOKLA_DB + ".build"
    for p in (build, build + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    db = sqlite3.connect(build)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.executescript(SCHEMA)
    if os.path.exists(OOKLA_DB):  # carry the other kind's rows (and meta) over unchanged
        db.execute("ATTACH ? AS old", (OOKLA_DB,))  # only read from
        try:
            db.execute("INSERT INTO tiles SELECT * FROM old.tiles WHERE kind != ?", (kind,))
            db.execute("INSERT INTO meta SELECT * FROM old.meta WHERE kind != ?", (kind,))
        except sqlite3.OperationalError:
            pass
        db.commit()
        db.execute("DETACH old")

    s, w, n_, e = ingest_bbox()
    con = duckdb.connect()
    rel = con.execute(
        "SELECT quadkey, avg_d_kbps, avg_u_kbps, avg_lat_ms, tests, devices FROM read_parquet(?) "
        "WHERE tile_y BETWEEN ? AND ? AND tile_x BETWEEN ? AND ?",
        [tmp, s, n_, w, e],
    )
    n = 0
    while True:
        rows = rel.fetchmany(200_000)
        if not rows:
            break
        db.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?,?,?,?)", [(kind, *r) for r in rows])
        n += len(rows)
        print(f"[{kind}] {n:,} rows", flush=True)
    db.execute(
        "INSERT OR REPLACE INTO meta VALUES (?,?,?,?,?,?,?)",
        (kind, year, quarter, u, published, dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", n),
    )
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(build, OOKLA_DB)  # atomic: readers see the old file or the new one, never a mix
    os.remove(tmp)
    print(f"[{kind}] done: {n:,} tiles for {year}Q{quarter} (published {published})", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="all", choices=["mobile", "fixed", "all"])
    ap.add_argument("--year", type=int)
    ap.add_argument("--quarter", type=int)
    ap.add_argument("--force", action="store_true", help="rebuild even if this quarter is already ingested")
    a = ap.parse_args()
    for k in (["mobile", "fixed"] if a.kind == "all" else [a.kind]):
        ingest(k, a.year, a.quarter, a.force)


if __name__ == "__main__":
    sys.exit(main())

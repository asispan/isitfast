"""Ingest M-Lab Monthly Stats (city x ASN) for Indian mobile carriers into carriers.db.

Source: M-Lab NDT "Monthly Stats" parquet (CC0), manifest in github.com/m-lab/iqb.
City coordinates: GeoNames cities5000 (CC BY 4.0) so a cafe can be matched to the nearest M-Lab city.

Usage: python -m app.mlab_ingest [--months 3]
"""
import argparse
import csv
import datetime as dt
import io
import os
import sqlite3
import unicodedata
import zipfile

import duckdb
import httpx

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB = os.path.join(DATA_DIR, "carriers.db")
MANIFEST = "https://raw.githubusercontent.com/m-lab/iqb/main/data/state/ghremote/manifest.json"
GEONAMES = "https://download.geonames.org/export/dump/cities5000.zip"
ADMIN1 = "https://download.geonames.org/export/dump/admin1CodesASCII.txt"

# Mobile-network ASNs only. Jio's 55836 also carries JioFiber (M-Lab can't split them).
# BSNL (AS9829) is excluded: it is overwhelmingly fixed broadband.
CARRIERS = {
    "IN": {
        "Jio": [55836],
        "Airtel": [45609],
        "Vi": [38266, 45271, 55410],
    }
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return "".join(ch for ch in s.lower() if ch.isalnum())


def load_geonames(country: str) -> dict[str, list[tuple]]:
    """name -> [(lat, lon, admin1_name_norm, population)]"""
    a1 = {}
    for line in httpx.get(ADMIN1, timeout=60).text.splitlines():
        code, name, ascii_name, _ = line.split("\t")
        if code.startswith(country + "."):
            a1[code.split(".")[1]] = norm(ascii_name)
    z = zipfile.ZipFile(io.BytesIO(httpx.get(GEONAMES, timeout=120, follow_redirects=True).content))
    idx: dict[str, list[tuple]] = {}
    with z.open("cities5000.txt") as f:
        for row in csv.reader(io.TextIOWrapper(f, "utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE):
            if row[8] != country:
                continue
            rec = (float(row[4]), float(row[5]), a1.get(row[10], ""), int(row[14] or 0))
            names = {norm(row[1]), norm(row[2])} | {norm(x) for x in row[3].split(",") if x}
            for n in names:
                if n:
                    idx.setdefault(n, []).append(rec)
    return idx


def locate(idx, city: str, region: str):
    cands = idx.get(norm(city)) or []
    if not cands:
        return None
    same = [c for c in cands if c[2] and c[2] == norm(region)]
    best = max(same or cands, key=lambda c: c[3])
    return best[0], best[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    a = ap.parse_args()

    files = httpx.get(MANIFEST, timeout=60).json()["files"]
    months = sorted({k.split("/")[2] for k in files if "downloads_by_country_city_asn/data.parquet" in k})[-a.months:]
    print(f"[mlab] months: {months}", flush=True)
    if os.path.exists(DB):
        try:
            c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            have = dict(c.execute("SELECT k, v FROM meta").fetchall())
            c.close()
        except sqlite3.Error:
            have = {}
        latest = dt.datetime.strptime(months[-1][:8], "%Y%m%d").date().isoformat()
        if have.get("period_end_month") == latest and have.get("months") == str(a.months):
            print(f"[mlab] {latest} already ingested — skipping", flush=True)
            return

    os.makedirs(DATA_DIR, exist_ok=True)
    con = duckdb.connect()
    rows = {}
    for m in months:
        start = [k for k in files if k.split("/")[2] == m][0].split("/")[2]
        for kind in ("downloads", "uploads"):
            key = next(k for k in files if k.split("/")[2] == m and f"/{kind}_by_country_city_asn/data.parquet" in k)
            path = os.path.join(DATA_DIR, f".mlab-{kind}.parquet")
            with httpx.stream("GET", files[key]["url"], timeout=None) as r, open(path, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
            for country, carriers in CARRIERS.items():
                for carrier, asns in carriers.items():
                    col = "download_p50, latency_p50" if kind == "downloads" else "upload_p50, NULL"
                    q = (f"SELECT city, subdivision1_name, asn, sample_count, {col} FROM read_parquet(?) "
                         f"WHERE country_code=? AND asn IN ({','.join(map(str, asns))}) AND city IS NOT NULL")
                    for city, region, asn, n, v, lat in con.execute(q, [path, country]).fetchall():
                        if v is None:
                            continue
                        r_ = rows.setdefault((country, region, city, carrier), {"dn": 0, "d": 0.0, "l": 0.0, "un": 0, "u": 0.0})
                        if kind == "downloads":
                            r_["dn"] += n; r_["d"] += v * n; r_["l"] += (lat or 0) * n
                        else:
                            r_["un"] += n; r_["u"] += v * n
            os.remove(path)
        print(f"[mlab] {start}: {len(rows)} city/carrier rows so far", flush=True)

    geo = {c: load_geonames(c) for c in CARRIERS}
    # Build a fresh database beside the live one and swap it in atomically (never DROP under readers).
    build = DB + ".build"
    if os.path.exists(build):
        os.remove(build)
    db = sqlite3.connect(build)
    db.executescript(
        """
        CREATE TABLE city_carrier (country TEXT, region TEXT, city TEXT, carrier TEXT, lat REAL, lon REAL,
          tests INTEGER, down_mbps REAL, up_mbps REAL, latency_ms REAL);
        CREATE INDEX cc_latlon ON city_carrier(lat, lon);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    unmatched = 0
    out = []
    for (country, region, city, carrier), r in rows.items():
        if r["dn"] < 30:
            continue
        ll = locate(geo[country], city, region)
        if not ll:
            unmatched += 1
            continue
        out.append((country, region, city, carrier, ll[0], ll[1], r["dn"], round(r["d"] / r["dn"], 1),
                    round(r["u"] / r["un"], 1) if r["un"] else None, round(r["l"] / r["dn"], 1)))
    db.executemany("INSERT INTO city_carrier VALUES (?,?,?,?,?,?,?,?,?,?)", out)
    first = dt.datetime.strptime(months[0][:8], "%Y%m%d").date()
    last_start = dt.datetime.strptime(months[-1][:8], "%Y%m%d").date()
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("period_start", first.isoformat()), ("period_end_month", last_start.isoformat()), ("months", str(a.months)),
        ("ingested_at", dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"),
    ])
    db.commit()
    db.close()
    if len(out) < 10:  # a broken upstream month must not wipe the live table
        raise SystemExit(f"[mlab] only {len(out)} rows — keeping the current carriers.db")
    os.replace(build, DB)
    print(f"[mlab] wrote {len(out)} rows ({unmatched} unmatched cities)", flush=True)


if __name__ == "__main__":
    main()

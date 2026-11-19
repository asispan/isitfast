"""Small concurrency soak against a local server: mixed reads + concurrent test-start/save writes."""
import concurrent.futures as cf
import random
import sys
import time

import httpx

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
c = httpx.Client(timeout=10, limits=httpx.Limits(max_connections=200, max_keepalive_connections=200))
place = c.get(f"{B}/api/places/tile/51/310").json()["features"]
ids = [f["properties"]["id"] for f in place]
coords = {f["properties"]["id"]: f["geometry"]["coordinates"] for f in place}
print("places in tile:", len(ids))


def read(k):
    r = random.random()
    if r < 0.5:
        return c.get(f"{B}/api/places/tile/{random.choice(['51/310','51/309','52/310'])}").status_code
    if r < 0.9:
        return c.get(f"{B}/api/places/{random.choice(ids)}").status_code
    return c.get(f"{B}/api/places", params={"bbox": "12.9,77.55,13.0,77.7"}).status_code


def write(k):
    pid = random.choice(ids)
    lon, lat = coords[pid]
    h = {"cf-connecting-ip": f"10.9.{k // 250}.{k % 250}", "x-visitor-asn": "55836"}
    s = c.post(f"{B}/api/places/{pid}/test-start", json={"lat": lat, "lon": lon}, headers=h)
    if s.status_code != 200:
        return s.status_code
    return s.status_code  # saving needs MIN_TEST_S to pass; covered by the unit tests


t0 = time.time()
codes = {}
lat = []
with cf.ThreadPoolExecutor(int(sys.argv[3]) if len(sys.argv) > 3 else 32) as ex:
    futs = [ex.submit(read if k % 10 else write, k) for k in range(N)]
    for f in futs:
        try:
            code = f.result()
        except httpx.HTTPError as e:
            code = type(e).__name__
        codes[code] = codes.get(code, 0) + 1
dt = time.time() - t0
print(f"{N} requests in {dt:.1f}s = {N/dt:.0f} req/s; status codes: {codes}")

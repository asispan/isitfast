"""OpenStreetMap places via Overpass, cached per grid cell in SQLite: cafes and coworking spaces, plus named
restaurants, offices and apartment buildings so every place you might work from shows as a dot on the map."""
import asyncio
import json
import math
import time

import httpx

KIND_LABEL = {"cafe": "Cafe", "coworking_space": "Coworking", "coworking": "Coworking", "restaurant": "Restaurant",
              "office": "Office", "apartments": "Apartments"}


def cells_for_bbox(s: float, w: float, n: float, e: float, size: float) -> list[tuple[int, int]]:
    i0, i1 = math.floor(s / size), math.floor(n / size)
    j0, j1 = math.floor(w / size), math.floor(e / size)
    return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]


def cell_key(i: int, j: int) -> str:
    return f"{i}:{j}"


def overpass_query(s: float, w: float, n: float, e: float) -> str:
    bb = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    return f"""[out:json][timeout:180];
(
  nwr["amenity"="cafe"]{bb};
  nwr["amenity"="coworking_space"]{bb};
  nwr["office"="coworking"]{bb};
  nwr["amenity"~"^(restaurant|fast_food)$"]["name"]{bb};
  nwr["office"]["name"]{bb};
  nwr["building"="apartments"]["name"]{bb};
  nwr["residential"="apartments"]["name"]{bb};
);
out center tags;"""


def parse_elements(elements: list[dict]) -> list[dict]:
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name") or tags.get("name:en") or tags.get("brand")
        if not name:
            continue
        if "lat" in el:
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        kind = kind_of(tags)
        if not kind:
            continue
        out.append({"id": f"{el['type'][0]}{el['id']}", "name": name, "kind": kind, "lat": lat, "lon": lon, "tags": tags})
    return out


def kind_of(tags: dict) -> str | None:
    """One kind per place, most specific first (a cafe inside an office building is a cafe)."""
    if tags.get("amenity") == "coworking_space" or tags.get("office") == "coworking":
        return "coworking"
    if tags.get("amenity") == "cafe":
        return "cafe"
    if tags.get("amenity") in ("restaurant", "fast_food"):
        return "restaurant"
    if tags.get("office") and tags.get("office") != "no":
        return "office"
    if tags.get("building") == "apartments" or tags.get("residential") == "apartments":
        return "apartments"
    return None


async def fetch_overpass(endpoints: list[str], ua: str, s, w, n, e) -> list[dict]:
    q = overpass_query(s, w, n, e)
    last = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), headers={"User-Agent": ua}) as client:
        for ep in endpoints:
            for attempt in range(2):
                try:
                    r = await client.post(ep, data={"data": q})
                    if r.status_code == 200:
                        return parse_elements(r.json().get("elements", []))
                    last = f"{ep} -> {r.status_code}"
                    if r.status_code in (429, 504):
                        await asyncio.sleep(2 + attempt * 3)
                        continue
                    break
                except (httpx.HTTPError, ValueError) as ex:  # network / bad json
                    last = f"{ep} -> {ex!r}"
                    break
    raise RuntimeError(f"overpass failed: {last}")


def public_tags(tags: dict) -> dict:
    keep = [
        "addr:housenumber", "addr:street", "addr:suburb", "addr:city", "addr:postcode",
        "opening_hours", "website", "contact:website", "phone", "contact:phone",
        "internet_access", "internet_access:fee", "internet_access:ssid", "cuisine",
        "outdoor_seating", "wheelchair", "brand", "description", "check_date",
    ]
    return {k: tags[k] for k in keep if k in tags}

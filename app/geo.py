"""Small geo helpers: Bing/Ookla quadkeys and distances."""
import math

OOKLA_ZOOM = 16  # Ookla open data tiles are zoom-16 web-mercator tiles (~610 m at the equator)


def latlon_to_tile(lat: float, lon: float, z: int = OOKLA_ZOOM) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    s = math.sin(math.radians(lat))
    y = int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_to_quadkey(x: int, y: int, z: int = OOKLA_ZOOM) -> str:
    out = []
    for i in range(z, 0, -1):
        d = 0
        mask = 1 << (i - 1)
        if x & mask:
            d += 1
        if y & mask:
            d += 2
        out.append(str(d))
    return "".join(out)


def quadkey(lat: float, lon: float, z: int = OOKLA_ZOOM) -> str:
    return tile_to_quadkey(*latlon_to_tile(lat, lon, z), z)


def neighbour_quadkeys(lat: float, lon: float, radius: int = 1, z: int = OOKLA_ZOOM) -> list[str]:
    x, y = latlon_to_tile(lat, lon, z)
    n = 1 << z
    keys = []
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            xx, yy = x + dx, y + dy
            if 0 <= xx < n and 0 <= yy < n:
                keys.append(tile_to_quadkey(xx, yy, z))
    return keys


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

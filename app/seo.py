"""What search engines and AI answer engines read: server-rendered place summaries, JSON-LD, robots.txt,
sitemap.xml, llms.txt, and a log of AI-crawler visits.

Most AI fetchers (GPTBot, ClaudeBot, PerplexityBot, ChatGPT-User, ...) don't run JavaScript, so the numbers
must be in the HTML itself. Every visitor gets the same HTML; the app's JS then replaces the summary with the
interactive panel showing the same figures (no user-agent branching, so no cloaking).

Pure functions only: main.py passes in rows and connections. Nothing here imports main.
"""
import datetime
import html
import json
import re
import statistics
import time

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
WINDOW_DAYS = 90         # medians and ranking use verified tests from this window
SESSION_S = 3600         # tests from one connection within an hour count as one reading
RANK_MIN_READINGS = 3    # a place can be ranked only with this many verified readings...
RANK_MIN_DAYS = 2        # ...on at least this many different days...
RANK_MIN_DEVICES = 2     # ...from at least this many connections, the latest within WINDOW_DAYS
KIND_SCHEMA = {"cafe": "CafeOrCoffeeShop", "restaurant": "Restaurant", "coworking": "LocalBusiness",
               "coworking_space": "LocalBusiness"}
INDEX_DAYS = 180         # a page stays indexed while it has a verified test this recent (90-180 days = stale)
KIND_LABEL = {"cafe": "Cafe", "coworking": "Coworking space", "coworking_space": "Coworking space",
              "restaurant": "Restaurant", "office": "Office"}
WORK_KINDS = {"cafe", "coworking", "coworking_space"}  # places people work from; get pages without a venue test
TRAI_MIN_RES = 7         # H3 res 7 is ~2.8 km across; coarser hexes describe a district, not a café's surroundings
TRAI_MIN_CARRIERS = 2    # a page needs at least this many carriers measured that finely
AREA_MIN_PLACES = 5      # an area page needs this many cafes/coworking spaces...
RANK_MIN_TESTS = 100     # TRAI: name a "highest median" carrier only with this many area tests each...
RANK_MIN_LEAD = 1.2      # ...and a 20% lead over the next carrier, at the same hexagon size
VENUE_FACTS = ("internet_access", "opening_hours", "addr:street", "addr:housenumber", "website", "contact:website")
SITE_DESCRIPTION = ("Cafe and coworking WiFi and Jio/Airtel/Vi mobile data speeds in India, tested on site, plus "
                    "TRAI MySpeed medians for the surrounding area.")
SITE = "isitfast.space"  # default host name in quotable text (main passes the canonical host)
CITY_LABEL = {"bengaluru": "Bengaluru", "mumbai": "Mumbai", "delhi-ncr": "Delhi NCR", "hyderabad": "Hyderabad",
              "chennai": "Chennai", "pune": "Pune", "kolkata": "Kolkata", "goa": "Goa"}


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def fmt_date(ts: int) -> str:
    """Absolute date in IST, e.g. "4 Nov 2026" (relative dates go stale in caches and AI answers)."""
    d = datetime.datetime.fromtimestamp(ts, IST)
    return f"{d.day} {d.strftime('%b %Y')}"


def iso(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, IST).isoformat(timespec="seconds")


def mbps(v: float) -> str:
    return f"{v:.0f}" if v >= 10 else f"{v:.1f}"


def city_key_for(lat: float, lon: float) -> str | None:
    from .seed import CITIES  # noqa: PLC0415 — seed imports main, so import late
    for key, (s, w, n, e) in CITIES.items():
        if s <= lat <= n and w <= lon <= e:
            return key
    return None


def city_label(key: str) -> str:
    return CITY_LABEL.get(key, key.replace("-", " ").title())


def city_for(lat: float, lon: float) -> str | None:
    key = city_key_for(lat, lon)
    return city_label(key) if key else None


def locality(tags: dict, lat: float, lon: float, area: str | None = None) -> str:
    """"Koramangala, Bengaluru": the place's own OSM suburb, else our nearest neighbourhood, plus its city."""
    city = city_for(lat, lon) or tags.get("addr:city")
    area = tags.get("addr:suburb") or area or tags.get("addr:street")
    return ", ".join(x for x in (area, city) if x)


# ---------------------------------------------------------------- statistics
def readings(rows: list) -> list[dict]:
    """Verified tests in the window, with each connection's tests within SESSION_S collapsed to one reading
    (their median), so one person re-running the test can't outvote everyone else."""
    out: list[dict] = []
    open_: dict[str, dict] = {}
    for t in sorted(rows, key=lambda r: r["created_at"]):
        key = t["ip_hash"] or f"?{t['created_at']}"  # no hash: its own reading, not counted as a device
        cur = open_.get(key)
        if cur and t["created_at"] - cur["start"] <= SESSION_S:
            cur["d"].append(t["down_mbps"]); cur["u"].append(t["up_mbps"]); cur["ts"] = t["created_at"]
            continue
        cur = open_[key] = {"start": t["created_at"], "ts": t["created_at"], "dev": key,
                            "d": [t["down_mbps"]], "u": [t["up_mbps"]]}
        out.append(cur)
    return [{"ts": r["ts"], "dev": r["dev"], "down": statistics.median(r["d"]), "up": statistics.median(r["u"])}
            for r in out]


def _date(ts: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(ts, IST).date()


def group_stats(rows: list, now: int) -> dict:
    """One network at one place (WiFi from one ISP, or one carrier's mobile data). `rows` are newest first."""
    latest = rows[0]
    lv = next((t for t in rows if t["verified"]), None)
    recent = [t for t in rows if t["verified"] and t["created_at"] >= now - WINDOW_DAYS * 86400]
    rd = readings(recent)
    out = {
        "tests": len(rows),
        "latest": {"down": latest["down_mbps"], "up": latest["up_mbps"], "ts": latest["created_at"],
                   "verified": bool(latest["verified"])},
        "latest_verified": {"down": lv["down_mbps"], "up": lv["up_mbps"], "ts": lv["created_at"]} if lv else None,
        "verified": None,
        "rankable": False,
    }
    if rd:
        days = {_date(r["ts"]) for r in rd}
        out["verified"] = {
            "readings": len(rd), "tests": len(recent),
            "down": statistics.median(r["down"] for r in rd), "up": statistics.median(r["up"] for r in rd),
            "down_min": min(r["down"] for r in rd), "down_max": max(r["down"] for r in rd),
            "first": min(r["ts"] for r in rd), "last": max(r["ts"] for r in rd), "days": len(days),
            "devices": len({r["dev"] for r in rd if not r["dev"].startswith("?")}),
        }
        out["rankable"] = (len(rd) >= RANK_MIN_READINGS and len(days) >= RANK_MIN_DAYS
                           and out["verified"]["devices"] >= RANK_MIN_DEVICES)
    return out


def place_groups(tests: list, now: int | None = None) -> list[dict]:
    """Per-network summaries for a place, WiFi first (by ISP), then mobile data by carrier. `tests` newest first,
    with columns network, carrier, down_mbps, up_mbps, created_at, verified, ip_hash."""
    now = int(now if now is not None else time.time())
    by: dict[tuple[str, str], list] = {}
    for t in tests:
        by.setdefault((t["network"], t["carrier"] or "Unknown"), []).append(t)
    groups = []
    for (net, who), rows in by.items():
        label = f"WiFi ({who})" if net == "wifi" else f"{who} mobile data"
        groups.append({"network": net, "provider": who, "label": label, **group_stats(rows, now)})
    # WiFi first; within a network, the provider with the most verified evidence, then the most recent.
    groups.sort(key=lambda g: (g["network"] != "wifi", -((g["verified"] or {}).get("readings", 0)),
                               -((g["latest_verified"] or {}).get("ts", 0)), -g["latest"]["ts"]))
    return groups


def headline_groups(groups: list[dict]) -> list[dict]:
    """The groups a one-line summary quotes: the best-evidenced WiFi and mobile network with a verified test."""
    out = []
    for net in ("wifi", "cellular"):
        g = next((g for g in groups if g["network"] == net and g["latest_verified"]), None)
        if g:
            out.append(g)
    return out


OSM_ID = re.compile(r"^[nwr]\d+$")


def venue_facts(tags: dict) -> bool:
    return any(tags.get(k) for k in VENUE_FACTS)


def indexable(place: dict, groups: list[dict], residential: set[str], now: int | None = None,
              local: dict | None = None) -> bool:
    """A place page is indexed only with an OpenStreetMap name (Google rows keep none; names typed by visitors
    are unmoderated), a non-residential kind (homes stay out of search), not closed, plus either a verified on-site
    test in INDEX_DAYS or, for a cafe/coworking space, TRAI medians for its surroundings AND at least one fact
    about the venue itself (address, hours, website, WiFi tag) — so no page is only a hexagon's numbers."""
    now = int(now if now is not None else time.time())
    if not (place.get("name") and OSM_ID.match(place.get("id") or "") and place.get("kind") not in residential):
        return False
    if any(g["latest_verified"] and g["latest_verified"]["ts"] >= now - INDEX_DAYS * 86400 for g in groups):
        return True
    return (place.get("kind") in WORK_KINDS and not place.get("gone_at") and trai_ok(local)
            and venue_facts(place.get("tags") or {}))


# ---------------------------------------------------------------- TRAI MySpeed area medians (app/local_carrier.py)
def trai_rows(local: dict | None, min_res: int = 0) -> list[dict]:
    """Carriers with a median download, fastest first: [{carrier, down, up, tests, across_km, res}]. Text uses
    min_res=TRAI_MIN_RES (no 7-20 km hexagons beside a cafe's name); tables show every row with its size."""
    out = [{"carrier": k, "down": v["down_mbps"], "up": v.get("up_mbps"), "tests": v.get("tests", 0),
            "across_km": v.get("across_km"), "res": v.get("h3_res", 0),
            "start": v.get("period_start"), "end": v.get("period_end")}
           for k, v in (local or {}).items() if v.get("down_mbps") is not None and v.get("h3_res", 0) >= min_res
           and v.get("across_km")]
    return sorted(out, key=lambda r: -r["down"])


def trai_ok(local: dict | None) -> bool:
    return sum(r["res"] >= TRAI_MIN_RES for r in trai_rows(local)) >= TRAI_MIN_CARRIERS


def _iso_day(v: str | None) -> str:
    try:
        d = datetime.date.fromisoformat(v or "")
        return f"{d.day} {d.strftime('%b %Y')}"
    except ValueError:
        return ""


def trai_size(rows: list[dict]) -> str:
    sizes = sorted({r["across_km"] for r in rows})
    return f"~{sizes[0]} km" if len(sizes) == 1 else f"~{sizes[0]}–{sizes[-1]} km"


def trai_period(rows: list[dict]) -> str:
    starts, ends = [r["start"] for r in rows if r["start"]], [r["end"] for r in rows if r["end"]]
    return f"{_iso_day(min(starts))} to {_iso_day(max(ends))}" if starts and ends else ""


def trai_leader(rows: list[dict]) -> dict | None:
    """The carrier with clearly the highest median, or None: same hexagon size, enough tests each, 20% lead."""
    if len(rows) < 2 or len({r["res"] for r in rows}) != 1 or min(r["tests"] for r in rows) < RANK_MIN_TESTS:
        return None
    return rows[0] if rows[0]["down"] >= RANK_MIN_LEAD * rows[1]["down"] else None


def trai_sentence(local: dict | None, where: str = "it", unit: str = "the venue") -> str:
    """Scoped before the numbers, so a cut-off quote still says it is area data, not the venue."""
    rows = trai_rows(local, TRAI_MIN_RES)
    if not rows:
        return ""
    nums = ", ".join(f"{r['carrier']} {mbps(r['down'])} Mbps ({r['tests']:,} area tests)" for r in rows)
    lead = trai_leader(rows)
    best = f"; {lead['carrier']} had the highest median" if lead else ""
    period = trai_period(rows)
    return (f"In the {trai_size(rows)} area around {where} (TRAI MySpeed, {period}; not measured at {unit}), "
            f"median mobile download: {nums}{best}.")


def trai_table_html(local: dict | None, caption: str) -> str:
    rows = trai_rows(local)
    if not rows:
        return ""
    body = "".join(
        f"<tr><th scope=\"row\">{esc(r['carrier'])}</th><td>{mbps(r['down'])}</td>"
        f"<td>{mbps(r['up']) if r['up'] is not None else '—'}</td><td>{r['tests']:,}</td>"
        f"<td>~{r['across_km']} km</td></tr>" for r in rows)
    return (f"<table><caption>{esc(caption)}</caption><thead><tr><th scope=\"col\">Carrier</th>"
            "<th scope=\"col\">Median download (Mbps)</th><th scope=\"col\">Median upload (Mbps)</th>"
            "<th scope=\"col\">Tests</th><th scope=\"col\">Area across</th></tr></thead>"
            f"<tbody>{body}</tbody></table>")


# ---------------------------------------------------------------- text
def answer_sentence(name: str, where: str, groups: list[dict], site: str = SITE, local: dict | None = None) -> str:
    """One self-contained, dated sentence an answer engine can quote without losing the caveats. Verified tests
    only (unverified ones are in the table, labelled), then TRAI's medians for the surroundings, named as such."""
    head = f"{name}{', ' + where if where else ''}"
    around = trai_sentence(local, name)
    if not groups:
        return (f"{head}: WiFi not tested on site yet. {around}" if around
                else f"{head}: no one has tested the internet here yet.")
    parts = []
    for g in headline_groups(groups):
        lv, v = g["latest_verified"], g["verified"]
        kind = "WiFi" if g["network"] == "wifi" else f"{g['provider']} mobile data"
        s = f"{kind} {mbps(lv['down'])} Mbps download / {mbps(lv['up'])} Mbps upload (latest verified test, {fmt_date(lv['ts'])})"
        if v and v["readings"] >= 2:
            s += (f", median of {v['readings']} verified readings from {fmt_date(v['first'])} to {fmt_date(v['last'])}: "
                  f"{mbps(v['down'])} / {mbps(v['up'])} Mbps")
        parts.append(s)
    if not parts:
        return f"{head}: only unverified speed tests so far ({site}).{' ' + around if around else ''}"
    return (f"{head}: " + "; ".join(parts) + f". Measured with in-browser speed tests run within 50 m of the place "
            f"({site}).{' ' + around if around else ''}")


def place_summary_html(place: dict, where: str, groups: list[dict], site: str = SITE, local: dict | None = None,
                       area: dict | None = None) -> str:
    """The server-rendered summary inside #panel. The app replaces it with the interactive panel."""
    name = place.get("name") or "This place"
    kind = KIND_LABEL.get(place.get("kind") or "", (place.get("kind") or "Place").replace("_", " ").capitalize())
    rows = []
    for g in groups:
        lt, v = g["latest"], g["verified"]
        med = (f"{mbps(v['down'])} / {mbps(v['up'])} <small>({v['readings']} reading{'s' if v['readings'] != 1 else ''}"
               + (f", range {mbps(v['down_min'])}–{mbps(v['down_max'])}, {fmt_date(v['first'])}–{fmt_date(v['last'])}"
                  if v['readings'] > 1 else f", {fmt_date(v['last'])}") + ")</small>") if v else "—"
        rows.append(
            f"<tr><th scope=\"row\">{esc(g['label'])}</th>"
            f"<td>{mbps(lt['down'])} / {mbps(lt['up'])}{'' if lt['verified'] else ' <small>(unverified)</small>'}</td>"
            f"<td>{med}</td><td>{g['tests']}</td>"
            f"<td><time datetime=\"{iso(lt['ts'])}\">{fmt_date(lt['ts'])}</time></td></tr>")
    table = (
        "<table><caption>Internet speeds measured at this place (download / upload, Mbps)</caption>"
        "<thead><tr><th scope=\"col\">Network</th><th scope=\"col\">Latest test</th>"
        f"<th scope=\"col\">Median of verified readings (past {WINDOW_DAYS} days)</th><th scope=\"col\">Tests</th>"
        "<th scope=\"col\">Last tested</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    ) if rows else "<p>No one has tested here yet. Be the first: a 20-second speed test, run at the place.</p>"
    wifi = (place.get("tags") or {}).get("internet_access")
    tags = place.get("tags") or {}
    facts = []
    if wifi in ("wlan", "yes", "wifi") and not any(g["network"] == "wifi" for g in groups):
        facts.append("OpenStreetMap lists WiFi here (untested; its speed is unknown until someone tests it)")
    elif wifi == "no":
        facts.append("OpenStreetMap lists no WiFi here")
    addr = ", ".join(x for x in (" ".join(y for y in (tags.get("addr:housenumber"), tags.get("addr:street")) if y),
                                 tags.get("addr:suburb"), tags.get("addr:postcode")) if x)
    if addr:
        facts.append(f"Address (OpenStreetMap): {addr}")
    if tags.get("opening_hours"):
        facts.append(f"Opening hours (OpenStreetMap): {tags['opening_hours']}")
    site = tags.get("website") or tags.get("contact:website")
    wifi_line = "<ul>" + "".join(f"<li>{esc(f)}</li>" for f in facts) + (
        f"<li>Website: <a href=\"{esc(site)}\" rel=\"nofollow noopener\">{esc(site)}</a></li>"
        if site and site.startswith(("http://", "https://")) else "") + "</ul>" if facts or site else ""
    area_link = (f" · <a href=\"{esc(area['url'])}\">Cafes in {esc(area['name'])}</a>") if area else ""
    return (
        f"<article class=\"ssr\" id=\"ssr\"><h1>{esc(name)}</h1>"
        f"<p class=\"ssr-sub\">{esc(kind)}{' · ' + esc(where) if where else ''}</p>"
        f"<p class=\"ssr-answer\">{esc(answer_sentence(name, where, groups, site, local))}</p>{table}{wifi_line}"
        + trai_table_html(local, "Mobile data in the surrounding area (TRAI MySpeed medians per hexagon; not "
                          "measured at this venue, and indoor signal may differ)")
        + "<p class=\"ssr-note\">Venue speeds are tested in the browser by people at the place. “Verified” means "
        "our server counted the bytes and signed a receipt. TRAI MySpeed, Ookla and M-Lab figures describe the "
        "surrounding area, not this venue. <a href=\"/about.html#method\">How we measure</a> · "
        f"<a href=\"/\">Map</a>{area_link}</p></article>")


def home_summary_html(cities: list[dict], tested: list[dict]) -> str:
    """Crawlable entry point on /: what the site is, the city and neighbourhood pages, and tested places."""
    city_items = "".join(
        f"<li><a href=\"{esc(c['url'])}\">{esc(c['name'])}</a>: " + ", ".join(
            f"<a href=\"{esc(a['url'])}\">{esc(a['name'])}</a>" for a in c["areas"][:12]) + "</li>" for c in cities)
    tested_items = "".join(
        f"<li><a href=\"/p/{esc(p['id'])}\">{esc(p['name'])}</a>"
        f"{' — ' + esc(p['where']) if p['where'] else ''}: {esc(p['tested_line'])}</li>" for p in tested)
    return (
        "<section class=\"ssr\" id=\"ssr\"><h2>Café and coworking WiFi and mobile data speeds</h2>"
        "<p>isitfast is a map of cafes and coworking spaces in India with the internet speeds that matter for "
        "working from one: the venue's WiFi and Jio, Airtel and Vi mobile data, tested in the browser by people "
        "who were there (within 50 m), plus TRAI MySpeed medians for the surrounding area (not the venue). Free, no "
        "app, no login.</p>"
        + (f"<h2>Cafes to work from, by city and neighbourhood</h2><ul>{city_items}</ul>" if city_items else "")
        + (f"<h2>Tested on site</h2><ul>{tested_items}</ul>" if tested_items else "")
        + "<p><a href=\"/about.html#method\">How we measure</a></p></section>")


# ---------------------------------------------------------------- JSON-LD
def _ld(obj: dict) -> str:
    body = (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return f"<script type=\"application/ld+json\">{body}</script>"


def place_jsonld(place: dict, city: str, groups: list[dict], url: str, base: str, title: str, desc: str) -> str:
    """Only figures the page shows to everyone: each network's latest test, when that test is verified."""
    feats = []
    for g in groups:
        lt = g["latest"]
        if not lt["verified"]:
            continue
        for direction, word in (("down", "download"), ("up", "upload")):
            feats.append({"@type": "LocationFeatureSpecification",
                          "name": f"{g['label']} {word} speed (latest verified on-site test, {fmt_date(lt['ts'])})",
                          "value": round(lt[direction], 1), "unitCode": "E20", "unitText": "Mbps",
                          "measurementTechnique": f"{base}/about.html#method"})
    biz = {"@type": KIND_SCHEMA.get(place.get("kind") or "", "Place"), "@id": f"{url}#place", "name": place["name"],
           "geo": {"@type": "GeoCoordinates", "latitude": round(place["lat"], 5), "longitude": round(place["lon"], 5)},
           **({"amenityFeature": feats} if feats else {})}
    if city:
        biz["address"] = {"@type": "PostalAddress", "addressLocality": city, "addressCountry": "IN"}
    page = {"@type": "WebPage", "@id": url, "url": url, "name": title, "mainEntity": {"@id": f"{url}#place"},
            "isPartOf": {"@id": f"{base}/#website"}}
    last = max((g["latest"]["ts"] for g in groups), default=None)
    if last:
        page["dateModified"] = iso(last)
    return _ld({"@context": "https://schema.org", "@graph": [page, biz, *_site_nodes(base, desc)]})


def _site_nodes(base: str, desc: str) -> list[dict]:
    return [
        {"@type": "WebSite", "@id": f"{base}/#website", "url": f"{base}/", "name": "isitfast", "description": desc,
         "inLanguage": "en-IN", "publisher": {"@id": f"{base}/#org"}},
        {"@type": "Organization", "@id": f"{base}/#org", "name": "isitfast", "url": f"{base}/",
         "logo": f"{base}/apple-touch-icon.png?v=7"},
    ]


def site_jsonld(base: str, desc: str) -> str:
    return _ld({"@context": "https://schema.org", "@graph": _site_nodes(base, desc)})


# ---------------------------------------------------------------- robots.txt, sitemap.xml, llms.txt
def robots_txt(base: str) -> str:
    # Everything is open: we want search and AI engines to read and cite the measurements. /api/ stays
    # crawlable because Google renders the app, which needs it; only operator pages are disallowed.
    return (
        "# isitfast: cafe and coworking internet speeds, measured on site. Search and AI engines welcome.\n"
        "User-agent: *\n"
        "Content-Signal: search=yes, ai-input=yes, ai-train=yes\n"
        "Allow: /\n"
        "Disallow: /stats.html\n"
        "Disallow: /api/metrics\n"
        "Disallow: /api/stats\n"
        "Disallow: /api/crawlers\n"
        "Disallow: /qr.svg\n"
        f"\nSitemap: {base}/sitemap.xml\n")


def sitemap_xml(base: str, urls: list[tuple[str, int | None]]) -> str:
    def u(path, ts):
        lm = f"<lastmod>{datetime.datetime.fromtimestamp(ts, IST).date().isoformat()}</lastmod>" if ts else ""
        return f"<url><loc>{esc(base + path)}</loc>{lm}</url>"
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">"
            + "".join(u(p, ts) for p, ts in urls) + "</urlset>\n")


def md_safe(v: str, cap: int = 80) -> str:
    """A name as inert Markdown text: no links/brackets, no control characters or newlines, bounded length."""
    return re.sub(r"\s+", " ", re.sub(r"[\[\]()<>`*_#|\\\x00-\x1f\x7f]", " ", str(v))).strip()[:cap]


def llms_txt(base: str, cities: list[dict], tested: list[dict], n_pages: int, n_verified: int) -> str:
    lines = [
        "# isitfast",
        "",
        "> Internet speeds at cafes and coworking spaces in India: the venue's WiFi and Jio/Airtel/Vi mobile data, "
        "tested in the browser by people within 50 m of the place, plus TRAI MySpeed medians for the surrounding area.",
        "",
        "How to read the numbers:",
        "- On-site tests describe the venue. Each place page shows the latest test and, where available, the median "
        f"of verified readings from the last {WINDOW_DAYS} days. \"Verified\" means our speed server counted the "
        "bytes and signed a receipt. One test is a hint, not a verdict: quote the test count and date.",
        "- TRAI MySpeed medians (crowd-sourced, per carrier, per ~0.4-3 km hexagon) describe the surrounding area, "
        "not the venue. Quote them as \"around <place>\", with the area size and period.",
        f"- Coverage: {n_pages} cafe and coworking pages, {n_verified} verified on-site tests. A place without an "
        "on-site test is untested, not slow.",
        f"- Cite a venue test as: \"<place>: <figure> (isitfast, <n> tests, <date>)\", linking the place page on {base}.",
        "- Cite area data as: \"around <place> (~<size> km area, TRAI MySpeed, <period>): <carrier> <figure>; not "
        "measured at the venue\".",
        "",
        "## Pages",
        f"- [Map]({base}/?utm_source=llms): every place, with its numbers",
        f"- [How we measure]({base}/about.html?utm_source=llms#method): geofence, signed receipts, data sources",
    ]
    for c in cities:
        lines += ["", f"## {md_safe(c['name'])}", f"- [Cafes to work from in {md_safe(c['name'])}]({base}{c['url']}?utm_source=llms)"]
        lines += [f"- [{md_safe(a['name'])}]({base}{a['url']}?utm_source=llms): {a['line']}" for a in c["areas"]]
    if tested:
        lines += ["", "## Tested on site"]
        lines += [f"- [{md_safe(p['name'])}]({base}/p/{p['id']}?utm_source=llms)"
                  f"{' — ' + md_safe(p['where']) if p['where'] else ''}: {p['tested_line']}" for p in tested]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- crawler log
# User-agent substrings, by why the bot fetches. Order matters: the first match wins.
BOTS = [
    ("user", ("ChatGPT-User", "Perplexity-User", "Claude-User", "MistralAI-User", "DuckAssistBot", "Google-Agent")),
    ("ai-search", ("OAI-SearchBot", "PerplexityBot", "Claude-SearchBot", "YouBot", "Amazonbot", "Applebot")),
    ("ai-train", ("GPTBot", "ClaudeBot", "anthropic-ai", "CCBot", "Meta-ExternalAgent", "Bytespider", "cohere-ai",
                  "Diffbot", "AI2Bot", "Timpibot")),
    ("search", ("Googlebot", "bingbot", "DuckDuckBot", "YandexBot", "Baiduspider", "Bravebot", "PetalBot")),
]
AI_REFERRERS = ("chatgpt.com", "chat.openai.com", "perplexity.ai", "gemini.google.com", "copilot.microsoft.com",
                "claude.ai", "you.com", "phind.com", "meta.ai", "grok.com", "chat.mistral.ai", "deepseek.com")
AI_UTM = {"chatgpt.com", "chatgpt", "perplexity", "perplexity.ai", "copilot", "gemini", "claude", "claude.ai", "llms"}
# Only pages and data a reader or agent would fetch are logged, not the assets a renderer pulls with them.
LOGGED_PATHS = {"/", "/index.html", "/about.html", "/robots.txt", "/sitemap.xml", "/llms.txt"}


def loggable(path: str) -> bool:
    return path in LOGGED_PATHS or path.startswith("/p/") or (
        path.startswith("/api/places/") and not path.startswith("/api/places/tile/"))


CRAWL_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_log (ts INTEGER NOT NULL, kind TEXT NOT NULL, agent TEXT NOT NULL,
  path TEXT NOT NULL, status INTEGER);
CREATE INDEX IF NOT EXISTS crawl_log_ts ON crawl_log(ts);
"""
CRAWL_RETAIN_DAYS = 400


def classify(ua: str, referer: str, utm: str | None) -> tuple[str, str] | None:
    """(kind, agent) for an AI/search crawler or a visit referred by an AI assistant; None otherwise."""
    for kind, names in BOTS:
        for n in names:
            if n.lower() in (ua or "").lower():
                return kind, n
    host = re.sub(r"^https?://([^/:]+).*$", r"\1", referer or "").lower()
    for h in AI_REFERRERS:
        if host == h or host.endswith("." + h):
            return "referral", h
    if utm and utm.lower() in AI_UTM:
        return "referral", utm.lower()[:32]
    return None


def path_group(path: str) -> str:
    if path.startswith("/p/"):
        return path[:320]  # kept per place: which place pages AI assistants fetch for their users
    if path.startswith("/api/places/tile/"):
        return "/api/places/tile/*"
    if path.startswith("/api/places/"):
        return "/api/places/*"
    return path if len(path) <= 40 else path[:40]


def crawl_report(c, days: int, now: int | None = None) -> dict:
    now = int(now if now is not None else time.time())
    since = now - days * 86400
    by_agent = [dict(r) for r in c.execute(
        "SELECT kind, agent, COUNT(*) AS hits, COUNT(DISTINCT path) AS paths, MAX(ts) AS last FROM crawl_log "
        "WHERE ts >= ? GROUP BY kind, agent ORDER BY hits DESC", (since,))]
    by_path = [dict(r) for r in c.execute(
        "SELECT kind, path, COUNT(*) AS hits FROM crawl_log WHERE ts >= ? GROUP BY kind, path "
        "ORDER BY hits DESC LIMIT 50", (since,))]
    user_pages = [dict(r) for r in c.execute(
        "SELECT path, COUNT(*) AS hits FROM crawl_log WHERE ts >= ? AND kind='user' AND path LIKE '/p/%' "
        "GROUP BY path ORDER BY hits DESC LIMIT 50", (since,))]
    return {"days": days, "by_agent": by_agent, "by_path": by_path, "user_fetches_of_place_pages": user_pages}


# ---------------------------------------------------------------- city and neighbourhood pages
PAGE_CSS = """
@font-face { font-family: "Geist"; font-weight: 400 600; font-display: swap; src: url(/fonts/geist-latin.woff2?v=1) format("woff2"); }
html { background: #EFEDE6; }
body { font-family: "Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; max-width: 760px; margin: 32px auto; padding: 32px 40px 40px; line-height: 1.6; color: #1B1A17; background: #FBFAF7; border-radius: 20px; box-shadow: 0 1px 2px rgba(27,26,23,.06), 0 8px 32px rgba(27,26,23,.08); }
h1 { font-size: 30px; font-weight: 600; letter-spacing: -.02em; line-height: 1.2; margin: 20px 0 8px; }
h2 { font-size: 18px; font-weight: 600; margin: 32px 0 8px; padding-top: 24px; border-top: 1px solid #E3DFD5; }
a { color: #1F2DA8; text-underline-offset: 2px; } code { background: #F4F2EC; border: 1px solid #E3DFD5; padding: 1px 5px; border-radius: 5px; font-size: .9em; }
.muted, small { color: #5F5A50; } .answer { font-size: 17px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; margin: 8px 0 16px; font-variant-numeric: tabular-nums; }
caption { text-align: left; font-weight: 600; padding-bottom: 6px; }
th, td { text-align: left; padding: 7px 6px; border-top: 1px solid #E3DFD5; vertical-align: top; }
ul.cols { columns: 2; column-gap: 32px; padding-left: 18px; } ul.cols li { break-inside: avoid; margin: 0 0 6px; }
nav.crumbs { font-size: 14px; } .back { display: inline-flex; align-items: center; gap: 8px; font-weight: 500; text-decoration: none; color: #1B1A17; }
.back svg { width: 22px; height: 22px; }
@media (max-width: 600px) { body { margin: 0; border-radius: 0; padding: 20px; box-shadow: none; } h1 { font-size: 25px; } ul.cols { columns: 1; } }
"""
BRAND = ("<p><a class=\"back\" href=\"/\"><svg viewBox=\"0 0 32 32\" aria-hidden=\"true\"><circle cx=\"16\" "
         "cy=\"16\" r=\"13\" fill=\"none\" stroke=\"#2A3BD4\" stroke-width=\"2.4\"/><path d=\"M16 16H29M29 12.5v7\" "
         "stroke=\"#2A3BD4\" stroke-width=\"2.4\" stroke-linecap=\"round\"/></svg>isitfast</a></p>")


def page_shell(title: str, desc: str, canonical: str, robots: str, jsonld: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
        f"<title>{esc(title)}</title>\n<meta name=\"description\" content=\"{esc(desc)}\" />\n"
        f"<meta name=\"robots\" content=\"{esc(robots)}\" />\n<link rel=\"canonical\" href=\"{esc(canonical)}\" />\n"
        f"<meta property=\"og:type\" content=\"website\" /><meta property=\"og:title\" content=\"{esc(title)}\" />"
        f"<meta property=\"og:description\" content=\"{esc(desc)}\" /><meta property=\"og:url\" content=\"{esc(canonical)}\" />\n"
        "<link rel=\"icon\" href=\"/favicon.svg?v=8\" type=\"image/svg+xml\" />"
        "<link rel=\"preload\" href=\"/fonts/geist-latin.woff2?v=1\" as=\"font\" type=\"font/woff2\" crossorigin />\n"
        f"<style>{PAGE_CSS}</style>\n{jsonld}\n</head>\n<body>\n{BRAND}\n{body}\n</body>\n</html>\n")


def _crumbs(items: list[tuple[str, str]]) -> str:
    return "<nav class=\"crumbs\" aria-label=\"Breadcrumb\">" + " › ".join(
        f"<a href=\"{esc(u)}\">{esc(n)}</a>" if u else esc(n) for n, u in items) + "</nav>"


def _breadcrumb_ld(base: str, items: list[tuple[str, str]]) -> dict:
    return {"@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": n, **({"item": base + u} if u else {})}
        for i, (n, u) in enumerate(items)]}


def area_line(area: dict) -> str:
    rows = trai_rows(area.get("local"))
    nums = ", ".join(f"{r['carrier']} {mbps(r['down'])}" for r in rows[:3])
    return (f"{area['n_places']} cafes/coworking; area mobile data median download {nums} Mbps (TRAI MySpeed, "
            f"{trai_size(rows)} hexagon)" if rows else f"{area['n_places']} cafes/coworking")


def area_page(area: dict, city: dict, base: str, site: str = SITE) -> tuple[str, str, str, str]:
    """(title, description, jsonld, body) for /in/<city>/<area>. `area` has name, url, local, places (dicts with
    id, name, kind, tested_line, wifi_tag), n_places, n_tested."""
    name, cname = area["name"], city["name"]
    work = area["n_tested"] or any(p.get("wifi_tag") for p in area["places"])
    head = f"Cafes to work from in {name}, {cname}" if work else f"Cafes in {name}, {cname}"
    title = f"{head}: WiFi tests & mobile data by carrier · isitfast"
    around = trai_sentence(area.get("local"), name, "any one cafe")
    tested_txt = (f"{area['n_tested']} with on-site speed tests on {site}" if area["n_tested"]
                  else "none tested on site yet")
    answer = f"{head}: {area['n_places']} cafes and coworking spaces listed, {tested_txt}." + (f" {around}" if around else "")
    desc = answer[:300]
    tested = [p for p in area["places"] if p.get("tested_line")]
    rest = [p for p in area["places"] if not p.get("tested_line")]
    li = lambda p: (f"<li><a href=\"/p/{esc(p['id'])}\">{esc(p['name'])}</a>"  # noqa: E731
                    + (f" — {esc(p['tested_line'])}" if p.get("tested_line") else "")
                    + (" <small>(OSM lists WiFi, untested)</small>" if p.get("wifi_tag") and not p.get("tested_line") else "")
                    + "</li>")
    crumbs = [("isitfast", "/"), (cname, city["url"]), (name, "")]
    body = (
        _crumbs(crumbs)
        + f"<h1>{esc(head)}</h1>"
        + f"<p class=\"answer\">{esc(answer)}</p>"
        + trai_table_html(area.get("local"), f"Mobile data around {name} (TRAI MySpeed medians for one hexagon at "
                          "the neighbourhood centre; individual cafes differ, and indoor signal may be weaker)")
        + (f"<h2>Tested on site</h2><ul>{''.join(li(p) for p in tested)}</ul>" if tested else "")
        + (f"<h2>Cafes and coworking spaces in {esc(name)}</h2><p class=\"muted\">Not tested on site yet. Open one and run "
           f"a 20-second test while you're there.</p><ul class=\"cols\">{''.join(li(p) for p in rest)}</ul>" if rest else "")
        + "<p class=\"muted\">Venue WiFi speeds come only from on-site tests (within 50 m). TRAI MySpeed figures are "
          "crowd-sourced medians for the surrounding hexagon and describe the area, not a particular cafe. "
          f"Places from OpenStreetMap. <a href=\"/about.html#method\">How we measure</a> · <a href=\"/\">Open the map</a></p>")
    url = base + area["url"]
    ld = _ld({"@context": "https://schema.org", "@graph": [
        {"@type": "CollectionPage", "@id": url, "url": url, "name": title, "description": desc,
         "isPartOf": {"@id": f"{base}/#website"}, "about": {"@type": "Place", "name": f"{name}, {cname}",
                                                             "geo": {"@type": "GeoCoordinates", "latitude": round(area["lat"], 5),
                                                                     "longitude": round(area["lon"], 5)}},
         "mainEntity": {"@type": "ItemList", "numberOfItems": min(len(area["places"]), 100), "itemListElement": [
             {"@type": "ListItem", "position": i + 1, "url": f"{base}/p/{p['id']}", "name": p["name"]}
             for i, p in enumerate(area["places"][:100])]}},
        _breadcrumb_ld(base, crumbs), *_site_nodes(base, SITE_DESCRIPTION)]})
    return title, desc, ld, body


def city_page(city: dict, base: str) -> tuple[str, str, str, str]:
    """(title, description, jsonld, body) for /in/<city>. `city` has name, url, areas (indexable, with line)."""
    name = city["name"]
    title = f"Cafes to work from in {name}: WiFi & mobile data by neighbourhood · isitfast"
    desc = (f"{sum(a['n_places'] for a in city['areas'])} cafes and coworking spaces in {len(city['areas'])} {name} "
            "neighbourhoods, with on-site WiFi tests where people have run them and TRAI MySpeed mobile data medians "
            "per carrier for each area.")
    crumbs = [("isitfast", "/"), (name, "")]
    items = "".join(f"<li><a href=\"{esc(a['url'])}\">{esc(a['name'])}</a> <small>{esc(a['line'])}</small></li>"
                    for a in city["areas"])
    body = (_crumbs(crumbs) + f"<h1>Cafes to work from in {esc(name)}</h1><p class=\"answer\">{esc(desc)}</p>"
            + f"<h2>Neighbourhoods</h2><ul>{items}</ul>"
            + "<p class=\"muted\"><a href=\"/about.html#method\">How we measure</a> · <a href=\"/\">Open the map</a></p>")
    url = base + city["url"]
    ld = _ld({"@context": "https://schema.org", "@graph": [
        {"@type": "CollectionPage", "@id": url, "url": url, "name": title, "description": desc,
         "isPartOf": {"@id": f"{base}/#website"},
         "mainEntity": {"@type": "ItemList", "numberOfItems": len(city["areas"]), "itemListElement": [
             {"@type": "ListItem", "position": i + 1, "url": base + a["url"], "name": a["name"]}
             for i, a in enumerate(city["areas"])]}},
        _breadcrumb_ld(base, crumbs), *_site_nodes(base, SITE_DESCRIPTION)]})
    return title, desc, ld, body

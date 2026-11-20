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


def city_for(lat: float, lon: float) -> str | None:
    from .seed import CITIES  # noqa: PLC0415 — seed imports main, so import late
    for key, (s, w, n, e) in CITIES.items():
        if s <= lat <= n and w <= lon <= e:
            return CITY_LABEL.get(key, key.title())
    return None


def locality(tags: dict, lat: float, lon: float) -> str:
    """"Koramangala, Bengaluru" from the place's OSM address tags plus the seeded city it falls in."""
    city = city_for(lat, lon) or tags.get("addr:city")
    area = tags.get("addr:suburb") or tags.get("addr:street")
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


def indexable(place: dict, groups: list[dict], residential: set[str], now: int | None = None) -> bool:
    """A place page is indexed only with an OpenStreetMap name (Google rows keep none; names typed by visitors
    are unmoderated), a non-residential kind (homes stay out of search), and a verified test in INDEX_DAYS."""
    now = int(now if now is not None else time.time())
    return (bool(place.get("name")) and bool(OSM_ID.match(place.get("id") or "")) and place.get("kind") not in residential
            and any(g["latest_verified"] and g["latest_verified"]["ts"] >= now - INDEX_DAYS * 86400 for g in groups))


# ---------------------------------------------------------------- text
def answer_sentence(name: str, where: str, groups: list[dict], site: str = "isitfast.cloud") -> str:
    """One self-contained, dated sentence an answer engine can quote without losing the caveats. Verified tests
    only; unverified ones are in the table, labelled."""
    head = f"{name}{', ' + where if where else ''}"
    if not groups:
        return f"{head}: no one has tested the internet here yet."
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
        return f"{head}: only unverified speed tests so far ({site})."
    return f"{head}: " + "; ".join(parts) + f". Measured with in-browser speed tests run within 50 m of the place ({site})."


def place_summary_html(place: dict, where: str, groups: list[dict], site: str = "isitfast.cloud") -> str:
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
    return (
        f"<article class=\"ssr\" id=\"ssr\"><h1>{esc(name)}</h1>"
        f"<p class=\"ssr-sub\">{esc(kind)}{' · ' + esc(where) if where else ''}</p>"
        f"<p class=\"ssr-answer\">{esc(answer_sentence(name, where, groups, site))}</p>{table}"
        "<p class=\"ssr-note\">Speeds are tested in the browser by people at the place. “Verified” means our "
        "server counted the bytes and signed a receipt. Ookla and M-Lab area averages are shown in the app and "
        "describe the neighbourhood, not this venue. <a href=\"/about.html#method\">How we measure</a> · "
        "<a href=\"/\">Map of tested places</a></p></article>")


def home_summary_html(places: list[dict]) -> str:
    """Crawlable entry point on /: what the site is, plus links to every indexable tested place."""
    items = "".join(
        f"<li><a href=\"/p/{esc(p['id'])}\">{esc(p['name'])}</a>"
        f"{' — ' + esc(p['where']) if p['where'] else ''}: {esc(p['line'])}</li>" for p in places)
    return (
        "<section class=\"ssr\" id=\"ssr\"><h2>Café and coworking WiFi speeds, measured on site</h2>"
        "<p>isitfast is a map of cafes and coworking spaces in India with the internet speeds that matter for "
        "working from one: the venue's WiFi and Jio, Airtel and Vi mobile data, tested in the browser by people "
        "who were there (within 50 m). Free, no app, no login.</p>"
        + (f"<h2>Tested places</h2><ul>{items}</ul>" if items else "")
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
           "amenityFeature": feats}
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


def llms_txt(base: str, places: list[dict], n_places: int, n_verified: int) -> str:
    lines = [
        "# isitfast",
        "",
        "> Internet speeds at cafes and coworking spaces in India (launch city: Bengaluru), measured on site: the "
        "venue's WiFi and Jio/Airtel/Vi mobile data, tested in the browser by people within 50 m of the place.",
        "",
        "How to read the numbers:",
        "- Each place page shows the latest test and, where available, the median of verified readings from the "
        f"last {WINDOW_DAYS} days. \"Verified\" means our speed server counted the bytes and signed a receipt.",
        "- One test is a hint, not a verdict. Quote the test count and the date with any figure.",
        "- Ookla/M-Lab figures in the app are neighbourhood or city averages, not venue measurements.",
        f"- Coverage today: {n_places} listed places with verified tests, {n_verified} verified tests in total. Absence from "
        "the site means untested, not slow.",
        "- Cite as: \"<place>: <figure> (isitfast.cloud, <n> tests, last tested <date>)\" with a link to the place page.",
        "",
        "## Pages",
        f"- [Map of tested places]({base}/?utm_source=llms): the app; every tested place is listed below too",
        f"- [How we measure]({base}/about.html?utm_source=llms#method): geofence, signed receipts, data sources",
        "",
        "## Tested places",
    ]
    lines += [f"- [{md_safe(p['name'])}]({base}/p/{p['id']}?utm_source=llms)"
              f"{' — ' + md_safe(p['where']) if p['where'] else ''}: {p['line']}" for p in places]
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

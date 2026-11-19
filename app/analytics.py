"""First-party, cookieless product analytics (the launch funnel: visit -> open a place -> test -> share).

No cookies, no localStorage, no third-party script. A visitor is counted per day by a hash of
(random daily salt + IP + user agent). The salt is random, kept only in app.db, and deleted when the day
(IST) ends, so hashes from different days can't be linked or recomputed, even by us.

Events carry a small whitelisted set of properties (never coordinates, place ids or names) plus the `?r=`
attribution code of the link the visitor arrived on.
"""
import collections
import datetime
import hashlib
import json
import re
import secrets
import sqlite3
import time

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
EVENTS = {"pageview", "panel_open", "test_cta_shown", "gate_fail", "test_start", "test_complete", "test_fail",
          "share_click", "here_click", "handoff_shown"}
# Property keys we keep; values are short tokens or numbers. Nothing that locates or identifies anyone.
PROP_KEYS = {"land", "dev", "rh", "tested", "kind", "reason", "acc", "dist", "net", "verified", "approx", "ch", "n"}
REF_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")
BOT_RE = re.compile(r"bot|crawl|spider|slurp|preview|headless|lighthouse|facebookexternalhit|whatsapp|curl|python", re.I)
MAX_EVENTS_PER_VISITOR_DAY = 300
RETAIN_DAYS = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (day TEXT NOT NULL, ts INTEGER NOT NULL, vh TEXT NOT NULL, name TEXT NOT NULL,
  ref TEXT, props TEXT);
CREATE INDEX IF NOT EXISTS events_day ON events(day, name);
CREATE INDEX IF NOT EXISTS events_vh ON events(vh, day);
CREATE TABLE IF NOT EXISTS analytics_salt (day TEXT PRIMARY KEY, salt TEXT NOT NULL);
"""


def today(now: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(now if now is not None else time.time(), IST).strftime("%Y-%m-%d")


def day_offset(day: str, delta: int) -> str:
    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=delta)).isoformat()


_salt: tuple[str | None, str | None] = (None, None)  # (day, salt) cached per worker


def day_salt(c: sqlite3.Connection, day: str) -> str:
    """Today's random salt, shared by all workers through app.db. Creating it also destroys older salts
    and prunes events past the retention window."""
    global _salt
    if _salt[0] == day and _salt[1]:
        return _salt[1]
    c.execute("INSERT OR IGNORE INTO analytics_salt VALUES (?, ?)", (day, secrets.token_hex(16)))
    salt = c.execute("SELECT salt FROM analytics_salt WHERE day=?", (day,)).fetchone()[0]
    c.execute("DELETE FROM analytics_salt WHERE day < ?", (day,))
    c.execute("DELETE FROM events WHERE day < ?", (day_offset(day, -RETAIN_DAYS),))
    _salt = (day, salt)
    return salt


def clean_props(p: dict | None) -> dict:
    out = {}
    for k, v in (p or {}).items():
        if k not in PROP_KEYS:
            continue
        if isinstance(v, bool):
            out[k] = int(v)
        elif isinstance(v, (int, float)) and abs(v) < 1e9:
            out[k] = round(v, 1) if isinstance(v, float) else v
        elif isinstance(v, str) and TOKEN_RE.match(v):
            out[k] = v.lower()
    return out


def clean_ref(r: str | None) -> str | None:
    r = (r or "").strip().lower()
    return r if REF_RE.match(r) else None


def record(c: sqlite3.Connection, ip: str, ua: str, name: str, ref: str | None, props: dict | None,
           now: float | None = None) -> bool:
    """Store one event. Returns False when it was dropped (unknown event, bot, per-visitor cap)."""
    if name not in EVENTS or not ua or BOT_RE.search(ua):
        return False
    now = time.time() if now is None else now
    day = today(now)
    salt = day_salt(c, day)
    vh = hashlib.sha256(f"{salt}|{ip}|{ua}".encode()).hexdigest()[:16]
    n = c.execute("SELECT COUNT(*) FROM events WHERE vh=? AND day=?", (vh, day)).fetchone()[0]
    if n >= MAX_EVENTS_PER_VISITOR_DAY:
        return False
    props = clean_props(props)
    c.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
              (day, int(now), vh, name, clean_ref(ref), json.dumps(props, separators=(",", ":")) if props else None))
    return True


def summary(c: sqlite3.Connection, days: int = 7, now: float | None = None) -> dict:
    """The daily launch numbers: visitors, engaged (opened a place), testers, by source, gate failures."""
    end = today(now)
    start = day_offset(end, -(days - 1))
    rows = c.execute("SELECT day, ts, vh, name, ref, props FROM events WHERE day >= ? ORDER BY ts", (start,)).fetchall()
    per_day: dict[str, dict] = {}
    visitor_ref: dict[tuple[str, str], str] = {}  # (day, vh) -> ref of their first pageview
    seen: dict[str, dict[str, set]] = collections.defaultdict(lambda: collections.defaultdict(set))
    gate = collections.Counter()
    gate_by = collections.defaultdict(collections.Counter)
    shares = collections.Counter()
    landing = collections.Counter()
    hosts = collections.Counter()
    for day, _ts, vh, name, ref, props in rows:
        p = json.loads(props) if props else {}
        d = per_day.setdefault(day, {"events": collections.Counter()})
        d["events"][name] += 1
        seen[day][name].add(vh)
        if name == "pageview":
            visitor_ref.setdefault((day, vh), ref or "direct")
            landing[p.get("land", "map")] += 1
            if p.get("rh"):
                hosts[p["rh"]] += 1
        elif name == "gate_fail":
            gate[p.get("reason", "unknown")] += 1
            for k in ("dev", "acc", "dist"):
                if k in p:
                    gate_by[k][str(p[k])] += 1
        elif name == "share_click":
            shares[p.get("ch", "other")] += 1
    # A visitor who didn't send a pageview first (e.g. a tab left open past midnight) counts as direct.
    refs = collections.defaultdict(lambda: {"visitors": 0, "engaged": 0, "testers": 0, "sharers": 0})
    out_days = []
    with_tests = {r[0]: r[1:] for r in c.execute(
        "SELECT date(created_at + 19800, 'unixepoch') AS d, COUNT(*), COUNT(DISTINCT place_id) FROM tests "
        "WHERE created_at >= ? GROUP BY d", (int(datetime.datetime.fromisoformat(start).replace(tzinfo=IST).timestamp()),))}
    for k in range(days):
        day = day_offset(start, k)
        s = seen.get(day, {})
        visitors = s.get("pageview", set())
        engaged = s.get("panel_open", set())
        testers = s.get("test_complete", set())
        sharers = s.get("share_click", set())
        for vh in visitors | engaged | testers:
            r = refs[visitor_ref.get((day, vh), "direct")]
            r["visitors"] += vh in visitors
            r["engaged"] += vh in engaged
            r["testers"] += vh in testers
            r["sharers"] += vh in sharers
        t = with_tests.get(day, (0, 0))
        out_days.append({
            "day": day, "visitors": len(visitors), "engaged": len(engaged), "testers": len(testers),
            "sharers": len(sharers), "tests_saved": t[0], "places_tested": t[1],
            "events": dict(per_day.get(day, {}).get("events", {})),
        })
    tested_14d = c.execute("SELECT COUNT(DISTINCT place_id) FROM tests WHERE created_at > ?",
                           (int(time.time()) - 14 * 86400,)).fetchone()[0]
    total = {k: sum(d[k] for d in out_days) for k in ("visitors", "engaged", "testers", "sharers", "tests_saved")}
    return {
        "range": [start, end], "total": total, "days": out_days,
        "refs": dict(sorted(refs.items(), key=lambda kv: -kv[1]["visitors"])),
        "gate_fail": {"reason": dict(gate), **{k: dict(v) for k, v in gate_by.items()}},
        "shares": dict(shares), "landing": dict(landing), "referrer_hosts": dict(hosts.most_common(20)),
        "places_tested_14d": tested_14d,
    }

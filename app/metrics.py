"""Instrumentation: per-worker counters/timings, flushed to SQLite every 30 s and read back by /api/metrics.

What it answers (the thresholds from the scaling review):
  - loop_lag_ms           event-loop overshoot of a 100 ms ticker (sustained > 50 ms: workers saturated)
  - threadpool_busy       borrowed threadpool tokens (sync handlers waiting for a thread)
  - route:<METHOD path>   latency per route + request counts (origin hits per route; CDN hit ratio)
  - db_error:<kind>       sqlite3.OperationalError by kind ("locked" = write contention)
  - test_* / outbound:*   test funnel (start, saved, rejects by reason) and upstream calls
  - telemetry:*           client beacons (speed endpoint used/failed, bytes per test)
"""
import asyncio
import os
import sqlite3
import time

import anyio.to_thread

FLUSH_S = 30
KEEP_DAYS = 14


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def flush(db, counts, timings) -> None:
    """Move this worker's counters/timings into the metrics table (one row per name/label per minute)."""
    if not counts and not timings:
        return
    minute = int(time.time() // 60) * 60
    pid = os.getpid()
    rows: list[tuple] = [(minute, pid, name, label, n, None, None, None) for (name, label), n in counts.items()]
    for name, xs in timings.items():
        if xs:
            rows.append((minute, pid, "timing", name, len(xs), _pct(xs, 0.5), _pct(xs, 0.95), max(xs)))
    counts.clear()
    timings.clear()
    try:
        with db() as c:
            c.executemany("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?)", rows)
            c.execute("DELETE FROM metrics WHERE minute < ?", (minute - KEEP_DAYS * 86400,))
    except sqlite3.Error:
        pass  # metrics must never take the app down


async def monitor(db, counts, timings, timing) -> None:
    """Event-loop lag + threadpool saturation sampling, and the periodic flush."""
    last_flush = time.monotonic()
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        timing("loop_lag_ms", max(0.0, (time.monotonic() - t0 - 0.1) * 1000))
        lim = anyio.to_thread.current_default_thread_limiter()
        timing("threadpool_busy", float(lim.borrowed_tokens))
        if time.monotonic() - last_flush >= FLUSH_S:
            last_flush = time.monotonic()
            await anyio.to_thread.run_sync(flush, db, counts, timings)


def summary(db, minutes: int = 60) -> dict:
    """Aggregate all workers over the last `minutes`."""
    since = int(time.time()) - minutes * 60
    out: dict = {"window_min": minutes, "counts": {}, "timings": {}}
    with db() as c:
        for r in c.execute(
            "SELECT name, label, SUM(n) FROM metrics WHERE minute >= ? AND name != 'timing' GROUP BY name, label "
            "ORDER BY name, label", (since,)):
            out["counts"][f"{r[0]}:{r[1]}" if r[1] else r[0]] = r[2]
        for r in c.execute(
            "SELECT label, SUM(n), MAX(p95), MAX(mx), SUM(p50 * n) / SUM(n) FROM metrics "
            "WHERE minute >= ? AND name = 'timing' GROUP BY label ORDER BY label", (since,)):
            out["timings"][r[0]] = {"n": r[1], "p50_avg": round(r[4] or 0, 1), "p95_worst": round(r[2] or 0, 1),
                                    "max": round(r[3] or 0, 1)}
    return out

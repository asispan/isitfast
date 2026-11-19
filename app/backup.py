"""Nightly consistent snapshot of app.db (the community tests can't be regenerated).

VACUUM INTO writes a compact, transactionally consistent copy without blocking readers.
Keeps the newest KEEP snapshots in $DATA_DIR/backups/.

Usage: python -m app.backup [--keep 14] [--if-older-hours 20]
"""
import argparse
import glob
import os
import sqlite3
import time

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
OUT = os.path.join(DATA_DIR, "backups")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=14)
    ap.add_argument("--if-older-hours", type=float, help="no-op if the newest snapshot is younger than this")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    existing = sorted(glob.glob(os.path.join(OUT, "app-*.db")))
    if a.if_older_hours is not None and existing and time.time() - os.path.getmtime(existing[-1]) < a.if_older_hours * 3600:
        return
    dest = os.path.join(OUT, time.strftime("app-%Y%m%d-%H%M%S.db"))
    c = sqlite3.connect(DB_PATH, timeout=30)
    try:
        c.execute("VACUUM INTO ?", (dest,))
    finally:
        c.close()
    chk = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    n = chk.execute("SELECT COUNT(*) FROM tests").fetchone()[0]
    chk.close()
    if ok != "ok":
        os.remove(dest)
        raise SystemExit(f"[backup] integrity check failed: {ok}")
    snaps = sorted(glob.glob(os.path.join(OUT, "app-*.db")))
    for old in snaps[: -a.keep]:
        os.remove(old)
    print(f"[backup] {dest} ({os.path.getsize(dest) / 1e6:.1f} MB, {n} tests); kept {min(len(snaps), a.keep)}", flush=True)


if __name__ == "__main__":
    main()

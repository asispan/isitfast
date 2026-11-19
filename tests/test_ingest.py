"""Ookla ingest builds a new DB and swaps it in atomically; only INGEST_BBOX tiles are kept."""
import contextlib
import os
import sqlite3
import sys
import tempfile

import duckdb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import ookla_ingest as oi  # noqa: E402


def make_parquet(path: str, rows: list[tuple]) -> None:
    c = duckdb.connect()
    c.execute("CREATE TABLE t (quadkey VARCHAR, tile_x DOUBLE, tile_y DOUBLE, avg_d_kbps BIGINT, avg_u_kbps BIGINT, "
              "avg_lat_ms BIGINT, tests BIGINT, devices BIGINT)")
    c.executemany("INSERT INTO t VALUES (?,?,?,?,?,?,?,?)", rows)
    c.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")


def test_atomic_swap_and_bbox(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setattr(oi, "DATA_DIR", d)
    monkeypatch.setattr(oi, "OOKLA_DB", os.path.join(d, "ookla.db"))
    src = os.path.join(d, "src.parquet")

    class FakeResp:
        def raise_for_status(self):
            pass

        def iter_bytes(self, n):
            with open(src, "rb") as f:
                while chunk := f.read(n):
                    yield chunk

    monkeypatch.setattr(oi.httpx, "stream", lambda *a, **k: contextlib.nullcontext(FakeResp()))

    # quarter 1: one tile in Bengaluru (kept) and one in Paris (dropped)
    make_parquet(src, [("q-blr", 77.6, 12.9, 50000, 10000, 20, 5, 3), ("q-paris", 2.35, 48.85, 90000, 20000, 15, 9, 4)])
    monkeypatch.setattr(oi, "latest_quarter", lambda kind: (2026, 2, "u", ""))
    oi.ingest("mobile", None, None)
    oi.ingest("fixed", None, None)

    reader = sqlite3.connect(f"file:{oi.OOKLA_DB}?mode=ro", uri=True)  # long-lived reader on the old file
    assert reader.execute("SELECT quadkey FROM tiles WHERE kind='mobile'").fetchall() == [("q-blr",)]

    # quarter 2 for mobile only: fixed rows must survive, the old reader must keep working
    make_parquet(src, [("q-blr2", 77.61, 12.91, 60000, 11000, 19, 6, 3)])
    monkeypatch.setattr(oi, "latest_quarter", lambda kind: (2026, 3, "u", ""))
    oi.ingest("mobile", None, None)
    assert reader.execute("SELECT COUNT(*) FROM tiles").fetchone()[0] == 2  # still the old snapshot
    new = sqlite3.connect(oi.OOKLA_DB)
    assert sorted(new.execute("SELECT kind, quadkey FROM tiles").fetchall()) == [("fixed", "q-blr"), ("mobile", "q-blr2")]
    assert new.execute("SELECT year, quarter FROM meta WHERE kind='mobile'").fetchone() == (2026, 3)
    assert not os.path.exists(oi.OOKLA_DB + ".build")

    # same quarter again: skipped without touching the file
    mtime = os.path.getmtime(oi.OOKLA_DB)
    oi.ingest("mobile", None, None)
    assert os.path.getmtime(oi.OOKLA_DB) == mtime

"""Instrumentation: counters flushed to SQLite, token-protected /api/metrics, telemetry beacon."""
import os
import sys
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="isitfast-metrics-")
os.environ["DATA_DIR"] = TMP
os.environ.setdefault("IP_SALT", "test-salt-0123456789")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "METRICS_TOKEN", "t0ken")
    with TestClient(main.app) as c:
        yield c


def test_metrics_hidden_without_token(client):
    assert client.get("/api/metrics").status_code == 404
    assert client.get("/api/metrics", headers={"x-metrics-token": "nope"}).status_code == 404


def test_metrics_aggregate_routes_and_telemetry(client):
    for _ in range(3):
        client.get("/healthz")
    assert client.post("/api/telemetry", json={"endpoint": "own", "outcome": "saved", "network": "wifi",
                                                "down_mb": 120.5, "up_mb": 30}).status_code == 204
    assert client.post("/api/telemetry", json={"endpoint": "evil", "outcome": "saved", "network": "wifi",
                                                "down_mb": 1, "up_mb": 1}).status_code == 422
    m = client.get("/api/metrics", headers={"x-metrics-token": "t0ken"}).json()
    assert m["timings"]["route:GET /healthz"]["n"] >= 3
    assert m["counts"]["telemetry:endpoint:own"] == 1
    assert m["timings"]["test_mb:wifi"]["max"] == 150.5
    assert "tests" in m["data"] and "file_mb" in m["data"]


def test_db_errors_counted_and_503(client, monkeypatch):
    import sqlite3

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(main, "features_in", boom)
    r = client.get("/api/places", params={"bbox": "12.9,77.5,13.0,77.6"})
    assert r.status_code == 503
    assert main._counts[("db_error", "locked")] >= 1

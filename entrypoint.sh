#!/bin/sh
# Background jobs, then the web server in the foreground.
#  - weekly: Ookla open data (no-op unless a new quarter is out), M-Lab carriers (no-op unless a new
#    month is out), OSM places for the seeded cities (no-op if refreshed in the last 6 days).
#    Each ingest builds a new database file and swaps it in atomically.
#  - daily: consistent backup of app.db (community tests).
set -e
# Basemap style/fonts/sprites are mirrored once into the data volume (tiles: $DATA_DIR/basemap/india.pmtiles,
# built with Planetiler; see README).
# Runs in the background: the web server must never wait on it (the map falls back to OpenFreeMap meanwhile).
[ -f "${DATA_DIR:-/data}/basemap/style.json" ] || (python -m app.basemap fetch-assets || echo "basemap assets fetch failed; map falls back to OpenFreeMap") &
(
  while true; do
    python -m app.ookla_ingest --kind all || echo "ookla ingest failed; will retry"
    python -m app.mlab_ingest || echo "mlab ingest failed; will retry"
    python -m app.seed --if-older-days 6 || echo "places refresh failed; will retry"
    sleep 604800
  done
) &
(
  # checked every 30 min, so restarts/deploys never postpone it; snapshots at most once per 20 h
  while true; do
    sleep 300
    python -m app.backup --keep 14 --if-older-hours 20 || echo "backup failed; will retry"
    sleep 1500
  done
) &
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers "${WEB_WORKERS:-3}"

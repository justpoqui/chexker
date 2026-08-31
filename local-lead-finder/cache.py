# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: every other module (osm_source now; enrich later) needs to
#      cache network results in one shared SQLite file instead of
#      each module inventing its own storage. Re-running the same
#      search must hit this cache, not the network.
# ============================================================

import sqlite3
import threading
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "lead_cache.sqlite3"


# ============================================================
# STEP 2 — CACHE: tables backing area/business Overpass queries
#           and permanent geocode lookups
# WHY: Overpass responses are keyed by a hash of the exact query
#      text, so identical queries (re-running the same search, or
#      re-resolving the same named area) are served from disk.
#      Nominatim's usage policy requires permanent client-side
#      caching of geocode results, so that table has no TTL.
# ============================================================

class Cache:
    """Thin wrapper around one SQLite file shared by the whole app.

    A single `sqlite3.Connection` is reused across threads. The
    background network thread (introduced in STEP 6) and the main
    Tk thread both go through this class, so every method takes a
    lock around its actual database access.
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS overpass_cache (
                    query_hash   TEXT PRIMARY KEY,
                    query_text   TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    fetched_at   REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    address_key TEXT PRIMARY KEY,
                    lat         REAL NOT NULL,
                    lon         REAL NOT NULL,
                    fetched_at  REAL NOT NULL
                )
                """
            )

    # -- Overpass response cache -----------------------------------

    def get_overpass(self, query_hash: str) -> Optional[str]:
        """Return the cached raw JSON text for this query hash, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT response_json FROM overpass_cache WHERE query_hash = ?",
                (query_hash,),
            ).fetchone()
        return row[0] if row else None

    def set_overpass(self, query_hash: str, query_text: str, response_json: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO overpass_cache (query_hash, query_text, response_json, fetched_at)
                VALUES (?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(query_hash) DO UPDATE SET
                    query_text = excluded.query_text,
                    response_json = excluded.response_json,
                    fetched_at = excluded.fetched_at
                """,
                (query_hash, query_text, response_json),
            )

    # -- Permanent geocode cache (Nominatim policy requires this) ---

    def get_geocode(self, address_key: str) -> Optional[tuple[float, float]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT lat, lon FROM geocode_cache WHERE address_key = ?",
                (address_key,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def set_geocode(self, address_key: str, lat: float, lon: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO geocode_cache (address_key, lat, lon, fetched_at)
                VALUES (?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(address_key) DO UPDATE SET
                    lat = excluded.lat,
                    lon = excluded.lon,
                    fetched_at = excluded.fetched_at
                """,
                (address_key, lat, lon),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# TODO (STEP 5): a site_cache table (domain -> enrichment result,
# fetched_at) with a 30-day TTL, added the same way as the two
# tables above once enrich.py exists to fill it.

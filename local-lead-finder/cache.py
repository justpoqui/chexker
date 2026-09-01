# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: every other module (osm_source now; enrich later) needs to
#      cache network results in one shared SQLite file instead of
#      each module inventing its own storage. Re-running the same
#      search must hit this cache, not the network.
# ============================================================

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional


def _default_db_path() -> Path:
    # When packaged (e.g. via PyInstaller --onefile), the app runs from a
    # temporary extraction directory that's wiped after every launch --
    # __file__-relative storage there would silently reset the lead
    # tracking and every cache on each run. Use a real per-user data
    # directory instead whenever the app is frozen.
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA") or Path.home())
        data_dir = base / "LocalLeadFinder"
    else:
        data_dir = Path(__file__).resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "lead_cache.sqlite3"


DEFAULT_DB_PATH = _default_db_path()


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
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reverse_geocode_cache (
                    coord_key  TEXT PRIMARY KEY,
                    label      TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS site_cache (
                    domain      TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    fetched_at  REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    osm_key        TEXT PRIMARY KEY,
                    status         TEXT NOT NULL DEFAULT 'new',
                    last_contacted REAL,
                    notes          TEXT NOT NULL DEFAULT '',
                    updated_at     REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dns_cache (
                    domain     TEXT PRIMARY KEY,
                    resolves   INTEGER NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_key TEXT PRIMARY KEY,
                    osm_keys_json TEXT NOT NULL,
                    run_at        REAL NOT NULL
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

    # -- Permanent reverse-geocode cache (same Nominatim policy as above) --
    # WHY: find_named_area_candidates()/resolve_named_area() in osm_source.py
    #      reverse-geocode a boundary's center point to disambiguate two
    #      same-named places when OSM's own tags don't say enough. Cached
    #      by rounded coordinate rather than address text.

    def get_reverse_geocode(self, coord_key: str) -> Optional[str]:
        """The cached label for this coordinate, "" if looked up but nothing
        was found, or None if it has never been looked up."""
        with self._lock:
            row = self._conn.execute(
                "SELECT label FROM reverse_geocode_cache WHERE coord_key = ?",
                (coord_key,),
            ).fetchone()
        return row[0] if row else None

    def set_reverse_geocode(self, coord_key: str, label: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO reverse_geocode_cache (coord_key, label, fetched_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(coord_key) DO UPDATE SET
                    label = excluded.label,
                    fetched_at = excluded.fetched_at
                """,
                (coord_key, label),
            )

    # -- Website enrichment cache, keyed by domain, with a TTL -------
    # WHY: unlike Overpass and geocode results, a site's health can
    #      change (it might get fixed, or go down) so this cache
    #      expires — the TTL is passed in by the caller (enrich.py
    #      uses 30 days) rather than hardcoded here.

    def get_site(self, domain: str, ttl_seconds: float) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json, fetched_at FROM site_cache WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        result_json, fetched_at = row
        if time.time() - fetched_at > ttl_seconds:
            return None
        return json.loads(result_json)

    def set_site(self, domain: str, result: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO site_cache (domain, result_json, fetched_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(domain) DO UPDATE SET
                    result_json = excluded.result_json,
                    fetched_at = excluded.fetched_at
                """,
                (domain, json.dumps(result)),
            )

    # -- DNS pre-call verification cache, keyed by guessed domain ----
    # WHY: enrich.py guesses several domain spellings per no-website
    #      business and checks each with a live DNS lookup (STEP 13).
    #      Re-running the same search shouldn't re-query DNS for a
    #      domain it already checked.

    def get_dns(self, domain: str, ttl_seconds: float) -> Optional[bool]:
        with self._lock:
            row = self._conn.execute(
                "SELECT resolves, fetched_at FROM dns_cache WHERE domain = ?",
                (domain,),
            ).fetchone()
        if row is None:
            return None
        resolves, fetched_at = row
        if time.time() - fetched_at > ttl_seconds:
            return None
        return bool(resolves)

    def set_dns(self, domain: str, resolves: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO dns_cache (domain, resolves, fetched_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(domain) DO UPDATE SET
                    resolves = excluded.resolves,
                    fetched_at = excluded.fetched_at
                """,
                (domain, int(resolves)),
            )

    # ============================================================
    # STEP 10 — LEAD STATE TRACKING
    # WHY: without this, the app has no memory — the same hottest
    #      leads resurface at the top of every run even after
    #      they've been called. This table is the app's only
    #      persistent, user-authored data (everything else here is
    #      a disposable cache of someone else's data), so unlike
    #      the tables above it is never expired or overwritten by
    #      a re-fetch — only by the user explicitly changing a
    #      lead's status.
    # ============================================================

    def get_all_leads(self) -> dict[str, dict]:
        """Every lead's status/notes, keyed by osm_key. Loaded once at
        startup; the GUI keeps its own in-memory copy from then on and
        only writes back through set_lead()."""
        with self._lock:
            cursor = self._conn.execute("SELECT osm_key, status, last_contacted, notes FROM leads")
            rows = cursor.fetchall()
        return {
            osm_key: {"status": status, "last_contacted": last_contacted, "notes": notes}
            for osm_key, status, last_contacted, notes in rows
        }

    def set_lead(self, osm_key: str, status: str, notes: str = "") -> None:
        # "new" means "no action taken yet" — leave last_contacted unset so
        # the GUI can distinguish "never touched" from "touched a while ago".
        touched_now = None if status == "new" else time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO leads (osm_key, status, last_contacted, notes, updated_at)
                VALUES (?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(osm_key) DO UPDATE SET
                    status = excluded.status,
                    last_contacted = COALESCE(excluded.last_contacted, leads.last_contacted),
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (osm_key, status, touched_now, notes),
            )

    # ============================================================
    # STEP 15 — DIFF MODE: remembering the last run of each search
    # WHY: only the most recent snapshot per search is needed to
    #      answer "what's new since last time" — this deliberately
    #      isn't a full history log, just enough to diff against.
    #      Like `leads`, this is overwritten by the app itself, not
    #      re-fetched from an outside source, so it has no TTL.
    # ============================================================

    def get_snapshot(self, snapshot_key: str) -> Optional[set[str]]:
        """The osm_keys seen the last time this exact search ran, or None
        if it has never run before (nothing to diff against yet)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT osm_keys_json FROM snapshots WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()
        return set(json.loads(row[0])) if row else None

    def set_snapshot(self, snapshot_key: str, osm_keys: set[str]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO snapshots (snapshot_key, osm_keys_json, run_at)
                VALUES (?, ?, strftime('%s', 'now'))
                ON CONFLICT(snapshot_key) DO UPDATE SET
                    osm_keys_json = excluded.osm_keys_json,
                    run_at = excluded.run_at
                """,
                (snapshot_key, json.dumps(sorted(osm_keys))),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

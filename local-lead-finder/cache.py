# ============================================================
# STEP 1 — SCAFFOLD: SQLite caching layer (placeholder)
# WHY: every other module (osm_source, enrich, and the optional
#      Nominatim geocoder) needs to cache network results. Putting
#      that in one shared module now means there's a single SQLite
#      file and a single set of table schemas, instead of each
#      module inventing its own caching. Built out alongside the
#      modules that use it, starting in STEP 2/3.
# ============================================================

# TODO: SQLite-backed cache with (at least) these tables —
#   - overpass_cache: query_hash -> raw JSON response, timestamp
#   - site_cache: domain -> enrichment result, timestamp (30-day TTL)
#   - geocode_cache: address -> lat/lon (permanent, only used if
#     the optional Nominatim geocoder is enabled)
# Re-running the same search must hit this cache, not the network.

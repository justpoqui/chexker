# ============================================================
# STEP 1 — SCAFFOLD: Overpass API + area-resolution module (placeholder)
# WHY: this is the only module allowed to talk to Overpass. Fixing
#      that boundary now means enrich.py, scoring.py and gui.py can
#      be written against a stable function signature later without
#      caring how the HTTP/query details work.
# ============================================================

# TODO (STEP 2): area resolution —
#   - Mode A: named-area Overpass query (admin_level=8 boundary),
#     with a picker for multiple name matches.
#   - Mode B: coordinates + radius (miles -> meters) "around" query.
#   - Optional, off-by-default Nominatim geocoding with a descriptive
#     User-Agent, a 1+ second sleep between requests, and a permanent
#     on-disk cache (see cache.py), per the Nominatim usage policy:
#     https://operations.osmfoundation.org/policies/nominatim/

# TODO (STEP 3): business query —
#   - nwr[...] query across shop / amenity / craft / office /
#     healthcare / leisure / tourism, each a GUI-toggleable key,
#     requiring ["name"].
#   - out center tags; so ways/relations return one coordinate.
#   - POST the query body (never a giant URL), one request at a
#     time, custom User-Agent "LocalLeadFinder/1.0 (personal lead
#     research)", exponential backoff on HTTP 429/504, and every
#     raw response cached in SQLite keyed by a hash of the query.

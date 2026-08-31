# ============================================================
# STEP 1 — SCAFFOLD: CSV export module (placeholder)
# WHY: exporting is a leaf feature that only needs the final,
#      scored result list — kept separate so gui.py doesn't grow
#      file-I/O and clipboard-formatting logic. Built out in STEP 7,
#      once scoring.py's tiers exist to write into the CSV header.
# ============================================================

# TODO (STEP 7):
#   - "Export CSV" writing: score, tier, name, category, phone,
#     website, facebook, full address, lat, lon, osm_id, flags,
#     date_checked, plus the required OSM/ODbL attribution line
#     in the CSV header (see README, STEP 8).
#   - "Copy as call sheet": top N leads to the clipboard as plain
#     text formatted for a phone call (name, phone, one-line reason).

# ============================================================
# STEP 1 — SCAFFOLD: website enrichment module (placeholder)
# WHY: enrichment (fetching a business's own site, checking
#      robots.txt, scanning for social links) is a separate
#      concern from scoring the result — keeping it in its own
#      module means scoring.py can stay pure and easy to test.
#      Built out in STEP 5, after scoring.py defines the tiers
#      this module's findings feed into.
# ============================================================

# TODO (STEP 5): for every business with a real website —
#   - Honor robots.txt via urllib.robotparser before fetching;
#     mark NOT_CHECKED if disallowed, never fetch anyway.
#   - GET with a 10s timeout, stream=True, read at most 500 KB,
#     then close the connection.
#   - Detect connection errors / DNS failures / 4xx / 5xx.
#   - Detect a redirect to a Facebook/Instagram URL.
#   - Regex the HTML for facebook/instagram/x.com/twitter/tiktok/
#     linkedin/youtube links.
#   - Flag missing <meta name="viewport"> and stale copyright
#     years as soft signals, not hard judgments.
#   - Sleep 1-2 seconds between different domains, run in a
#     thread pool of at most 4 workers, cache by domain with a
#     30-day TTL (via cache.py).

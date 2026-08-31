# ============================================================
# STEP 1 — SCAFFOLD: lead scoring module (placeholder)
# WHY: scoring is pure logic — raw OSM tags (and, once enrich.py
#      has run, its findings) in, a tier + numeric score out. No
#      network, no Tkinter, so it can be unit-tested on its own.
#      Built out in STEP 4.
# ============================================================

# TODO (STEP 4): tiers, highest score first —
#   NO_PRESENCE=100, FACEBOOK_ONLY=90, SOCIAL_ONLY=85,
#   SITE_NO_SOCIAL=60, WEAK_SITE=40, HEALTHY=0, plus a +15 bonus
#   for having a phone number. The website-is-actually-a-social-
#   platform check must parse the URL with urllib.parse and
#   compare the registered domain — never a substring test like
#   "facebook" in url, which false-positives on domains such as
#   facebookmarketingpros.com.

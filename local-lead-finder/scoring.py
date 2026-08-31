# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: scoring is pure logic — a Business (and, once available,
#      an EnrichmentResult) in, a tier + numeric score out. No
#      network, no Tkinter, so every rule here can be exercised
#      and tested without hitting Overpass or a real website.
# ============================================================

import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from osm_source import Business

# ============================================================
# STEP 3 — SCORING: turn raw OSM tags into a lead score
# WHY: a business with no website at all is the hottest lead,
#      so it must sort to the top of the results table.
# ============================================================
#
# Tier            Score  Condition
# --------------- -----  ---------------------------------------------------
# NO_PRESENCE       100  No website, no contact:* social tag at all
# FACEBOOK_ONLY      90  The "website" field's domain is actually a social
#                        platform (facebook.com, fb.me, instagram.com, ...)
# SOCIAL_ONLY        85  No website, but a contact:facebook/instagram tag
#                        exists
# SITE_NO_SOCIAL     60  Real website, but no social links found on it
#                        (requires STEP 5 enrichment; the default before
#                        enrichment has run)
# WEAK_SITE          40  Real website that fails a health check
# HEALTHY             0  Real site with social links, loads fine
#
# Plus a flat +15 bonus whenever a phone number exists, since a lead with
# no way to contact them is a dead end no matter how weak their web
# presence is.

TIER_BASE_SCORES = {
    "NO_PRESENCE": 100,
    "FACEBOOK_ONLY": 90,
    "SOCIAL_ONLY": 85,
    "SITE_NO_SOCIAL": 60,
    "WEAK_SITE": 40,
    "HEALTHY": 0,
}

PHONE_BONUS = 15


# ============================================================
# STEP 4 — DOMAIN CHECK: is the "website" field actually a
#           social platform in disguise?
# WHY: a naive `"facebook" in url` substring test false-positives
#      on real independent businesses like facebookmarketingpros.com.
#      Parsing the URL and comparing the *registered* domain avoids
#      that, at the cost of needing a (small, non-exhaustive) list
#      of two-label public suffixes so eg. "shop.co.uk" isn't
#      mis-parsed as domain "co.uk".
# ============================================================

# Domains that mean "this isn't really an independent website" per the
# spec's tier table. `linktr.ee` is the real Linktree domain; `linktree.com`
# is included too since the spec's table names the product "linktree"
# without a TLD, and that alias has pointed at Linktree in the past.
SOCIAL_PLATFORM_DOMAINS = {
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
    "linktr.ee",
    "linktree.com",
    "business.site",
}

# Common two-label public suffixes where the *registrable* domain is three
# labels (e.g. "example.co.uk", not "co.uk"). Not exhaustive — good enough
# to keep this app's own domain comparisons from misfiring on the most
# common non-.com business sites.
COMPOUND_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk",
    "co.nz", "co.jp", "co.za",
    "com.au", "com.br", "com.mx",
}


def extract_registered_domain(url: str) -> Optional[str]:
    """Return the registrable domain of `url` (e.g. "joeshair.com"), or None.

    Accepts URLs with or without a scheme (OSM `website` tags are
    inconsistent about including "https://"). Returns None for anything
    that doesn't parse to a usable hostname.
    """
    if not url:
        return None
    candidate = url if "://" in url else f"//{url}"
    try:
        hostname = urllib.parse.urlparse(candidate).hostname
    except ValueError:
        return None
    if not hostname:
        return None

    labels = hostname.lower().split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in COMPOUND_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_social_platform_domain(url: str) -> bool:
    domain = extract_registered_domain(url)
    return domain in SOCIAL_PLATFORM_DOMAINS if domain else False


# ============================================================
# STEP 4 — enrichment input shape
# WHY: SITE_NO_SOCIAL / WEAK_SITE / HEALTHY can't be decided from
#      OSM tags alone — they depend on actually fetching the site,
#      which is STEP 5's job. Defining that result shape here (not
#      in enrich.py) keeps the *scoring rules* — what each field
#      means for the tier — in one place; enrich.py will construct
#      these once it exists.
# ============================================================

@dataclass
class EnrichmentResult:
    checked: bool = False  # False = not yet fetched, or robots.txt disallowed
    loads: Optional[bool] = None
    redirected_to_social: Optional[str] = None  # domain, if the site redirects to one
    social_links_found: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # soft signals, e.g. "missing_viewport"


@dataclass
class ScoreResult:
    tier: str
    score: int
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


# ============================================================
# STEP 4 — the scoring function itself
# ============================================================

def score_business(
    business: Business,
    enrichment: Optional[EnrichmentResult] = None,
) -> ScoreResult:
    """Classify one Business into a tier and score.

    `enrichment` is None (or has `checked=False`) when the business hasn't
    been through STEP 5's website fetch yet — this happens for every
    business the instant it comes back from Overpass, and briefly for
    everything with a real website while the enrichment thread pool works
    through the queue.
    """
    reasons: list[str] = []
    has_social_tag = bool(business.facebook or business.instagram)
    website_domain = extract_registered_domain(business.website) if business.website else None

    if website_domain and website_domain in SOCIAL_PLATFORM_DOMAINS:
        tier = "FACEBOOK_ONLY"
        reasons.append(f'"Website" field just points to {website_domain}.')

    elif not business.website and not has_social_tag:
        tier = "NO_PRESENCE"
        reasons.append("No website and no social-media tag in OSM at all.")

    elif not business.website and has_social_tag:
        tier = "SOCIAL_ONLY"
        platform = "Facebook" if business.facebook else "Instagram"
        reasons.append(f"No website, but OSM lists a {platform} page.")

    elif enrichment is None or not enrichment.checked:
        tier = "SITE_NO_SOCIAL"
        reasons.append("Has a real website; not yet checked for health or social links.")

    elif enrichment.redirected_to_social:
        tier = "FACEBOOK_ONLY"
        reasons.append(f"Website redirects to {enrichment.redirected_to_social}.")

    elif not enrichment.loads:
        tier = "WEAK_SITE"
        reasons.append("Website did not load (connection error, timeout, or bad status code).")

    elif enrichment.social_links_found:
        tier = "HEALTHY"
        reasons.append("Website loads and links to its own social pages.")

    else:
        tier = "SITE_NO_SOCIAL"
        reasons.append("Website loads, but no social links were found on it.")

    score = TIER_BASE_SCORES[tier]

    if business.phone:
        score += PHONE_BONUS
        reasons.append(f"+{PHONE_BONUS}: has a phone number.")

    flags = list(enrichment.flags) if enrichment else []

    return ScoreResult(tier=tier, score=score, reasons=reasons, flags=flags)


# ============================================================
# STEP 4 — manual smoke test
# WHY: this module is pure logic, so unlike osm_source.py it can
#      be exercised directly with hand-built Business records
#      instead of needing a mocked network call.
# ============================================================

if __name__ == "__main__":
    def make_business(**overrides) -> Business:
        defaults = dict(
            osm_type="node", osm_id=1, name="Test Biz", category_key="shop",
            category_value="hairdresser", lat=0.0, lon=0.0, phone=None,
            website=None, facebook=None, instagram=None, address="",
            opening_hours=None, raw_tags={},
        )
        defaults.update(overrides)
        return Business(**defaults)

    cases = [
        ("no presence, no phone", make_business(), None),
        ("no presence, with phone", make_business(phone="555-1234"), None),
        (
            "facebook as website",
            make_business(website="https://www.facebook.com/joeshair"),
            None,
        ),
        (
            "NOT a false-positive facebook match",
            make_business(website="https://facebookmarketingpros.com"),
            None,
        ),
        ("social tag only", make_business(facebook="https://facebook.com/kims"), None),
        (
            "real site, not yet enriched",
            make_business(website="https://joeshair.example.com"),
            None,
        ),
        (
            "real site, redirects to instagram",
            make_business(website="https://joeshair.example.com"),
            EnrichmentResult(checked=True, redirected_to_social="instagram.com"),
        ),
        (
            "real site, broken",
            make_business(website="https://joeshair.example.com"),
            EnrichmentResult(checked=True, loads=False),
        ),
        (
            "real site, healthy",
            make_business(website="https://joeshair.example.com", phone="555-1234"),
            EnrichmentResult(checked=True, loads=True, social_links_found=["facebook.com/joeshair"]),
        ),
        (
            "real site, loads but no social",
            make_business(website="https://joeshair.example.com"),
            EnrichmentResult(checked=True, loads=True, social_links_found=[]),
        ),
    ]

    for label, business, enrichment in cases:
        result = score_business(business, enrichment)
        print(f"{label:38s} -> {result.tier:15s} score={result.score:3d}  {result.reasons}  flags={result.flags}")

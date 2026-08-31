# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: enrichment (fetching a business's own site, checking
#      robots.txt, scanning for social links) is a separate
#      concern from scoring the result — keeping it in its own
#      module means scoring.py can stay pure and easy to test.
# ============================================================

import random
import re
import threading
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from cache import Cache
from osm_source import USER_AGENT, Business
from scoring import EnrichmentResult, extract_registered_domain, is_social_platform_domain

FETCH_TIMEOUT_SECONDS = 10
MAX_BYTES = 500 * 1024
MAX_WORKERS = 4
SITE_CACHE_TTL_SECONDS = 30 * 24 * 3600
MIN_DOMAIN_SLEEP_SECONDS = 1.0
MAX_DOMAIN_SLEEP_SECONDS = 2.0
STALE_COPYRIGHT_YEARS = 2


# ============================================================
# STEP 5 — ROBOTS.TXT: never fetch what a site asks us not to
# WHY: honoring robots.txt is table stakes for polite scraping.
#      urllib.robotparser does the actual rule matching; we fetch
#      the robots.txt body ourselves (via requests, with the same
#      timeout as everything else) so a slow or hanging robots.txt
#      can't stall a worker indefinitely — robotparser's own
#      .read() has no timeout at all.
# ============================================================

def _robots_allows(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    parser = urllib.robotparser.RobotFileParser()
    try:
        response = requests.get(
            robots_url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT_SECONDS
        )
    except requests.RequestException:
        return True  # can't reach robots.txt; don't block on a network hiccup

    if response.status_code >= 400:
        return True  # no robots.txt published => nothing to disallow

    parser.parse(response.text.splitlines())
    return parser.can_fetch(USER_AGENT, url)


# ============================================================
# STEP 5 — FETCH: bounded GET, then hand the HTML to the analyzers
# WHY: a lead-research tool has no business downloading an entire
#      site — 500 KB is enough HTML to find a footer and a nav bar,
#      and streaming lets us stop reading well before that if the
#      server tries to send more.
# ============================================================

def _read_limited(response: requests.Response, max_bytes: int) -> str:
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    return b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")


HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
VIEWPORT_RE = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.IGNORECASE)
COPYRIGHT_YEAR_RE = re.compile(r'(?:©|\(c\)|copyright)\D{0,12}(\d{4})', re.IGNORECASE)

SOCIAL_LINK_PATTERNS = {
    "facebook": re.compile(r"facebook\.com", re.IGNORECASE),
    "instagram": re.compile(r"instagram\.com", re.IGNORECASE),
    "twitter/x": re.compile(r"(?:twitter\.com|x\.com)", re.IGNORECASE),
    "tiktok": re.compile(r"tiktok\.com", re.IGNORECASE),
    "linkedin": re.compile(r"linkedin\.com", re.IGNORECASE),
    "youtube": re.compile(r"youtube\.com|youtu\.be", re.IGNORECASE),
}


def _find_social_links(html: str) -> list[str]:
    hrefs = HREF_RE.findall(html)
    return [
        platform
        for platform, pattern in SOCIAL_LINK_PATTERNS.items()
        if any(pattern.search(href) for href in hrefs)
    ]


def _soft_flags(html: str) -> list[str]:
    """Soft, non-judgmental "this site might be neglected" signals."""
    flags = []
    if not VIEWPORT_RE.search(html):
        flags.append("missing_viewport (may not be mobile-responsive)")

    # A footer copyright notice often has more than one year mentioned
    # ("2015-2019"); the last match in the page is usually the most recent.
    years = [int(m.group(1)) for m in COPYRIGHT_YEAR_RE.finditer(html)]
    if years:
        newest_year = max(years)
        current_year = datetime.now(timezone.utc).year
        if current_year - newest_year > STALE_COPYRIGHT_YEARS:
            flags.append(f"stale_copyright_year ({newest_year})")

    return flags


def fetch_and_analyze(url: str) -> EnrichmentResult:
    """Fetch one website once and return what STEP 4's scoring needs.

    Never raises for network problems — those become `loads=False` with a
    flag describing what went wrong, since a broken site is exactly the
    WEAK_SITE case scoring.py already knows how to handle.
    """
    if not _robots_allows(url):
        return EnrichmentResult(
            checked=False, flags=["NOT_CHECKED: robots.txt disallows fetching this page"]
        )

    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=FETCH_TIMEOUT_SECONDS,
            stream=True,
        )
    except requests.RequestException as exc:
        return EnrichmentResult(checked=True, loads=False, flags=[f"fetch_error: {exc.__class__.__name__}"])

    try:
        if is_social_platform_domain(response.url):
            redirected_domain = extract_registered_domain(response.url)
            return EnrichmentResult(checked=True, loads=True, redirected_to_social=redirected_domain)

        if response.status_code >= 400:
            return EnrichmentResult(checked=True, loads=False, flags=[f"http_{response.status_code}"])

        html = _read_limited(response, MAX_BYTES)
    finally:
        response.close()

    return EnrichmentResult(
        checked=True,
        loads=True,
        social_links_found=_find_social_links(html),
        flags=_soft_flags(html),
    )


# ============================================================
# STEP 5 — ORCHESTRATION: one fetch per domain, cached, throttled,
#           spread across a small thread pool
# WHY: several Businesses can share one website (franchise
#      locations, a plaza with one shared site tag), so grouping
#      by domain avoids fetching the same page twice in one run —
#      on top of the 30-day on-disk cache that avoids it across runs.
#      The 1-2 second gap between *new* domain fetches is enforced
#      by one shared rate limiter so it holds even with 4 workers
#      running concurrently.
# ============================================================

def _hostname(url: str) -> Optional[str]:
    """The full hostname a URL points at (unlike scoring's registered-domain
    reduction, this must NOT collapse "shared.example.com" and
    "other.example.com" to the same key — they are different sites, even
    though both happen to be registered under example.com).
    """
    candidate = url if "://" in url else f"//{url}"
    try:
        hostname = urllib.parse.urlparse(candidate).hostname
    except ValueError:
        return None
    return hostname.lower() if hostname else None


class _DomainRateLimiter:
    def __init__(self, min_delay: float, max_delay: float):
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait_turn(self) -> None:
        with self._lock:
            delay = random.uniform(self._min_delay, self._max_delay)
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < delay:
                time.sleep(delay - elapsed)
            self._last_request_at = time.monotonic()


def enrich_businesses(
    businesses: list[Business],
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
    on_result: Callable[[Business, EnrichmentResult], None] = lambda business, result: None,
) -> dict[int, EnrichmentResult]:
    """Enrich every business with a real (non-social) website.

    Returns a dict of business.osm_id -> EnrichmentResult, one entry per
    business whose website was worth checking. Businesses already resolved
    to NO_PRESENCE / SOCIAL_ONLY / FACEBOOK_ONLY from OSM tags alone don't
    need a fetch at all, and are simply absent from the result.

    `on_result` fires once per business as soon as its host's check
    completes (not only when the whole batch finishes) — STEP 6's GUI uses
    this to update table rows live instead of showing nothing until every
    website has been checked.
    """
    businesses_by_domain: dict[str, list[Business]] = {}
    for business in businesses:
        if not business.website or is_social_platform_domain(business.website):
            continue
        host = _hostname(business.website)
        if host:
            businesses_by_domain.setdefault(host, []).append(business)

    if not businesses_by_domain:
        return {}

    limiter = _DomainRateLimiter(MIN_DOMAIN_SLEEP_SECONDS, MAX_DOMAIN_SLEEP_SECONDS)
    domains = list(businesses_by_domain.keys())
    total = len(domains)
    completed = 0
    progress_lock = threading.Lock()

    def check_one_domain(domain: str) -> tuple[str, EnrichmentResult]:
        cached = cache.get_site(domain, SITE_CACHE_TTL_SECONDS)
        if cached is not None:
            return domain, EnrichmentResult(**cached)

        limiter.wait_turn()
        url = businesses_by_domain[domain][0].website
        result = fetch_and_analyze(url)
        cache.set_site(domain, asdict(result))
        return domain, result

    results: dict[int, EnrichmentResult] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(check_one_domain, domain) for domain in domains]
        for future in as_completed(futures):
            domain, result = future.result()
            for business in businesses_by_domain[domain]:
                results[business.osm_id] = result
                on_result(business, result)
            with progress_lock:
                completed += 1
                status_cb(f"Checked {completed}/{total} websites ({domain})")

    return results


# ============================================================
# STEP 5 — manual smoke test
# WHY: spins up a throwaway local HTTP server so the fetch/parse
#      logic above can be exercised end-to-end without needing
#      real internet access (this sandbox's egress policy blocks
#      arbitrary outbound hosts anyway) and without being polite-
#      test-traffic against someone else's real website.
# ============================================================

if __name__ == "__main__":
    import http.server
    import threading as _threading

    HEALTHY_HTML = b"""
    <html><head><meta name="viewport" content="width=device-width"></head>
    <body>
      <nav><a href="https://facebook.com/example">Facebook</a></nav>
      <footer>&copy; 2026 Example Co.</footer>
    </body></html>
    """
    STALE_HTML = b"""
    <html><head></head>
    <body><footer>Copyright 2016 Old Shop</footer></body></html>
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /blocked\n"
            elif self.path == "/blocked":
                body = b"should never be fetched"
            elif self.path == "/stale":
                body = STALE_HTML
            else:
                body = HEALTHY_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep the smoke-test output readable

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    _threading.Thread(target=server.serve_forever, daemon=True).start()

    print("-- healthy page --")
    result = fetch_and_analyze(f"http://127.0.0.1:{port}/")
    print(result)
    assert result.checked and result.loads
    assert "facebook" in result.social_links_found
    assert not result.flags  # has a viewport and a fresh copyright year

    print("-- stale page, no viewport --")
    result = fetch_and_analyze(f"http://127.0.0.1:{port}/stale")
    print(result)
    assert any("missing_viewport" in f for f in result.flags)
    assert any("stale_copyright_year" in f for f in result.flags)

    print("-- robots.txt disallows /blocked --")
    result = fetch_and_analyze(f"http://127.0.0.1:{port}/blocked")
    print(result)
    assert result.checked is False

    print("-- unreachable domain --")
    result = fetch_and_analyze("http://127.0.0.1:1/nope")
    print(result)
    assert result.checked and result.loads is False

    server.shutdown()
    print("ALL OK")

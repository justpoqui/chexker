# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: this is the only module allowed to talk to Overpass (and,
#      optionally, Nominatim). Keeping that boundary fixed means
#      every other module works with plain Python data (dataclasses,
#      dicts) and never touches HTTP directly.
# ============================================================

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from cache import Cache

# STEP 14: the main instance throttles or goes down often enough that a
# single hardcoded URL isn't reliable. Add a mirror here (one line) to grow
# the rotation; query_overpass() tries them in order and moves to the next
# one whenever the current one fails, rather than giving up.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "LocalLeadFinder/1.0 (personal lead research)"

# Nominatim asks that the User-Agent identify the actual application and,
# ideally, a way to contact its operator. Edit the contact detail below to
# your own address before enabling the optional geocoder in the GUI.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = (
    "LocalLeadFinder/1.0 (personal lead research; contact: set-your-email@example.com)"
)
NOMINATIM_MIN_INTERVAL_SECONDS = 1.1  # policy requires "at most 1 request/second"

# Exponential backoff schedule (seconds) used on Overpass 429 / 504 responses.
OVERPASS_BACKOFF_SCHEDULE = (2, 4, 8, 16)


# ============================================================
# STEP 2 — AREA RESOLUTION: shared data types
# WHY: STEP 3's business query needs either an Overpass area id
#      (Mode A: named place) or a center point + radius (Mode B:
#      coordinates). One small dataclass lets the rest of the app
#      handle "the area to search" as a single value regardless
#      of which mode produced it.
# ============================================================

@dataclass
class AreaCandidate:
    """One administrative-boundary match for a searched name."""

    osm_type: str  # "relation" or "way"
    osm_id: int
    name: str
    admin_level: Optional[str]
    detail: str  # extra tags (state/country) shown to disambiguate in a picker

    @property
    def area_id(self) -> int:
        # Overpass's documented convention for turning an OSM element into
        # an area id: ways get a 2.4 billion offset, relations a 3.6 billion
        # offset. This lets STEP 3 write area(id:...) directly, with no
        # second name lookup needed.
        offset = 2_400_000_000 if self.osm_type == "way" else 3_600_000_000
        return offset + self.osm_id

    def __str__(self) -> str:
        level = f", admin_level {self.admin_level}" if self.admin_level else ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"{self.name}{level}{detail}"


@dataclass
class SearchArea:
    """The resolved area a STEP 3 business query should run against."""

    mode: str  # "named" or "radius"
    label: str
    area_id: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_m: Optional[float] = None


PickerFn = Callable[[list[AreaCandidate]], Optional[AreaCandidate]]


# ============================================================
# STEP 2 — OVERPASS TRANSPORT: cached, throttled, backing-off POST
# WHY: Overpass's fair-use rules require a real User-Agent, one
#      request at a time, the query in the POST body (not a giant
#      URL), and backing off on 429 ("too many requests") or 504
#      ("timed out server-side") instead of hammering it again.
#      Every raw response is cached by a hash of the query text so
#      re-running the same search costs nothing on the network.
# ============================================================

def _query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _query_one_endpoint(
    endpoint: str,
    query: str,
    status_cb: Callable[[str], None],
) -> str:
    """POST `query` to one Overpass endpoint, backing off on 429/504.

    Returns the raw response text on success. Raises requests.RequestException
    (a connection failure, or a non-2xx status after the backoff schedule is
    exhausted) so the caller — STEP 14's mirror rotation — knows to move on.
    """
    delays = iter(OVERPASS_BACKOFF_SCHEDULE)
    while True:
        status_cb(f"Querying Overpass ({endpoint})...")
        response = requests.post(
            endpoint,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=120,
        )
        if response.status_code == 200:
            return response.text

        if response.status_code in (429, 504):
            try:
                delay = next(delays)
            except StopIteration:
                response.raise_for_status()
            status_cb(
                f"{endpoint} returned {response.status_code}; "
                f"backing off {delay}s before retrying..."
            )
            time.sleep(delay)
            continue

        response.raise_for_status()


# ============================================================
# STEP 14 — MIRROR FAILOVER
# WHY: the flagship overpass-api.de instance throttles or goes
#      down often enough that a single hardcoded URL isn't
#      reliable for anything but a quick test. Rotating to the
#      next mirror in OVERPASS_ENDPOINTS (after that mirror's own
#      backoff schedule is exhausted, or on an outright connection
#      failure) means one instance having a bad day doesn't stop
#      the search — it only slows the first query against it down.
# ============================================================

def query_overpass(
    query: str,
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> dict:
    """Run one Overpass QL query, using the cache when possible.

    Tries each endpoint in OVERPASS_ENDPOINTS in order, moving to the next
    one whenever the current one fails (after its own backoff schedule is
    exhausted). Raises the last endpoint's requests.RequestException only if
    every endpoint in the list failed.
    """
    query_hash = _query_hash(query)
    cached = cache.get_overpass(query_hash)
    if cached is not None:
        status_cb("Loaded from cache (no Overpass request made).")
        return json.loads(cached)

    last_error: Optional[Exception] = None
    for i, endpoint in enumerate(OVERPASS_ENDPOINTS):
        try:
            response_text = _query_one_endpoint(endpoint, query, status_cb)
        except requests.RequestException as exc:
            last_error = exc
            if i + 1 < len(OVERPASS_ENDPOINTS):
                status_cb(
                    f"{endpoint} failed ({exc.__class__.__name__}); "
                    f"trying {OVERPASS_ENDPOINTS[i + 1]}..."
                )
            continue

        cache.set_overpass(query_hash, query, response_text)
        return json.loads(response_text)

    raise last_error


# ============================================================
# STEP 2 — MODE A: named-area resolution, with a picker for
#           ambiguous names ("Springfield" exists in many states)
# WHY: rather than guessing which same-named place the user means,
#      find every administrative boundary with that name and hand
#      the list to a picker function. A GUI (STEP 6) will supply a
#      real dialog; until then, `terminal_picker` below lets this
#      module be tested from the command line.
# ============================================================

def _escape_ql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def find_named_area_candidates(
    name: str,
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> list[AreaCandidate]:
    """Look up every admin_level=8 boundary matching `name`."""
    escaped = _escape_ql_string(name)
    query = (
        "[out:json][timeout:60];\n"
        "(\n"
        f'  relation["name"="{escaped}"]["boundary"="administrative"]["admin_level"="8"];\n'
        f'  way["name"="{escaped}"]["boundary"="administrative"]["admin_level"="8"];\n'
        ");\n"
        "out tags center;"
    )
    data = query_overpass(query, cache, status_cb)

    candidates = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        detail_parts = [
            tags.get(key)
            for key in ("addr:state", "is_in:state", "is_in", "addr:country")
            if tags.get(key)
        ]
        candidates.append(
            AreaCandidate(
                osm_type=element["type"],
                osm_id=element["id"],
                name=tags.get("name", name),
                admin_level=tags.get("admin_level"),
                detail=", ".join(dict.fromkeys(detail_parts)),
            )
        )
    return candidates


def terminal_picker(candidates: list[AreaCandidate]) -> Optional[AreaCandidate]:
    """A no-GUI picker for testing this module from the command line."""
    print(f"Found {len(candidates)} places with that name:")
    for i, candidate in enumerate(candidates, start=1):
        print(f"  {i}. {candidate}")
    choice = input("Pick one (number), or blank to cancel: ").strip()
    if not choice:
        return None
    return candidates[int(choice) - 1]


def resolve_named_area(
    name: str,
    cache: Cache,
    picker: Optional[PickerFn] = None,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> Optional[SearchArea]:
    """Resolve a place name to a SearchArea, prompting `picker` on ambiguity.

    Returns None if no matches were found, or if the picker returned None
    (the user cancelled).
    """
    candidates = find_named_area_candidates(name, cache, status_cb)
    if not candidates:
        return None

    if len(candidates) == 1:
        chosen = candidates[0]
    else:
        chosen = (picker or terminal_picker)(candidates)
        if chosen is None:
            return None

    return SearchArea(mode="named", label=str(chosen), area_id=chosen.area_id)


# ============================================================
# STEP 2 — MODE B: coordinates + radius
# WHY: Overpass's `around` filter takes meters, but a US audience
#      thinks in miles, so the GUI's radius slider (STEP 6) will
#      be in miles and this is the one place that converts.
# ============================================================

MILES_TO_METERS = 1609.344


def make_radius_area(lat: float, lon: float, radius_miles: float) -> SearchArea:
    radius_m = radius_miles * MILES_TO_METERS
    label = f"{radius_miles:g} mi around ({lat:.4f}, {lon:.4f})"
    return SearchArea(mode="radius", label=label, lat=lat, lon=lon, radius_m=radius_m)


# ============================================================
# STEP 3 — BUSINESS QUERY: pulling named, business-like objects
# WHY: this is the query that actually produces leads. It covers
#      both SearchArea modes from STEP 2, requires a name tag
#      (an unnamed shop can't be pitched or called), and uses
#      `out center tags meta;` so ways/relations come back with one
#      representative coordinate instead of full geometry — much
#      smaller responses for the same information. `meta` (added in
#      STEP 12) is what puts a last-edit timestamp on every element.
# ============================================================

# Every one of these is a toggle in the GUI (STEP 6); all are on by default.
ALL_CATEGORY_KEYS = ("shop", "amenity", "craft", "office", "healthcare", "leisure", "tourism")


@dataclass
class Business:
    """One named, business-like OSM object — a candidate lead."""

    osm_type: str
    osm_id: int
    name: str
    category_key: str
    category_value: str
    lat: Optional[float]
    lon: Optional[float]
    phone: Optional[str]
    website: Optional[str]
    facebook: Optional[str]
    instagram: Optional[str]
    address: str
    opening_hours: Optional[str]
    raw_tags: dict = field(default_factory=dict)
    osm_timestamp: Optional[str] = None  # last-edit time, e.g. "2020-01-15T08:23:41Z" (STEP 12)
    osm_version: Optional[int] = None

    @property
    def osm_key(self) -> str:
        # osm_id alone isn't unique across element types — a node and a way
        # can share the same numeric id — so anything that needs a stable,
        # collision-free identifier for a business (the leads table, the
        # results table's row id, the CSV's osm_id column) uses this instead.
        return f"{self.osm_type}/{self.osm_id}"


def _validate_category_keys(category_keys) -> list[str]:
    keys = [key for key in category_keys if key in ALL_CATEGORY_KEYS]
    if not keys:
        raise ValueError(
            f"No valid category keys given; choose from {ALL_CATEGORY_KEYS}"
        )
    return keys


def build_business_query(area: SearchArea, category_keys) -> str:
    """Build the nwr query for one SearchArea, restricted to the given keys."""
    keys = _validate_category_keys(category_keys)

    if area.mode == "named":
        header = f"[out:json][timeout:90];\narea(id:{area.area_id})->.a;\n"
        filters = "\n".join(f'  nwr["{key}"]["name"](area.a);' for key in keys)
    elif area.mode == "radius":
        header = "[out:json][timeout:90];\n"
        around = f"around:{area.radius_m:.1f},{area.lat},{area.lon}"
        filters = "\n".join(f'  nwr["{key}"]["name"]({around});' for key in keys)
    else:
        raise ValueError(f"Unknown SearchArea mode: {area.mode!r}")

    # STEP 12: "meta" adds each element's last-edit timestamp/version to the
    # response, on top of "center tags" from STEP 3 — see Business.osm_timestamp.
    return f"{header}(\n{filters}\n);\nout center tags meta;"


def _format_address(tags: dict) -> str:
    street = " ".join(
        part for part in (tags.get("addr:housenumber"), tags.get("addr:street")) if part
    )
    locality = ", ".join(
        part
        for part in (tags.get("addr:city"), tags.get("addr:state"), tags.get("addr:postcode"))
        if part
    )
    return ", ".join(part for part in (street, locality) if part)


def _element_to_business(element: dict, category_keys: list[str]) -> Optional[Business]:
    tags = element.get("tags", {})
    name = tags.get("name")
    if not name:
        return None

    # Use whichever requested category key this element actually has; an
    # object can carry more than one (e.g. shop + office), so the first
    # match in the caller's key order wins.
    category_key = next((key for key in category_keys if key in tags), None)
    if category_key is None:
        return None

    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")

    return Business(
        osm_type=element["type"],
        osm_id=element["id"],
        name=name,
        category_key=category_key,
        category_value=tags[category_key],
        lat=lat,
        lon=lon,
        phone=tags.get("phone") or tags.get("contact:phone"),
        website=tags.get("website") or tags.get("contact:website") or tags.get("url"),
        facebook=tags.get("contact:facebook"),
        instagram=tags.get("contact:instagram"),
        address=_format_address(tags),
        opening_hours=tags.get("opening_hours"),
        raw_tags=tags,
        osm_timestamp=element.get("timestamp"),
        osm_version=element.get("version"),
    )


def search_businesses(
    area: SearchArea,
    category_keys,
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> list[Business]:
    """Run the STEP 3 business query for `area` and return parsed Businesses."""
    keys = _validate_category_keys(category_keys)
    query = build_business_query(area, keys)
    data = query_overpass(query, cache, status_cb)

    businesses = [
        business
        for element in data.get("elements", [])
        if (business := _element_to_business(element, keys)) is not None
    ]
    status_cb(f"{len(businesses)} named businesses found.")
    return businesses


# ============================================================
# STEP 2 — OPTIONAL: Nominatim geocoding (off by default)
# WHY: turning a typed street address into lat/lon needs a
#      geocoder, and Nominatim is the only free, key-less option.
#      Its usage policy is strict: a real User-Agent, at most one
#      request per second, and permanent local caching of every
#      result — see https://operations.osmfoundation.org/policies/nominatim/
#      This function is never called unless a future GUI checkbox
#      (off by default) turns it on.
# ============================================================

_last_nominatim_request_at = 0.0


def geocode_address(address: str, cache: Cache) -> Optional[tuple[float, float]]:
    """Look up an address via Nominatim, permanently caching the result."""
    global _last_nominatim_request_at

    address_key = address.strip().lower()
    cached = cache.get_geocode(address_key)
    if cached is not None:
        return cached

    elapsed = time.monotonic() - _last_nominatim_request_at
    if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
        time.sleep(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)

    response = requests.get(
        NOMINATIM_URL,
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        timeout=10,
    )
    _last_nominatim_request_at = time.monotonic()
    response.raise_for_status()
    results = response.json()
    if not results:
        return None

    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    cache.set_geocode(address_key, lat, lon)
    return (lat, lon)


# ============================================================
# STEP 2/3 — manual smoke test
# WHY: gui.py doesn't exist yet, so this lets area resolution and
#      the business query be exercised from the terminal before
#      scoring.py (STEP 4) has anything to consume them.
# ============================================================

if __name__ == "__main__":
    cache = Cache()

    if len(sys.argv) >= 2 and sys.argv[1] == "radius":
        _, _, lat, lon, radius = sys.argv
        area = make_radius_area(float(lat), float(lon), float(radius))
        print(area)

    elif len(sys.argv) >= 2 and sys.argv[1] == "businesses":
        _, _, place_name, *category_keys = sys.argv
        area = resolve_named_area(place_name, cache, status_cb=print)
        if area is None:
            print("No match found (or selection cancelled).")
        else:
            print(f"Searching {area.label} ...")
            businesses = search_businesses(
                area, category_keys or ALL_CATEGORY_KEYS, cache, status_cb=print
            )
            for business in businesses[:20]:
                print(
                    f"- {business.name} [{business.category_value}] "
                    f"phone={business.phone} website={business.website} "
                    f"fb={business.facebook}"
                )

    elif len(sys.argv) >= 2:
        place_name = " ".join(sys.argv[1:])
        area = resolve_named_area(place_name, cache, status_cb=print)
        print(area if area else "No match found (or selection cancelled).")

    else:
        print("Usage:")
        print('  python osm_source.py "Grand Rapids"')
        print("  python osm_source.py radius 42.9634 -85.6681 5")
        print('  python osm_source.py businesses "Grand Rapids" shop craft office')

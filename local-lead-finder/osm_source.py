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
# ideally, a way to contact its operator.
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = (
    "LocalLeadFinder/1.0 (personal lead research; contact: mardarv96@gmail.com)"
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
    """One place matching a searched name: either a real administrative
    boundary (admin_level set) or a place=* node/way with no boundary at
    all (place_value set instead) -- see find_named_area_candidates()."""

    osm_type: str  # "node", "way", or "relation"
    osm_id: int
    name: str
    admin_level: Optional[str]
    detail: str  # extra tags (state/country) shown to disambiguate in a picker
    place_value: Optional[str] = None  # e.g. "suburb", "census_designated_place"
    lat: Optional[float] = None
    lon: Optional[float] = None

    @property
    def area_id(self) -> int:
        # Overpass's documented convention for turning a way/relation into
        # an area id: ways get a 2.4 billion offset, relations a 3.6 billion
        # offset. This lets STEP 3 write area(id:...) directly, with no
        # second name lookup needed. A bare node (common for small
        # unincorporated communities -- see find_named_area_candidates) has
        # no polygon to convert; resolve_named_area() checks osm_type and
        # falls back to a radius search around it instead of calling this.
        if self.osm_type == "node":
            raise ValueError("a node candidate has no area id; use its lat/lon instead")
        offset = 2_400_000_000 if self.osm_type == "way" else 3_600_000_000
        return offset + self.osm_id

    def __str__(self) -> str:
        kind = self.admin_level and f", admin_level {self.admin_level}"
        kind = kind or (self.place_value and f", {self.place_value}") or ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"{self.name}{kind}{detail}"


@dataclass
class SearchArea:
    """The resolved area a STEP 3 business query should run against."""

    mode: str  # "named" or "radius"
    label: str
    area_id: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    radius_m: Optional[float] = None


PickerFn = Callable[[list[AreaCandidate]], list[AreaCandidate]]


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


# Unincorporated communities and Census-designated places (CDPs) are real,
# named towns -- Brandon, FL is one, along with its neighbors Valrico,
# Seffner, and Mango -- but very often have NO administrative boundary in
# OSM at all, only a place=* node/way marking where they are. Matching only
# boundary=administrative admin_level=8 (the original query) silently
# excludes every one of these; this list of place= values is what widens
# find_named_area_candidates() to also catch them.
PLACE_VALUES = (
    "city", "town", "village", "hamlet", "suburb", "neighbourhood",
    "unincorporated_community", "census_designated_place", "locality",
)

# US states + DC, for the GUI's optional state filter. Name -> ISO3166-2
# code. resolve_named_area() filters candidates against this state *after*
# fetching them (by tag when present, else by reverse-geocoding each one's
# center point) rather than asking Overpass to filter spatially up front --
# that turned out to depend on the area index being complete and current on
# whichever Overpass mirror answered, which isn't reliable enough to trust.
US_STATES = [
    ("Alabama", "US-AL"), ("Alaska", "US-AK"), ("Arizona", "US-AZ"), ("Arkansas", "US-AR"),
    ("California", "US-CA"), ("Colorado", "US-CO"), ("Connecticut", "US-CT"), ("Delaware", "US-DE"),
    ("District of Columbia", "US-DC"), ("Florida", "US-FL"), ("Georgia", "US-GA"), ("Hawaii", "US-HI"),
    ("Idaho", "US-ID"), ("Illinois", "US-IL"), ("Indiana", "US-IN"), ("Iowa", "US-IA"),
    ("Kansas", "US-KS"), ("Kentucky", "US-KY"), ("Louisiana", "US-LA"), ("Maine", "US-ME"),
    ("Maryland", "US-MD"), ("Massachusetts", "US-MA"), ("Michigan", "US-MI"), ("Minnesota", "US-MN"),
    ("Mississippi", "US-MS"), ("Missouri", "US-MO"), ("Montana", "US-MT"), ("Nebraska", "US-NE"),
    ("Nevada", "US-NV"), ("New Hampshire", "US-NH"), ("New Jersey", "US-NJ"), ("New Mexico", "US-NM"),
    ("New York", "US-NY"), ("North Carolina", "US-NC"), ("North Dakota", "US-ND"), ("Ohio", "US-OH"),
    ("Oklahoma", "US-OK"), ("Oregon", "US-OR"), ("Pennsylvania", "US-PA"), ("Rhode Island", "US-RI"),
    ("South Carolina", "US-SC"), ("South Dakota", "US-SD"), ("Tennessee", "US-TN"), ("Texas", "US-TX"),
    ("Utah", "US-UT"), ("Vermont", "US-VT"), ("Virginia", "US-VA"), ("Washington", "US-WA"),
    ("West Virginia", "US-WV"), ("Wisconsin", "US-WI"), ("Wyoming", "US-WY"),
]


def find_named_area_candidates(
    name: str,
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> list[AreaCandidate]:
    """Look up every place matching `name`: real admin_level=8 boundaries
    (incorporated cities/towns/villages) *and* place=* nodes/ways/relations
    (unincorporated communities and CDPs -- see the PLACE_VALUES comment
    above). A boundary candidate can be searched directly with
    area(id:...); a bare place node has no polygon, so resolve_named_area()
    falls back to a radius search centered on it instead.
    """
    escaped = _escape_ql_string(name)
    place_regex = "|".join(PLACE_VALUES)
    query = (
        "[out:json][timeout:60];\n"
        "(\n"
        f'  relation["name"="{escaped}"]["boundary"="administrative"]["admin_level"="8"];\n'
        f'  way["name"="{escaped}"]["boundary"="administrative"]["admin_level"="8"];\n'
        f'  node["name"="{escaped}"]["place"~"^({place_regex})$"];\n'
        f'  way["name"="{escaped}"]["place"~"^({place_regex})$"];\n'
        f'  relation["name"="{escaped}"]["place"~"^({place_regex})$"];\n'
        ");\n"
        "out tags center;"
    )
    data = query_overpass(query, cache, status_cb)

    candidates = []
    seen: set[tuple[str, int]] = set()
    for element in data.get("elements", []):
        key = (element["type"], element["id"])
        if key in seen:
            # A boundary relation that also carries place=town would
            # otherwise match both the admin_level and place clauses above.
            continue
        seen.add(key)

        tags = element.get("tags", {})
        if element["type"] == "node":
            lat, lon = element.get("lat"), element.get("lon")
        else:
            center = element.get("center", {})
            lat, lon = center.get("lat"), center.get("lon")

        # Real admin_level=8 boundary relations very often carry NONE of
        # addr:state/is_in — those are conventions for buildings and POIs,
        # not administrative boundaries. ISO3166-2 (e.g. "US-FL") and a
        # county tag are much more commonly present on the boundary itself.
        detail_parts = [
            tags.get(key)
            for key in (
                "addr:state", "is_in:state", "addr:county", "is_in:county",
                "ISO3166-2", "is_in", "addr:country",
            )
            if tags.get(key)
        ]
        detail = ", ".join(dict.fromkeys(detail_parts))
        # If OSM's own tags don't disambiguate, resolve_named_area() below
        # fills `detail` in via reverse geocoding once it knows whether this
        # name is actually ambiguous (no point spending a Nominatim call on
        # every single-match search).

        admin_level = tags.get("admin_level")
        candidates.append(
            AreaCandidate(
                osm_type=element["type"],
                osm_id=element["id"],
                name=tags.get("name", name),
                admin_level=admin_level,
                place_value=None if admin_level else tags.get("place"),
                detail=detail,
                lat=lat,
                lon=lon,
            )
        )
    return candidates


def terminal_picker(candidates: list[AreaCandidate]) -> list[AreaCandidate]:
    """A no-GUI picker for testing this module from the command line."""
    print(f"Found {len(candidates)} places with that name:")
    for i, candidate in enumerate(candidates, start=1):
        print(f"  {i}. {candidate}")
    choice = input(
        "Pick one or more (comma-separated numbers), or blank to cancel: "
    ).strip()
    if not choice:
        return []
    return [candidates[int(part.strip()) - 1] for part in choice.split(",") if part.strip()]


def _candidate_in_state(
    candidate: AreaCandidate,
    state_name: str,
    state_iso: Optional[str],
    cache: Cache,
) -> Optional[bool]:
    """True/False if we can tell whether `candidate` is in this US state;
    None if we couldn't determine it at all (e.g. Nominatim unreachable) --
    callers should keep candidates we're unsure about rather than risk
    hiding the one the user actually wants.

    Checks the candidate's own OSM tags first (cheap, but rarely present —
    see the detail-tag comment in find_named_area_candidates), then falls
    back to the same reverse-geocode lookup used for picker disambiguation.
    """
    if candidate.detail:
        haystack = candidate.detail.lower()
        if state_name.lower() in haystack or (state_iso and state_iso.lower() in haystack):
            return True

    if candidate.lat is None or candidate.lon is None:
        return None

    address = _reverse_geocode_address(candidate.lat, candidate.lon, cache)
    actual_state = (address.get("state") or "").strip().lower()
    if not actual_state:
        return None
    return actual_state == state_name.strip().lower()


def resolve_named_area(
    name: str,
    cache: Cache,
    picker: Optional[PickerFn] = None,
    status_cb: Callable[[str], None] = lambda msg: None,
    state_name: Optional[str] = None,
    state_iso: Optional[str] = None,
    point_radius_miles: float = 5.0,
) -> list[SearchArea]:
    """Resolve a place name to one or more SearchAreas, prompting `picker`
    on ambiguity. `picker` can return more than one candidate — e.g. OSM
    carries both a boundary and a separate place=* node for the same real
    place, or two genuinely different nearby places share a name — in
    which case every chosen candidate is searched and the caller (see
    search_businesses_multi()) merges the results.

    `state_name`/`state_iso` (e.g. "Florida"/"US-FL", from the GUI's
    optional state dropdown) narrow an ambiguous name down to one state,
    usually leaving a single match with no picker needed — see
    _candidate_in_state() above.

    A chosen candidate with no administrative boundary (a bare place=*
    node — see find_named_area_candidates) becomes a `point_radius_miles`
    radius search centered on it instead of an area(id:...) search, since
    there's no polygon to search within.

    Returns an empty list if no matches were found, or if the picker
    returned none (the user cancelled).
    """
    candidates = find_named_area_candidates(name, cache, status_cb)
    if not candidates:
        return []

    if state_name:
        status_cb(f"Narrowing {len(candidates)} match(es) to {state_name}...")
        filtered = [
            c for c in candidates
            if _candidate_in_state(c, state_name, state_iso, cache) is not False
        ]
        # If every candidate got ruled out, trust that over showing nothing
        # only when we could actually confirm it; otherwise (e.g. Nominatim
        # unreachable for all of them) fall back to the unfiltered list
        # rather than falsely reporting "no match found".
        if filtered:
            candidates = filtered

    if len(candidates) == 1:
        chosen_candidates = candidates
    else:
        # Only names that actually collide reach here, so it's worth a
        # Nominatim reverse-geocode call per undisambiguated candidate to
        # show a real "Hillsborough County, FL" instead of raw coordinates.
        for candidate in candidates:
            if candidate.detail or candidate.lat is None or candidate.lon is None:
                continue
            status_cb(f"Looking up {candidate.name}'s location to tell it apart...")
            label = _reverse_geocode_label(candidate.lat, candidate.lon, cache)
            candidate.detail = label or f"≈{candidate.lat:.2f}, {candidate.lon:.2f}"

        chosen_candidates = (picker or terminal_picker)(candidates)
        if not chosen_candidates:
            return []

    areas = []
    for chosen in chosen_candidates:
        if chosen.osm_type == "node":
            label = f"{point_radius_miles:g} mi around {chosen}"
            areas.append(make_radius_area(chosen.lat, chosen.lon, point_radius_miles, label=label))
        else:
            areas.append(SearchArea(mode="named", label=str(chosen), area_id=chosen.area_id))
    return areas


# ============================================================
# STEP 2 — MODE B: coordinates + radius
# WHY: Overpass's `around` filter takes meters, but a US audience
#      thinks in miles, so the GUI's radius slider (STEP 6) will
#      be in miles and this is the one place that converts.
# ============================================================

MILES_TO_METERS = 1609.344


def make_radius_area(
    lat: float, lon: float, radius_miles: float, label: Optional[str] = None
) -> SearchArea:
    radius_m = radius_miles * MILES_TO_METERS
    if label is None:
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


def search_businesses_multi(
    areas: list[SearchArea],
    category_keys,
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> list[Business]:
    """Run search_businesses() over every area — resolve_named_area() can
    return more than one when its picker selects multiple candidates — and
    merge the results, de-duplicating by osm_key since overlapping areas
    can easily surface the same business twice."""
    seen: set[str] = set()
    merged: list[Business] = []
    for area in areas:
        for business in search_businesses(area, category_keys, cache, status_cb):
            if business.osm_key in seen:
                continue
            seen.add(business.osm_key)
            merged.append(business)
    return merged


# ============================================================
# STEP 2/16 — NOMINATIM GEOCODING
# WHY: turning a typed address or ZIP/postal code into lat/lon
#      needs a geocoder, and Nominatim is the only free, key-less
#      option. Its usage policy is strict: a real User-Agent, at
#      most one request per second, and permanent local caching of
#      every result — see
#      https://operations.osmfoundation.org/policies/nominatim/
#      geocode_postal_code() is wired to the GUI's "ZIP/postal code"
#      search mode (STEP 16) — one geocode call per search, the same
#      order of request volume as resolving a named area, so it's a
#      normal always-available mode rather than a per-business
#      operation needing its own opt-in. geocode_address() is kept
#      available for the same purpose against a free-text address,
#      should a future mode want one.
# ============================================================

_last_nominatim_request_at = 0.0


def _nominatim_request(url: str, params: dict):
    """Rate-limited GET against a Nominatim endpoint. Returns parsed JSON."""
    global _last_nominatim_request_at

    elapsed = time.monotonic() - _last_nominatim_request_at
    if elapsed < NOMINATIM_MIN_INTERVAL_SECONDS:
        time.sleep(NOMINATIM_MIN_INTERVAL_SECONDS - elapsed)

    response = requests.get(
        url,
        params={**params, "format": "json"},
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        timeout=10,
    )
    _last_nominatim_request_at = time.monotonic()
    response.raise_for_status()
    return response.json()


def _nominatim_lookup(params: dict, cache_key: str, cache: Cache) -> Optional[tuple[float, float]]:
    """Shared Nominatim /search call: rate-limited, cached permanently."""
    cached = cache.get_geocode(cache_key)
    if cached is not None:
        return cached

    results = _nominatim_request(NOMINATIM_SEARCH_URL, {**params, "limit": 1})
    if not results:
        return None

    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    cache.set_geocode(cache_key, lat, lon)
    return (lat, lon)


def geocode_address(address: str, cache: Cache) -> Optional[tuple[float, float]]:
    """Look up a free-text address via Nominatim, permanently caching it."""
    cache_key = f"address:{address.strip().lower()}"
    return _nominatim_lookup({"q": address}, cache_key, cache)


def geocode_postal_code(postal_code: str, cache: Cache) -> Optional[tuple[float, float]]:
    """Look up a ZIP/postal code's approximate center via Nominatim.

    Uses Nominatim's structured `postalcode` parameter rather than free-text
    `q`, which resolves a bare code (US ZIP, Canadian postal code, etc.)
    much more reliably.
    """
    cache_key = f"postal:{postal_code.strip().lower()}"
    return _nominatim_lookup({"postalcode": postal_code}, cache_key, cache)


def _reverse_geocode_address(lat: float, lon: float, cache: Cache) -> dict:
    """Rate-limited, permanently-cached Nominatim /reverse lookup.

    Returns the raw "address" component dict (county/state/country_code/
    ...), or {} if the request failed or Nominatim had nothing for this
    point. Rounds to ~100m for the cache key since every caller here uses
    this to tell places apart, not for pinpoint accuracy — see
    find_named_area_candidates/resolve_named_area/_candidate_in_state.
    """
    cache_key = f"{lat:.3f},{lon:.3f}"
    cached = cache.get_reverse_geocode(cache_key)
    if cached is not None:
        try:
            return json.loads(cached) if cached else {}
        except json.JSONDecodeError:
            pass  # pre-JSON cache entry from an older version; refetch

    try:
        data = _nominatim_request(
            NOMINATIM_REVERSE_URL,
            {"lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1},
        )
    except requests.RequestException:
        return {}

    address = data.get("address", {}) if isinstance(data, dict) else {}
    cache.set_reverse_geocode(cache_key, json.dumps(address) if address else "")
    return address


def _reverse_geocode_label(lat: float, lon: float, cache: Cache) -> Optional[str]:
    """Reverse-geocode a point to a short "County, State" style label, for
    disambiguating same-named admin boundaries whose own OSM tags carry
    nothing useful (see find_named_area_candidates/resolve_named_area)."""
    address = _reverse_geocode_address(lat, lon, cache)
    county = address.get("county")
    state = address.get("state") or address.get("state_district")
    parts = [part for part in (county, state) if part]
    if not parts and address.get("country_code"):
        parts = [address["country_code"].upper()]
    return ", ".join(parts) if parts else None


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

    elif len(sys.argv) >= 2 and sys.argv[1] == "zip":
        _, _, zip_code = sys.argv
        center = geocode_postal_code(zip_code, cache)
        print(center if center else f"Couldn't geocode ZIP/postal code {zip_code!r}.")

    elif len(sys.argv) >= 2 and sys.argv[1] == "businesses":
        _, _, place_name, *category_keys = sys.argv
        areas = resolve_named_area(place_name, cache, status_cb=print)
        if not areas:
            print("No match found (or selection cancelled).")
        else:
            for area in areas:
                print(f"Searching {area.label} ...")
            businesses = search_businesses_multi(
                areas, category_keys or ALL_CATEGORY_KEYS, cache, status_cb=print
            )
            for business in businesses[:20]:
                print(
                    f"- {business.name} [{business.category_value}] "
                    f"phone={business.phone} website={business.website} "
                    f"fb={business.facebook}"
                )

    elif len(sys.argv) >= 2:
        place_name = " ".join(sys.argv[1:])
        areas = resolve_named_area(place_name, cache, status_cb=print)
        if not areas:
            print("No match found (or selection cancelled).")
        else:
            for area in areas:
                print(area)

    else:
        print("Usage:")
        print('  python osm_source.py "Grand Rapids"')
        print("  python osm_source.py radius 42.9634 -85.6681 5")
        print("  python osm_source.py zip 49503")
        print('  python osm_source.py businesses "Grand Rapids" shop craft office')

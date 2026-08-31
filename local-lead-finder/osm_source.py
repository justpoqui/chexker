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
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from cache import Cache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
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


def query_overpass(
    query: str,
    cache: Cache,
    status_cb: Callable[[str], None] = lambda msg: None,
) -> dict:
    """Run one Overpass QL query, using the cache when possible.

    Returns the parsed JSON body. Raises requests.RequestException if every
    retry in OVERPASS_BACKOFF_SCHEDULE is exhausted.
    """
    query_hash = _query_hash(query)
    cached = cache.get_overpass(query_hash)
    if cached is not None:
        status_cb("Loaded from cache (no Overpass request made).")
        return json.loads(cached)

    delays = iter(OVERPASS_BACKOFF_SCHEDULE)
    while True:
        status_cb("Querying Overpass...")
        response = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=120,
        )
        if response.status_code == 200:
            cache.set_overpass(query_hash, query, response.text)
            return response.json()

        if response.status_code in (429, 504):
            try:
                delay = next(delays)
            except StopIteration:
                response.raise_for_status()
            status_cb(
                f"Overpass returned {response.status_code}; "
                f"backing off {delay}s before retrying..."
            )
            time.sleep(delay)
            continue

        response.raise_for_status()


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
# STEP 2 — manual smoke test
# WHY: gui.py doesn't exist yet, so this lets the area-resolution
#      logic above be exercised from the terminal before STEP 3
#      builds anything on top of it.
# ============================================================

if __name__ == "__main__":
    cache = Cache()
    if len(sys.argv) >= 2 and sys.argv[1] == "radius":
        _, _, lat, lon, radius = sys.argv
        area = make_radius_area(float(lat), float(lon), float(radius))
        print(area)
    elif len(sys.argv) >= 2:
        place_name = " ".join(sys.argv[1:])
        area = resolve_named_area(place_name, cache, status_cb=print)
        print(area if area else "No match found (or selection cancelled).")
    else:
        print("Usage:")
        print('  python osm_source.py "Grand Rapids"')
        print("  python osm_source.py radius 42.9634 -85.6681 5")

# Local Lead Finder

A desktop GUI tool that finds real businesses in a given area with weak or
missing web presence — no website, a Facebook page standing in for a site, a
website that's stale or broken — so you can pitch them web/marketing work.

**Zero paid APIs. Zero API keys. Zero billing.** Every data point comes from
the free, key-less [Overpass API](https://overpass-api.de/) (a query engine
over OpenStreetMap data) plus, optionally, a direct HTTP fetch of a business's
own public website. Nothing is scraped from Facebook, Instagram, or Google
Maps — a Facebook URL is only ever recorded when OpenStreetMap already lists
one; judging whether that page is still active is left to you.

## Status

All seven build steps are done: area resolution, the Overpass business
query, lead scoring, website enrichment, the Tkinter GUI, and CSV/call-sheet
export are all working. Run `python main.py` to launch the real app.

## Requirements

- Python 3.11+
- The only third-party dependency: `requests` (see `requirements.txt`)

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Don't want to install Python at all? See [BUILDING.md](BUILDING.md) for a
standalone `LocalLeadFinder.exe` — either download one built by CI, or
build one yourself with PyInstaller.

## Module map

- **`main.py`** — The entry point. Its only job is to build the pieces
  (cache, GUI) and start the Tkinter event loop. There is intentionally no
  business logic here.

- **`gui.py`** — All Tkinter widget code: the search bar, the sortable
  results table, the detail panel for a selected business, the export
  buttons, the status bar, an About box (Help menu), and the
  background-thread-to-UI bridge (a `queue.Queue` polled with `after()` so
  network calls never freeze the window).

- **`osm_source.py`** — The only module allowed to talk to the Overpass API.
  Resolves a search area (either a named place OSM already knows, or a
  coordinate + radius), then runs the actual business query (shops, offices,
  crafts, etc. that have a `name` tag). Handles Overpass's politeness
  requirements: POST the query, one request at a time, a real User-Agent,
  and exponential backoff on rate-limit or timeout responses. Rotates
  through `OVERPASS_ENDPOINTS` (a plain tuple of mirror URLs at the top of
  the file — add one to grow the list) if the current mirror is down or
  stays throttled past its backoff schedule.

- **`enrich.py`** — For every business that lists a real website (as opposed
  to a Facebook/Instagram link masquerading as one), fetches that site once
  to check whether it loads, whether it redirects to a social platform, and
  what social links appear in its HTML. Respects `robots.txt` and runs in a
  small thread pool so it never hammers a single host.

- **`scoring.py`** — Pure logic, no network calls: takes a business's raw
  OSM tags (and, once available, `enrich.py`'s findings) and returns a lead
  tier and numeric score. A business with literally no web presence scores
  highest, since it's the easiest pitch.

- **`cache.py`** — A single SQLite database shared by the other modules.
  Overpass responses, per-domain enrichment results, and (if you enable it)
  geocoding lookups are all cached here so that re-running the same search
  hits disk instead of the network.

- **`export.py`** — Turns the final results table into a CSV file (with the
  OpenStreetMap attribution required by its license baked into the header)
  or a plain-text "call sheet" of the top leads, copied to the clipboard.

## Data sources & attribution

- Business data comes from **OpenStreetMap**, © OpenStreetMap contributors,
  licensed under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/).
  This attribution is shown in the app's About box and included in every
  CSV export.
- OSM coverage is uneven. Rural areas and small strip-mall businesses are
  often missing entirely, and a business with no `website` tag in OSM may
  still have a real website that nobody has recorded there yet. Treat every
  "no website" result as a hypothesis to verify by hand, not a fact.
- If the optional Nominatim geocoder is ever enabled, it is used strictly
  according to the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/):
  a descriptive User-Agent, at most one request per second, and permanent
  local caching of every result.
- This app never determines whether a Facebook page is active or abandoned.
  That judgment call is yours — made by clicking the link.

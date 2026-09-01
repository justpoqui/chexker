# ============================================================
# STEP 1 — SCAFFOLD: module purpose
# WHY: exporting is a leaf feature that only needs the final,
#      scored result list — kept separate so gui.py doesn't grow
#      file-I/O and clipboard-formatting logic.
# ============================================================

import csv
from datetime import date
from typing import Iterable

from osm_source import Business
from scoring import ScoreResult

Row = tuple[Business, ScoreResult]

# ============================================================
# STEP 7 — CSV EXPORT
# WHY: this is the deliverable the user actually takes away from
#      the app — a spreadsheet of leads to work through. The OSM
#      attribution line is required by ODbL wherever OSM-derived
#      data is redistributed (see README, STEP 8), so it goes in
#      as the file's first line, before the real CSV header.
# ============================================================

OSM_ATTRIBUTION = (
    "Data © OpenStreetMap contributors, licensed under the Open Database "
    "License (ODbL) -- https://www.openstreetmap.org/copyright"
)

CSV_FIELDNAMES = [
    "score", "tier", "name", "category", "phone", "website", "facebook",
    "address", "lat", "lon", "osm_id", "osm_last_edited", "flags", "date_checked",
]


def _row_dict(business: Business, result: ScoreResult, export_date: str) -> dict:
    return {
        "score": result.score,
        "tier": result.tier,
        "name": business.name,
        "category": f"{business.category_key}={business.category_value}",
        "phone": business.phone or "",
        "website": business.website or "",
        "facebook": business.facebook or "",
        "address": business.address,
        "lat": business.lat if business.lat is not None else "",
        "lon": business.lon if business.lon is not None else "",
        "osm_id": business.osm_key,
        "osm_last_edited": business.osm_timestamp[:10] if business.osm_timestamp else "",
        "flags": "; ".join(result.flags),
        "date_checked": export_date,
    }


def write_csv(path: str, rows: Iterable[Row]) -> int:
    """Write `rows` to a CSV file at `path`. Returns the number of rows written.

    `date_checked` records when this export was generated, not a per-row
    fetch timestamp — the app doesn't currently track a fetch time per
    business, only a 30-day cache TTL per website (see cache.py).
    """
    export_date = date.today().isoformat()
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# {OSM_ATTRIBUTION}\n")
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for business, result in rows:
            writer.writerow(_row_dict(business, result, export_date))
            count += 1
    return count


# ============================================================
# STEP 7 — "COPY AS CALL SHEET"
# WHY: a spreadsheet is for reviewing leads later; a call sheet is
#      for working the phone right now. Leads with no phone number
#      are dropped here even if their tier score is high, since
#      there's nothing to call.
# ============================================================

def build_call_sheet(rows: Iterable[Row], limit: int = 20) -> str:
    """Format the top `limit` callable leads as plain text for a phone call.

    Sorts by score internally (callers don't need to pre-sort), keeping
    only businesses with a phone number.
    """
    callable_rows = sorted(
        (pair for pair in rows if pair[0].phone),
        key=lambda pair: pair[1].score,
        reverse=True,
    )[:limit]

    if not callable_rows:
        return "No leads with a phone number found.\n"

    lines = []
    for business, result in callable_rows:
        reason = result.reasons[0] if result.reasons else result.tier
        lines.append(f"{business.name} -- {business.phone}")
        lines.append(f"    {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ============================================================
# STEP 7 — manual smoke test
# WHY: both functions above are pure (no Tkinter, no network), so
#      they can be exercised directly with hand-built data.
# ============================================================

if __name__ == "__main__":
    import tempfile

    def make_business(osm_id, name, **overrides) -> Business:
        defaults = dict(
            osm_type="node", osm_id=osm_id, name=name, category_key="shop",
            category_value="hairdresser", lat=42.9, lon=-85.6, phone=None,
            website=None, facebook=None, instagram=None, address="123 Main St",
            opening_hours=None, raw_tags={},
        )
        defaults.update(overrides)
        return Business(**defaults)

    rows: list[Row] = [
        (
            make_business(1, "No Presence Biz", phone="555-0001"),
            ScoreResult(tier="NO_PRESENCE", score=115, reasons=["No website and no social tag.", "+15: has a phone number."]),
        ),
        (
            make_business(2, "Weak Site Biz", website="https://weak.example.com", phone="555-0002"),
            ScoreResult(tier="WEAK_SITE", score=55, reasons=["Website did not load."], flags=["http_500"]),
        ),
        (
            make_business(3, "No Phone Biz"),
            ScoreResult(tier="NO_PRESENCE", score=100, reasons=["No website and no social tag."]),
        ),
    ]

    with tempfile.NamedTemporaryFile(mode="r", suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name
    written = write_csv(csv_path, rows)
    print(f"wrote {written} rows to {csv_path}")
    with open(csv_path) as f:
        content = f.read()
    print(content)
    assert content.startswith("# Data © OpenStreetMap contributors")
    assert "No Presence Biz" in content
    assert "http_500" in content

    call_sheet = build_call_sheet(rows, limit=20)
    print("--- call sheet ---")
    print(call_sheet)
    assert "No Presence Biz -- 555-0001" in call_sheet
    assert "No Phone Biz" not in call_sheet  # no phone -> excluded
    # higher score (115) must be listed before the lower one (55)
    assert call_sheet.index("No Presence Biz") < call_sheet.index("Weak Site Biz")

    empty_sheet = build_call_sheet([(make_business(9, "Phoneless"), ScoreResult(tier="NO_PRESENCE", score=100))])
    assert empty_sheet == "No leads with a phone number found.\n"

    print("ALL OK")

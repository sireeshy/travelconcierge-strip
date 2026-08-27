"""One-off seeding script: pre-populates place_details_cache with real outlets of the brands in
app.py's RECOGNIZED_CHAINS, along the two corridors the app already has curated data for (see
TOLL_PLAZAS in app.py) -- Bengaluru/Mysuru/Ooty and Hyderabad/Nagpur -- rather than a nationwide
crawl, to keep this bounded and cheap.

Standalone (no Streamlit/st.session_state dependency, unlike app.py) so it can run outside the
Streamlit runtime. Mirrors get_place_details_and_reviews's exact field mask and processing logic so
what lands in the cache is byte-for-byte what a live app request would have produced and cached
itself -- this is just doing that work upfront instead of waiting for a real user to trigger it.

Safe to re-run: upserts by place_id (updates the existing row if already cached, appends if new),
same as _persist_place_details_cache in app.py.
"""
import json
import re
import time
from datetime import datetime, timezone

import requests

with open(".env", encoding="utf-8") as f:
    env_content = f.read()


def _env(name: str) -> str:
    m = re.search(rf'^{re.escape(name)}=(.*)$', env_content, re.MULTILINE)
    if not m:
        raise SystemExit(f"{name} not found in .env")
    value = m.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value


API_KEY = _env("GOOGLE_MAPS_API_KEY")
SHEETS_CREDS = json.loads(_env("GOOGLE_SHEETS_CREDENTIALS_JSON"))
SHEET_ID = _env("USAGE_SHEET_ID")

RECOGNIZED_CHAINS = [
    "Kailash Parbat", "Saravana Bhavan", "Adyar Ananda Bhavan", "A2B",
    "Haldiram's", "Haldiram", "Sagar Ratna",
    "Chaayos", "Third Wave Coffee", "Cafe Coffee Day", "Café Coffee Day", "Starbucks",
    "Jio-bp", "Jio BP", "Shell",
    "Big Bay", "Cube Stop", "PATH Recharge", "Highway Star",
]
# Dedupe near-duplicate brand spellings for search purposes (searching "Jio-bp" and "Jio BP"
# separately would just waste calls on the same outlets).
SEARCH_BRANDS = [
    "Kailash Parbat", "Saravana Bhavan", "Adyar Ananda Bhavan", "A2B",
    "Haldiram's", "Sagar Ratna", "Chaayos", "Third Wave Coffee", "Cafe Coffee Day", "Starbucks",
    "Jio-bp", "Shell petrol pump", "Big Bay", "Cube Stop", "PATH Recharge", "Highway Star",
]

CITIES = ["Bengaluru", "Mysuru", "Ooty", "Hyderabad", "Nagpur", "Chennai", "Vijayawada"]

DETAILS_FIELD_MASK = (
    "id,displayName.text,rating,userRatingCount,formattedAddress,nationalPhoneNumber,websiteUri,"
    "currentOpeningHours,priceLevel,regularOpeningHours,reviews,servesBreakfast,servesLunch,"
    "servesDinner,servesVegetarianFood,parkingOptions,photos,restroom"
)


def search_outlets(brand: str, city: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json", "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName.text,places.formattedAddress",
    }
    body = {"textQuery": f"{brand} {city}", "pageSize": 5, "languageCode": "en-US"}
    try:
        r = requests.post("https://places.googleapis.com/v1/places:searchText", headers=headers, json=body, timeout=20)
    except requests.RequestException as exc:
        print(f"  [{brand} / {city}] search failed: {exc}")
        return []
    if not r.ok:
        print(f"  [{brand} / {city}] search {r.status_code}: {r.text[:200]}")
        return []
    return r.json().get("places", [])


def fetch_details(place_id: str) -> dict | None:
    headers = {"Content-Type": "application/json", "X-Goog-Api-Key": API_KEY, "X-Goog-FieldMask": DETAILS_FIELD_MASK}
    try:
        r = requests.get(f"https://places.googleapis.com/v1/places/{place_id}", headers=headers, timeout=20)
    except requests.RequestException as exc:
        print(f"  details failed for {place_id}: {exc}")
        return None
    if not r.ok:
        print(f"  details {r.status_code} for {place_id}: {r.text[:200]}")
        return None
    return r.json()


def process(details_data: dict) -> tuple[dict, str | None]:
    """Mirrors get_place_details_and_reviews._process in app.py exactly."""
    all_reviews = []
    for r_data in details_data.get("reviews", []):
        review_text = r_data.get("text", {}).get("text", "")
        all_reviews.append({
            "author_name": r_data.get("authorAttribution", {}).get("displayName", "Anonymous"),
            "rating": r_data.get("rating", 0),
            "text": review_text[:280],
            "relative_time": r_data.get("relativePublishTimeDescription"),
            "publish_time": r_data.get("publishTime", ""),
        })
    reviews = all_reviews[:3]
    most_recent_review = max(all_reviews, key=lambda r: r["publish_time"], default=None)
    worst = min(all_reviews, key=lambda r: r["rating"], default=None)
    critical_review = worst if worst and worst["rating"] <= 3 else None

    parking_options = details_data.get("parkingOptions", {})
    available_parking = [
        label for flag, label in [
            ("freeParkingLot", "free parking lot"), ("paidParkingLot", "paid parking lot"),
            ("freeStreetParking", "free street parking"), ("paidStreetParking", "paid street parking"),
            ("valetParking", "valet parking"), ("freeGarageParking", "free garage parking"),
            ("paidGarageParking", "paid garage parking"),
        ] if parking_options.get(flag)
    ]

    restroom_flag = details_data.get("restroom")
    if restroom_flag is True:
        restroom_text = "Confirmed by Google"
    elif restroom_flag is False:
        restroom_text = "Google indicates no restroom at this location"
    else:
        restroom_text = "Not confirmed by Google -- verify locally if this matters"

    model_dict = {
        "opening_hours": details_data.get("regularOpeningHours"),
        "reviews": reviews,
        "most_recent_review": (
            {"relative_time": most_recent_review["relative_time"], "rating": most_recent_review["rating"]}
            if most_recent_review else None
        ),
        "critical_review": (
            {
                "author_name": critical_review["author_name"], "rating": critical_review["rating"],
                "text": critical_review["text"], "relative_time": critical_review["relative_time"],
            } if critical_review else None
        ),
        "current_opening_status": (
            "Open now" if details_data.get("currentOpeningHours", {}).get("openNow")
            else "Closed now" if "currentOpeningHours" in details_data
            else None
        ),
        "price_level": details_data.get("priceLevel"),
        "serves_breakfast": details_data.get("servesBreakfast"),
        "serves_lunch": details_data.get("servesLunch"),
        "serves_dinner": details_data.get("servesDinner"),
        "serves_vegetarian_food": details_data.get("servesVegetarianFood"),
        "parking_available": available_parking if available_parking else "Not listed by Google -- mention this is unverified if parking matters for this trip",
        "restroom_available": restroom_text,
    }
    photos = details_data.get("photos", [])
    photo_name = photos[0]["name"] if photos else None
    return model_dict, photo_name


def main():
    import gspread
    gc = gspread.service_account_from_dict(SHEETS_CREDS)
    spreadsheet = gc.open_by_key(SHEET_ID)
    try:
        sheet = spreadsheet.worksheet("place_details_cache")
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title="place_details_cache", rows=2000, cols=5)
        sheet.append_row(["place_id", "name", "cached_at", "rating_at_cache", "details_json"])

    existing_rows = sheet.get_all_values()
    existing_place_ids = {row[0]: i + 2 for i, row in enumerate(existing_rows[1:])}  # place_id -> row number
    print(f"Cache already has {len(existing_place_ids)} entries.")

    # Step 1: discover outlets (cheap searches).
    found = {}  # place_id -> name
    for brand in SEARCH_BRANDS:
        for city in CITIES:
            places = search_outlets(brand, city)
            for p in places:
                found[p["id"]] = p.get("displayName", {}).get("text", brand)
            print(f"[{brand} / {city}] {len(places)} result(s)")
            time.sleep(0.1)

    print(f"\n{len(found)} unique candidate outlets found across all brand/city searches.")

    # Step 2: fetch full details + write into the cache, skipping ones already fresh (<30 days).
    now = datetime.now(timezone.utc)
    written, skipped, failed = 0, 0, 0
    for place_id, name in found.items():
        if place_id in existing_place_ids:
            row_num = existing_place_ids[place_id]
            cached_at_str = existing_rows[row_num - 1][2] if len(existing_rows[row_num - 1]) > 2 else ""
            try:
                age_days = (now - datetime.fromisoformat(cached_at_str)).days
                if age_days <= 30:
                    skipped += 1
                    continue
            except ValueError:
                pass

        details_data = fetch_details(place_id)
        if details_data is None:
            failed += 1
            continue
        model_dict, photo_name = process(details_data)
        row_values = [
            place_id, name, now.isoformat(timespec="seconds"),
            details_data.get("rating") if details_data.get("rating") is not None else "",
            json.dumps({"model": model_dict, "photo_name": photo_name}, ensure_ascii=False),
        ]
        if place_id in existing_place_ids:
            sheet.update(f"A{existing_place_ids[place_id]}:E{existing_place_ids[place_id]}", [row_values])
        else:
            sheet.append_row(row_values)
        written += 1
        print(f"  cached: {name}")
        time.sleep(0.1)

    print(f"\nDone. written={written} skipped_already_fresh={skipped} failed={failed}")


if __name__ == "__main__":
    main()

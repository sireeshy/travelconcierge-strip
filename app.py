import streamlit as st
import pydeck as pdk
import google.genai as genai
from google.genai import types
import requests
from datetime import datetime, timedelta, timezone
from dateutil import parser
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import base64
import csv
import functools
import json
import logging
import math
import os
import re
import time
import dotenv

dotenv.load_dotenv()

# Plain stdout logging -- Streamlit Community Cloud's "Manage app" log viewer shows this directly,
# no extra setup needed. Kept separate from the usage_log.csv below: this is for "what happened,
# skim server logs" visibility, the CSV is for "how long is this actually taking, per request".
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("journey_concierge")

# Both app variants (this one and the original layout) write usage and feedback into the same
# shared Sheets (see HANDOFF.md) so every row is tagged with which one it came from -- otherwise
# "which layout gets used more, and rated better" is unanswerable.
APP_VERSION = "strip"

# CSV lives next to app.py, not in the repo (gitignored) -- structured per-request timing data for
# local analysis. Note this resets on every Streamlit Community Cloud restart/redeploy since its
# filesystem is ephemeral; treat it as a local-dev tool, not a durable analytics store.
USAGE_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage_log.csv")


USAGE_LOG_HEADER = [
    "timestamp_utc", "version", "event", "origin", "destination", "preferences", "duration_s",
    "tool_calls", "tool_errors", "structured_ok", "response_type",
    "places_api_new", "places_api_legacy", "places_api_failed",
    "routes_api_new", "routes_api_legacy", "routes_api_failed",
]


@st.cache_resource(show_spinner=False)
def _get_usage_worksheet():
    """Opens the shared usage-log Google Sheet via a service account, if one is configured -- same
    pattern as _get_feedback_worksheet below. usage_log.csv resets on every Streamlit Community Cloud
    restart/redeploy (ephemeral filesystem), so it can't answer "when and whether people actually use
    this" on its own; this Sheet is the durable copy. Returns None (falls back to local-only logging)
    when GOOGLE_SHEETS_CREDENTIALS_JSON / USAGE_SHEET_ID aren't set, or the Sheet can't be reached."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON")
    sheet_id = os.environ.get("USAGE_SHEET_ID")
    if not creds_json or not sheet_id:
        return None
    try:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(creds_json))
        sheet = gc.open_by_key(sheet_id).sheet1
        if not sheet.row_values(1):
            sheet.append_row(USAGE_LOG_HEADER)
        return sheet
    except Exception:
        logger.exception("failed to connect to the usage Google Sheet")
        return None


def log_usage_event(event_type: str, origin: str, destination: str, preferences: str, duration_s: float,
                     tool_trace: list[dict], response_meta: dict, places_api_stats: dict | None = None,
                     routes_api_stats: dict | None = None):
    """Appends one row per user-facing request (initial plan or follow-up) to usage_log.csv, and
    mirrors a summary line to stdout. tool_trace is the list of {name, detail, duration_s, ok}
    dicts collected by @timed_tool during this request. response_meta is the {structured_ok,
    response_type} dict from response_to_markdown. places_api_stats / routes_api_stats are the
    {'new', 'legacy', 'failed'} counter dicts accumulated in st.session_state['_places_api_stats']
    / ['_routes_api_stats'] by the Places/Routes tool functions this request (empty/None if a tool
    didn't run, e.g. a follow-up that didn't need a fresh lookup).

    Beyond timing, this is the performance signal for things that can silently degrade without
    anyone noticing in a chat UI: tool_errors catches a Gemini tool call that ultimately failed;
    places_api_*/routes_api_* break that down further, one row per underlying Maps/Places API call
    (a single search_places_along_route call can cover several categories, each independently
    served by the new API, the legacy fallback, or neither) -- this is what actually answers "what's
    our failure rate" and "how often is a legacy fallback needed" as trackable numbers instead of
    something only visible by reading server logs one incident at a time. structured_ok catches the
    structured-output-plus-tools combo (a preview feature as of this writing, see the comment above
    CONCIERGE_RESPONSE_SCHEMA) silently reverting to unparsed text. All of this is invisible to a
    user who just sees *a* plan and has no earlier run to compare against.
    """
    tool_summary = "; ".join(f"{t['name']}{t['detail']}({t['duration_s']:.1f}s)" for t in tool_trace)
    tool_errors = sum(1 for t in tool_trace if not t.get("ok", True))
    places_api_stats = places_api_stats or {}
    routes_api_stats = routes_api_stats or {}
    logger.info(
        "usage event=%s origin=%r destination=%r duration_s=%.2f tool_calls=%d tool_errors=%d "
        "places_api_new=%d places_api_legacy=%d places_api_failed=%d "
        "routes_api_new=%d routes_api_legacy=%d routes_api_failed=%d structured_ok=%s response_type=%s [%s]",
        event_type, origin, destination, duration_s, len(tool_trace), tool_errors,
        places_api_stats.get('new', 0), places_api_stats.get('legacy', 0), places_api_stats.get('failed', 0),
        routes_api_stats.get('new', 0), routes_api_stats.get('legacy', 0), routes_api_stats.get('failed', 0),
        response_meta.get("structured_ok"), response_meta.get("response_type"), tool_summary,
    )
    row = [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        APP_VERSION,
        event_type,
        origin,
        destination,
        preferences.replace("\n", " ")[:200],
        f"{duration_s:.2f}",
        tool_summary,
        tool_errors,
        response_meta.get("structured_ok"),
        response_meta.get("response_type"),
        places_api_stats.get('new', 0),
        places_api_stats.get('legacy', 0),
        places_api_stats.get('failed', 0),
        routes_api_stats.get('new', 0),
        routes_api_stats.get('legacy', 0),
        routes_api_stats.get('failed', 0),
    ]
    try:
        file_exists = os.path.exists(USAGE_LOG_PATH)
        with open(USAGE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(USAGE_LOG_HEADER)
            writer.writerow(row)
    except Exception:
        logger.exception("failed to write usage_log.csv")

    sheet = _get_usage_worksheet()
    if sheet is not None:
        try:
            sheet.append_row(row)
        except Exception:
            logger.exception("failed to append usage event to the Google Sheet")


FEEDBACK_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feedback_log.csv")
FEEDBACK_LOG_HEADER = ["timestamp_utc", "version", "rating", "comment", "origin", "destination"]


@st.cache_resource(show_spinner=False)
def _get_feedback_worksheet():
    """Opens the shared feedback Google Sheet via a service account, if one is configured.
    st.cache_resource (not cache_data) because this holds a live API client connection, not
    serializable data -- built once per server process, not per user session.

    Returns None -- and log_feedback() below falls back to local-only logging -- when
    GOOGLE_SHEETS_CREDENTIALS_JSON / FEEDBACK_SHEET_ID aren't set, or the Sheet can't be reached
    (wrong ID, not shared with the service account, API not enabled, etc). Feedback should never be
    silently lost or block the submit button just because the Sheet integration isn't set up yet or
    has a transient problem -- see HANDOFF.md for the one-time setup steps."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON")
    sheet_id = os.environ.get("FEEDBACK_SHEET_ID")
    if not creds_json or not sheet_id:
        return None
    try:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(creds_json))
        sheet = gc.open_by_key(sheet_id).sheet1
        if not sheet.row_values(1):
            sheet.append_row(FEEDBACK_LOG_HEADER)
        return sheet
    except Exception:
        logger.exception("failed to connect to the feedback Google Sheet")
        return None


def log_feedback(rating: int | None, comment: str):
    """Records one feedback submission -- always to stdout + a local CSV (the same
    zero-infrastructure pattern as usage_log.csv), and additionally to a shared Google Sheet when
    that's configured. The Sheet write is additive, not load-bearing: local logging always happens
    first and independently, so a misconfigured or briefly-unreachable Sheet never loses feedback or
    breaks the submit action for the user."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    origin = st.session_state.get("origin", "")
    destination = st.session_state.get("destination", "")
    comment = (comment or "").strip()

    logger.info("feedback version=%s rating=%s comment=%r origin=%r destination=%r",
                APP_VERSION, rating, comment, origin, destination)
    try:
        file_exists = os.path.exists(FEEDBACK_LOG_PATH)
        with open(FEEDBACK_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(FEEDBACK_LOG_HEADER)
            writer.writerow([timestamp, APP_VERSION, rating, comment, origin, destination])
    except Exception:
        logger.exception("failed to write feedback_log.csv")

    sheet = _get_feedback_worksheet()
    if sheet is not None:
        try:
            sheet.append_row([timestamp, APP_VERSION, rating, comment, origin, destination])
        except Exception:
            logger.exception("failed to append feedback to the Google Sheet")


# Hand-rolled instead of using the `polyline` pip package or Google's Static Maps API (which would
# draw the map for us) -- Static Maps API needs a separate Cloud Console enablement most keys won't
# have by default (this app hit that wall repeatedly with other "one more API" surprises), and the
# decode algorithm itself is short, stable, and dependency-free. See render_route_map() below.
def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decodes a Google-encoded polyline string into a list of (lat, lng) points."""
    points = []
    index = lat = lng = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1f) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lng += delta
        points.append((lat / 1e5, lng / 1e5))
    return points


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in km -- accurate enough for placing a stop on a route visualization;
    not meant for turn-by-turn precision."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def route_cumulative_km(path: list[tuple[float, float]]) -> list[float]:
    """Cumulative distance (km) from the route's start to each point in a decoded polyline."""
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + _haversine_km(*path[i - 1], *path[i]))
    return cum


def distance_along_route_km(path: list[tuple[float, float]], cum: list[float], lat: float, lng: float) -> float:
    """How far along the route (km from the origin) the *nearest polyline point* to (lat, lng) is --
    this is what actually places a stop proportionally on The Strip (see DESIGN_CONCEPTS.md), from
    the stop's own real coordinates (already tracked in discovered_places from the Places API), not
    a guessed or model-estimated distance. A decoded Google polyline is normally dense enough
    (points every ~10-50m on a highway route) that nearest-vertex matching is accurate enough for a
    visualization; this deliberately doesn't do true point-to-segment projection onto the polyline,
    since that precision isn't needed here and isn't worth the added complexity."""
    best_i, best_d = 0, float("inf")
    for i, (plat, plng) in enumerate(path):
        d = _haversine_km(lat, lng, plat, plng)
        if d < best_d:
            best_d, best_i = d, i
    return cum[best_i]


_timezone_finder = TimezoneFinder()

# --- Helper Function for Google Maps Autocomplete ---

def get_place_predictions(query_text: str, api_key: str) -> list[str]:
    """Fetches autocomplete predictions from the Places API (New) for guaranteed place selections."""
    if not query_text or not api_key:
        return [query_text] if query_text else []

    url = "https://places.googleapis.com/v1/places:autocomplete"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
    }
    data = {"input": query_text, "languageCode": "en"}
    try:
        response = requests.post(url, headers=headers, json=data, timeout=5)
        if response.ok:
            suggestions = response.json().get('suggestions', [])
            predictions = [
                s['placePrediction']['text']['text']
                for s in suggestions
                if 'placePrediction' in s
            ]
            if predictions:
                return predictions
    except Exception:
        pass
    return [query_text]


@st.cache_data(ttl=3600, show_spinner=False)
def get_timezone_for_location(location: str, api_key: str) -> str:
    """Resolves a place name to its IANA timezone (e.g. 'Asia/Kolkata') by geocoding it and
    then looking up the timezone offline for those coordinates -- no separate Time Zone API
    call or extra enablement needed. Falls back to Asia/Kolkata (this app's home turf) if the
    location can't be geocoded."""
    if location and api_key:
        try:
            response = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": location, "key": api_key},
                timeout=5,
            )
            results = response.json().get('results', [])
            if results:
                loc = results[0]['geometry']['location']
                tz_name = _timezone_finder.timezone_at(lat=loc['lat'], lng=loc['lng'])
                if tz_name:
                    return tz_name
        except Exception:
            pass
    return "Asia/Kolkata"


@st.cache_data(ttl=86400, show_spinner=False)
def get_wikipedia_thumbnail(place_name: str) -> dict | None:
    """Looks up a Wikipedia summary (title, thumbnail image, page link) for a place name via
    Wikipedia's public REST API -- no key needed. This is what backs the "region imagery" side
    panel: real, freely-licensed photos (Wikipedia article images live on Wikimedia Commons, all
    under a free license) tied to whatever origin/destination the user actually searched for, not a
    fixed demo set baked in for one route.

    A descriptive User-Agent is required -- Wikimedia's CDN silently serves an HTML error page
    instead of the image/JSON for requests that don't identify themselves
    (https://meta.wikimedia.org/wiki/User-Agent_policy), which is exactly what happened testing this
    with a plain curl request before adding one here.

    The confirmed place string from Places Autocomplete looks like "Anantapur, Andhra Pradesh,
    India" -- the first comma-separated segment is a reasonable Wikipedia article title guess for
    an Indian town/city, though it isn't guaranteed for every place name."""
    title = place_name.split(",")[0].strip()
    if not title:
        return None
    try:
        response = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title)}",
            headers={"User-Agent": "JourneyConcierge/1.0 (travel-planning side project; contact via GitHub sireeshy/travelconcierge)"},
            timeout=5,
        )
        if not response.ok:
            return None
        data = response.json()
        thumbnail_url = data.get("thumbnail", {}).get("source")
        if not thumbnail_url:
            return None
        return {
            "title": data.get("title", title),
            "thumbnail_url": thumbnail_url,
            "page_url": data.get("content_urls", {}).get("desktop", {}).get("page"),
        }
    except Exception:
        return None

# --- Gemini Tool Definitions ---

_TOOL_LABELS = {
    "calculate_route_and_etas": "🚗 Calculating route and ETAs",
    "search_places_along_route": "🔍 Searching along the route",
    "get_place_details_and_reviews": "📋 Checking reviews, hours, and parking",
}


def _tool_call_detail(name: str, kwargs: dict) -> str:
    """Short human-readable suffix for a tool call, used both in the live progress status and the
    usage log -- e.g. what category was searched, or how many places were looked up."""
    if name == "search_places_along_route":
        cats = kwargs.get('categories') or []
        return " for " + ", ".join(f'"{c}"' for c in cats)
    if name == "get_place_details_and_reviews":
        n = len(kwargs.get('place_ids') or [])
        return f" ({n} candidate place{'s' if n != 1 else ''})"
    if name == "calculate_route_and_etas":
        return f" ({kwargs.get('origin', '')} → {kwargs.get('destination', '')})"
    return ""


def timed_tool(func):
    """Wraps a Gemini tool function to (1) post a line to the live st.status progress panel as the
    call starts, and (2) record its name/detail/duration/outcome into st.session_state['_tool_trace']
    for the usage log. Automatic function calling drives these tool functions synchronously inside
    one blocking chat.send_message() call, so a live progress panel is the only way to show the user
    what's happening without a bigger architecture change (see handoff notes on concurrent tool
    execution) -- this decorator is how each tool reports in.

    functools.wraps preserves __name__/__doc__/__wrapped__ so inspect.signature(wrapper) still
    resolves to the original function's signature -- required for the genai SDK to build the tool's
    schema correctly from the wrapped function.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        detail = _tool_call_detail(func.__name__, kwargs)
        status = st.session_state.get("_progress_status")
        if status is not None:
            status.write(f"{_TOOL_LABELS.get(func.__name__, func.__name__)}{detail}...")

        start = time.monotonic()
        try:
            result = func(*args, **kwargs)
            ok = not (isinstance(result, dict) and "error" in result)
            return result
        except Exception:
            ok = False
            raise
        finally:
            duration_s = time.monotonic() - start
            trace = st.session_state.get("_tool_trace")
            if trace is not None:
                trace.append({"name": func.__name__, "detail": detail, "duration_s": duration_s, "ok": ok})
            logger.info("tool=%s%s duration_s=%.2f ok=%s", func.__name__, detail, duration_s, ok)
    return wrapper


def _api_request(method: str, url: str, *, headers: dict | None = None, json_body: dict | None = None,
                  params: dict | None = None, timeout: float = 12.0, max_attempts: int = 3) -> requests.Response:
    """Calls a Maps/Places API endpoint with a timeout (none of these calls had one before, so a
    single slow/stuck request could hang the whole plan indefinitely) and short retries on transient
    failures -- a 5xx, a timeout, or a dropped connection. search_places_along_route in particular
    has been observed to fail occasionally on a well-formed, valid request where an identical retry
    immediately succeeds; previously the only retry was the user re-asking by hand.

    'params' is for the legacy Places API's query-string auth/filter style (e.g. `key=...`); the
    Places/Routes API (New) calls use header auth and a JSON body instead, via 'json_body'.

    Does not retry 4xx responses -- those mean the request itself is wrong, and retrying won't help.
    Raises requests.RequestException if every attempt fails on a network-level error (not a real
    HTTP response); callers turn that into the same {"error": ...} dict shape used for a bad status.
    """
    response = None
    for attempt in range(max_attempts):
        try:
            response = requests.request(method, url, headers=headers or {}, json=json_body, params=params, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError):
            if attempt == max_attempts - 1:
                raise
            time.sleep(0.75 * (attempt + 1))
            continue
        if response.status_code < 500:
            return response
        if attempt < max_attempts - 1:
            time.sleep(0.75 * (attempt + 1))
    return response


@timed_tool
def calculate_route_and_etas(origin: str, destination: str, departure_time_iso: str) -> dict:
    """
    Calculates the route between an origin and destination, providing total duration, distance,
    estimated toll cost, and estimated arrival times (ETAs) for major milestones, considering traffic.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    # Routes API (New) hard-rejects a departureTime that isn't strictly in the future -- verified
    # directly against the live API: "400 INVALID_ARGUMENT: Timestamp must be set to a future time."
    # This is a real, observed failure mode, not theoretical: departure_time_iso is computed once
    # when planning starts, but a single AFC round trip has been measured at 30-80+ seconds of
    # Gemini's own latency (see ARCHITECTURE.md) -- on a slow or retried plan, the instant this tool
    # actually runs can land after a timestamp that was still valid when the request was built.
    # Bumping forward to "now + a small buffer" whenever the given time has already passed turns
    # that from a guaranteed 400 into a request that still succeeds, just a minute or two later than
    # what was originally asked for.
    try:
        parsed_departure = datetime.fromisoformat(departure_time_iso.replace('Z', '+00:00'))
    except ValueError:
        parsed_departure = None
    now = datetime.now(timezone.utc)
    if parsed_departure is None or parsed_departure <= now:
        departure_time_iso = (now + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')

    def _route_new():
        url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.legs.duration,routes.legs.distanceMeters,routes.legs.polyline.encodedPolyline,routes.polyline.encodedPolyline,routes.travelAdvisory.tollInfo"
        }
        data = {
            "origin": {"address": origin},
            "destination": {"address": destination},
            "departureTime": departure_time_iso,
            "travelMode": "DRIVE",
            "routingPreference": "TRAFFIC_AWARE",
            "computeAlternativeRoutes": False,
            "languageCode": "en-US",
            "units": "METRIC",
            # Toll estimates are opt-in: extraComputations must list "TOLLS", and the API requires
            # routeModifiers.vehicleInfo to be present (any value) before it will compute a price.
            "extraComputations": ["TOLLS"],
            "routeModifiers": {"vehicleInfo": {"emissionType": "GASOLINE"}}
        }
        try:
            response = _api_request("POST", url, headers=headers, json_body=data)
        except requests.RequestException as exc:
            return None, f"Routes API (New) request failed: {exc}"
        if not response.ok:
            return None, f"Routes API (New) error {response.status_code}: {response.text}"
        return response.json(), None

    def _route_legacy():
        # Fallback for when Routes API (New) is down/failing. The legacy Directions API has no
        # structured toll-price field (that's a Routes API v2-only feature), so a route served by
        # this tier comes back with estimated_toll_cost=None -- a real, acceptable degradation,
        # same as legacy Places results coming back missing a couple of fields New provides.
        # Normalizes the response into the same {'routes': [...]} shape _route_new returns so
        # every line below this point stays identical regardless of which tier served it.
        try:
            dep_ts = int(datetime.fromisoformat(departure_time_iso).timestamp())
        except ValueError:
            dep_ts = "now"
        params = {"origin": origin, "destination": destination, "departure_time": dep_ts,
                  "mode": "driving", "key": api_key}
        try:
            response = _api_request("GET", "https://maps.googleapis.com/maps/api/directions/json", params=params)
        except requests.RequestException as exc:
            return None, f"Directions API (legacy) request failed: {exc}"
        if not response.ok:
            return None, f"Directions API (legacy) error {response.status_code}: {response.text}"
        payload = response.json()
        if payload.get('status') != 'OK' or not payload.get('routes'):
            return None, f"Directions API (legacy) status {payload.get('status')}"
        leg = payload['routes'][0]['legs'][0]
        duration_s = leg.get('duration_in_traffic', leg.get('duration', {})).get('value', 0)
        distance_m = leg.get('distance', {}).get('value', 0)
        polyline = payload['routes'][0].get('overview_polyline', {}).get('points', '')
        return {"routes": [{
            "duration": f"{duration_s}s",
            "distanceMeters": distance_m,
            "legs": [{"duration": f"{duration_s}s", "distanceMeters": distance_m,
                      "polyline": {"encodedPolyline": polyline}}],
            "polyline": {"encodedPolyline": polyline},
            "travelAdvisory": {},
        }]}, None

    routes_data, error = _route_new()
    source = "new"
    if error:
        print(f"[calculate_route_and_etas] falling back to legacy Directions API after: {error}", flush=True)
        routes_data, legacy_error = _route_legacy()
        source = "legacy"
        if legacy_error:
            stats = st.session_state.setdefault('_routes_api_stats', {'new': 0, 'legacy': 0, 'failed': 0})
            stats['failed'] = stats.get('failed', 0) + 1
            return {"error": f"Both Routes APIs failed -- new: {error}; legacy: {legacy_error}"}
    stats = st.session_state.setdefault('_routes_api_stats', {'new': 0, 'legacy': 0, 'failed': 0})
    stats[source] = stats.get(source, 0) + 1

    if not routes_data.get('routes'):
        return {"total_duration_seconds": 0, "total_distance_meters": 0, "legs": [], "encoded_overall_polyline": ""}

    route = routes_data['routes'][0]
    legs = []
    for leg_data in route['legs']:
        legs.append({
            "duration_seconds": int(leg_data['duration'].replace('s', '')),
            "distance_meters": leg_data['distanceMeters'],
            # There is no legs.startAddress/endAddress field in Routes API v2 (that's a legacy
            # Directions API field name that silently 400s here) -- since this app never passes
            # waypoints, computeRoutes always returns exactly one leg spanning origin->destination,
            # so the function's own params are a safe stand-in for the address strings.
            "end_address": destination,
            "start_address": origin,
            "encoded_polyline": leg_data['polyline']['encodedPolyline']
        })

    toll_prices = route.get('travelAdvisory', {}).get('tollInfo', {}).get('estimatedPrice', [])
    estimated_toll = None
    if toll_prices:
        price = toll_prices[0]
        estimated_toll = f"{price.get('units', '0')}.{price.get('nanos', 0) // 10_000_000:02d} {price.get('currencyCode', '')}".strip()

    # Not sent to the model -- stashed for the UI to draw the route on a map.
    st.session_state.route_polyline = route['polyline']['encodedPolyline']

    return {
        "total_duration_seconds": int(route['duration'].replace('s', '')),
        "total_distance_meters": route['distanceMeters'],
        "legs": legs,
        "encoded_overall_polyline": route['polyline']['encodedPolyline'],
        "estimated_toll_cost": estimated_toll
    }


@timed_tool
def search_places_along_route(encoded_polyline: str, categories: list[str]) -> dict:
    """
    Searches for places along an encoded polyline route matching one or more free-text queries.

    'categories' has no default value on purpose: it used to be a single "category" string that
    defaulted to "restaurant", which meant the model would silently fall back to restaurant
    searches for any request it didn't reason carefully about (e.g. "pick up snacks and drinks"
    still returned restaurants). Making it required, plus the system prompt's explicit "don't
    default to restaurants" instruction, forces the model to actually decide what kind of place
    fits the request.

    Each entry in 'categories' is a natural-language search query for whatever the user actually
    needs along the route -- not limited to food, e.g. "vegetarian restaurant", "clean public
    restroom", "grocery store", "liquor store", "convenience store selling snacks and drinks",
    "petrol pump", "pharmacy", "ATM".

    Pass every distinct kind of stop the trip needs in ONE call instead of calling this once per
    category -- each call is a full round trip through the model's own reasoning (observed at
    30-80+ seconds apiece, versus ~1s for the underlying Places lookup itself), so one call
    covering N categories is dramatically faster than N separate calls, the same reasoning
    get_place_details_and_reviews below already applies to batching place_ids.

    Returns {"results_by_category": {category: {"places": [...]} | {"error": "..."}}} -- one entry
    per requested category, each independently either a places list or an error, so one bad/empty
    category never blocks the results for the others.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    # There is no dedicated "search along route" endpoint in the Places API (New) -- that's a
    # parameter (searchAlongRouteParameters) on ordinary Text Search, not its own URL. An earlier
    # version of this app called a nonexistent places:searchAlongRoute endpoint, which 404'd every
    # time and drove the model to hallucinate a placeholder place_id to keep going.
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName.text,places.rating,places.userRatingCount,places.location,places.types,places.formattedAddress"
    }

    def _search_new(category: str):
        data = {
            "textQuery": category,
            "searchAlongRouteParameters": {"polyline": {"encodedPolyline": encoded_polyline}},
            "pageSize": 20,
            "languageCode": "en-US",
            "minRating": 3.5,
        }
        try:
            response = _api_request("POST", url, headers=headers, json_body=data)
        except requests.RequestException as exc:
            print(f"[search_places_along_route] category={category!r} polyline_len={len(encoded_polyline)} request failed: {exc}", flush=True)
            return None, f"Places API (New) request failed: {exc}"
        if not response.ok:
            # _api_request already retried transient 5xx a couple of times -- this is logged (error
            # path only, not per-call) so a recurrence past those retries still shows in server logs.
            print(f"[search_places_along_route] category={category!r} polyline_len={len(encoded_polyline)} HTTP {response.status_code}: {response.text}", flush=True)
            return None, f"Places API (New) error {response.status_code}: {response.text}"
        return response.json().get('places', []), None

    def _search_legacy(category: str):
        # Fallback for when Places API (New) is down/failing -- the legacy Places API has no
        # "search along route" parameter at all, so this approximates it by running a Nearby Search
        # around a handful of points sampled along the route polyline and merging/deduping the
        # results, instead of one precise along-route query. Normalizes into the exact same shape
        # _search_new returns (a New-API-style 'places' list) so every line below this point stays
        # identical regardless of which tier actually served the data.
        path = decode_polyline(encoded_polyline)
        if not path:
            return None, "no route polyline available for the legacy fallback"
        sample_n = min(4, len(path))
        step = (len(path) - 1) / (sample_n - 1) if sample_n > 1 else 0
        points = [path[round(i * step)] for i in range(sample_n)]

        seen = {}
        for lat, lng in points:
            params = {"location": f"{lat},{lng}", "radius": 15000, "keyword": category, "key": api_key}
            try:
                response = _api_request("GET", "https://maps.googleapis.com/maps/api/place/nearbysearch/json", params=params)
            except requests.RequestException:
                continue
            if not response.ok:
                continue
            for r in response.json().get('results', []):
                place_id = r.get('place_id')
                if not place_id or place_id in seen or (r.get('rating') or 0) < 3.5:
                    continue
                loc = r.get('geometry', {}).get('location', {})
                seen[place_id] = {
                    "id": place_id,
                    "displayName": {"text": r.get('name', '')},
                    "rating": r.get('rating'),
                    "userRatingCount": r.get('user_ratings_total'),
                    "formattedAddress": r.get('vicinity', ''),
                    "types": r.get('types', []),
                    "location": {"latitude": loc.get('lat'), "longitude": loc.get('lng')},
                }
        if not seen:
            return None, "legacy Places API returned no results either"
        return list(seen.values()), None

    def _fetch(category: str):
        # Runs in a worker thread -- network I/O only, no st.session_state access (see the note in
        # get_place_details_and_reviews below for why that matters).
        places, error = _search_new(category)
        source = "new"
        if error:
            print(f"[search_places_along_route] category={category!r} falling back to legacy Places API after: {error}", flush=True)
            places, legacy_error = _search_legacy(category)
            source = "legacy"
            if legacy_error:
                return category, None, f"Both Places APIs failed -- new: {error}; legacy: {legacy_error}", "failed"
        return category, places, None, source

    with ThreadPoolExecutor(max_workers=min(8, len(categories)) or 1) as executor:
        fetched = list(executor.map(_fetch, categories))

    if 'discovered_places' not in st.session_state:
        st.session_state.discovered_places = {}

    # Tracks which tier (new/legacy/failed) actually served each category's results this request --
    # read by log_usage_event so "how often is the fallback needed, how often do both fail" is a
    # real, trackable number instead of something only visible by reading server logs (see HANDOFF.md).
    api_stats = st.session_state.setdefault('_places_api_stats', {'new': 0, 'legacy': 0, 'failed': 0})

    results_by_category = {}
    for category, places_data, error, source in fetched:
        api_stats[source] = api_stats.get(source, 0) + 1
        if error:
            results_by_category[category] = {"error": error}
            continue

        places = []
        for p_data in places_data[:5]:
            places.append({
                "place_id": p_data['id'],
                "name": p_data['displayName']['text'],
                "rating": p_data.get('rating'),
                "user_ratings_total": p_data.get('userRatingCount'),
                "vicinity": p_data.get('formattedAddress', ''),
                "types": p_data.get('types', [])[:4]
            })

            # Track every discovered place so the UI can offer a "Navigate" link and a map marker
            # for it later -- the chat response is free-form text, so this is the only reliable
            # source of real place_ids and coordinates. Lat/lng isn't sent to the model, just
            # stashed for the UI.
            location = p_data.get('location', {})
            st.session_state.discovered_places[p_data['id']] = {
                "name": p_data['displayName']['text'],
                "vicinity": p_data.get('formattedAddress', ''),
                "lat": location.get('latitude'),
                "lng": location.get('longitude'),
            }

        results_by_category[category] = {"places": places}

    return {"results_by_category": results_by_category}


@timed_tool
def get_place_details_and_reviews(place_ids: list[str]) -> dict:
    """
    Fetches detailed information, operating hours, and top user reviews for MULTIPLE places at once.
    Pass every candidate place_id from search_places_along_route in a single call (do not call this
    once per place) so all pitstop options are evaluated together.

    This is intentionally batched (one call per plan, not one per place) because the automatic
    function-calling loop that drives this app has a capped number of round trips
    (see maximum_remote_calls below) -- evaluating 5 candidate places used to cost 5 of that budget
    on its own, which was the dominant reason plans ran out of calls before producing a final answer.

    The result for each place includes 'most_recent_review' (how long ago the newest review was
    posted, to gauge whether the rating still reflects the place today) and 'critical_review' (the
    most unfavorable review Google returned, only present if it's actually <=3 stars -- null if
    every review Google returned was positive) alongside the usual top reviews.
    """
    api_key = st.session_state.get("google_maps_api_key")
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # currentOpeningStatus is NOT a real field on the Place resource (it 400s) -- the actual
        # field for "is it open right now" is currentOpeningHours.openNow, mapped below.
        "X-Goog-FieldMask": "id,displayName.text,rating,userRatingCount,formattedAddress,nationalPhoneNumber,websiteUri,currentOpeningHours,priceLevel,regularOpeningHours,reviews,servesBreakfast,servesLunch,servesDinner,servesVegetarianFood,parkingOptions,photos"
    }

    def _details_new(place_id: str):
        url = f"https://places.googleapis.com/v1/places/{place_id}"
        try:
            response = _api_request("GET", url, headers=headers)
        except requests.RequestException as exc:
            return None, f"Places API (New) request failed: {exc}"
        if not response.ok:
            return None, f"Places API (New) error {response.status_code}: {response.text}"
        return response.json(), None

    def _details_legacy(place_id: str):
        # Fallback for when Places API (New) is down/failing. Normalizes the legacy response into
        # the exact same shape _details_new returns (New-API field names) so the review/parking/
        # price-level processing below stays identical regardless of which tier served the data --
        # a few fields legacy simply doesn't have (servesVegetarianFood, parkingOptions, photos)
        # come back empty/null rather than guessed, same as a real "not listed by Google" case today.
        params = {
            "place_id": place_id,
            "fields": "name,rating,user_ratings_total,formatted_address,opening_hours,price_level,reviews",
            "key": api_key,
        }
        try:
            response = _api_request("GET", "https://maps.googleapis.com/maps/api/place/details/json", params=params)
        except requests.RequestException as exc:
            return None, f"Places API (legacy) request failed: {exc}"
        if not response.ok:
            return None, f"Places API (legacy) error {response.status_code}: {response.text}"
        payload = response.json()
        if payload.get('status') != 'OK':
            return None, f"Places API (legacy) status {payload.get('status')}"
        result = payload.get('result', {})
        reviews = []
        for r in result.get('reviews', []):
            ts = r.get('time')
            reviews.append({
                "authorAttribution": {"displayName": r.get('author_name', 'Anonymous')},
                "rating": r.get('rating', 0),
                "text": {"text": r.get('text', '')},
                "relativePublishTimeDescription": r.get('relative_time_description'),
                # legacy gives a unix timestamp, not RFC3339 -- convert so the recency sort below
                # (which compares publishTime as a string) still works the same way either tier.
                "publishTime": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
            })
        opening_hours = result.get('opening_hours')
        return {
            "displayName": {"text": result.get('name', '')},
            "regularOpeningHours": opening_hours,
            "currentOpeningHours": {"openNow": opening_hours.get('open_now')} if opening_hours else {},
            "priceLevel": result.get('price_level'),
            "servesBreakfast": None, "servesLunch": None, "servesDinner": None, "servesVegetarianFood": None,
            "parkingOptions": {},
            "reviews": reviews,
            "photos": [],
        }, None

    def _fetch(place_id: str):
        # Runs in a worker thread -- network I/O only, no st.session_state access. Streamlit's
        # session_state is tied to the calling thread's ScriptRunContext and isn't safe to touch
        # from a worker thread; every session_state write below happens after results are gathered
        # back on the main thread.
        details_data, error = _details_new(place_id)
        source = "new"
        if error:
            print(f"[get_place_details_and_reviews] place_id={place_id!r} falling back to legacy Places API after: {error}", flush=True)
            details_data, legacy_error = _details_legacy(place_id)
            source = "legacy"
            if legacy_error:
                return place_id, None, f"Both Places APIs failed -- new: {error}; legacy: {legacy_error}", "failed"
        return place_id, details_data, None, source

    # Fetched in parallel -- these are independent GET requests, one per candidate place, previously
    # issued one at a time so total latency scaled with the number of places being evaluated instead
    # of the slowest single one.
    with ThreadPoolExecutor(max_workers=min(8, len(place_ids)) or 1) as executor:
        fetched = list(executor.map(_fetch, place_ids))

    # See the matching comment in search_places_along_route -- same tracked-tier pattern, same key,
    # so one request's stats cover both tools' underlying Places calls together.
    api_stats = st.session_state.setdefault('_places_api_stats', {'new': 0, 'legacy': 0, 'failed': 0})

    results = []
    for place_id, details_data, error, source in fetched:
        api_stats[source] = api_stats.get(source, 0) + 1
        if error:
            results.append({"place_id": place_id, "error": error})
            continue

        # Process every review Google actually returns (New Places API caps this at 5) before
        # slicing anything down -- picking the "top 3" first, as this used to, meant recency and
        # any negative review nearly always got cut before they were even looked at, since Google's
        # default ordering favors highly-rated/relevant reviews.
        all_reviews = []
        for r_data in details_data.get('reviews', []):
            review_text = r_data.get('text', {}).get('text', '')
            all_reviews.append({
                "author_name": r_data.get('authorAttribution', {}).get('displayName', 'Anonymous'),
                "rating": r_data.get('rating', 0),
                "text": review_text[:280],
                "relative_time": r_data.get('relativePublishTimeDescription'),
                "publish_time": r_data.get('publishTime', ''),  # RFC3339 UTC -- sorts correctly as a plain string
            })

        reviews = all_reviews[:3]
        # publishTime sorts correctly as a string since it's RFC3339 UTC (e.g. "2026-08-02T...Z").
        most_recent_review = max(all_reviews, key=lambda r: r['publish_time'], default=None)
        # "Critical" means an actually unfavorable review (<=3 stars), not just the least-glowing of
        # five great ones -- if nothing in Google's returned set clears that bar, say so rather than
        # mislabel a 4-star review as the downside.
        worst = min(all_reviews, key=lambda r: r['rating'], default=None)
        critical_review = worst if worst and worst['rating'] <= 3 else None

        parking_options = details_data.get('parkingOptions', {})
        available_parking = [
            label for flag, label in [
                ('freeParkingLot', 'free parking lot'),
                ('paidParkingLot', 'paid parking lot'),
                ('freeStreetParking', 'free street parking'),
                ('paidStreetParking', 'paid street parking'),
                ('valetParking', 'valet parking'),
                ('freeGarageParking', 'free garage parking'),
                ('paidGarageParking', 'paid garage parking'),
            ] if parking_options.get(flag)
        ]

        results.append({
            "place_id": place_id,
            "opening_hours": details_data.get('regularOpeningHours'),
            "reviews": reviews,
            "most_recent_review": (
                {"relative_time": most_recent_review['relative_time'], "rating": most_recent_review['rating']}
                if most_recent_review else None
            ),
            "critical_review": (
                {
                    "author_name": critical_review['author_name'],
                    "rating": critical_review['rating'],
                    "text": critical_review['text'],
                    "relative_time": critical_review['relative_time'],
                }
                if critical_review else None
            ),
            "current_opening_status": (
                "Open now" if details_data.get('currentOpeningHours', {}).get('openNow')
                else "Closed now" if 'currentOpeningHours' in details_data
                else None
            ),
            "price_level": details_data.get('priceLevel'),
            "serves_breakfast": details_data.get('servesBreakfast'),
            "serves_lunch": details_data.get('servesLunch'),
            "serves_dinner": details_data.get('servesDinner'),
            "serves_vegetarian_food": details_data.get('servesVegetarianFood'),
            "parking_available": available_parking if available_parking else "Not listed by Google -- mention this is unverified if parking matters for this trip"
        })

        # Not sent to the model (not useful context, just tokens) -- stashed for the UI to render a photo.
        photos = details_data.get('photos', [])
        if place_id in st.session_state.get('discovered_places', {}) and photos:
            st.session_state.discovered_places[place_id]['photo_name'] = photos[0]['name']

    return {"details": results}


def render_copy_and_share(text: str):
    """Renders a Copy button and a Share-on-WhatsApp button for a block of text.

    WhatsApp's pre-filled share links (wa.me / api.whatsapp.com) can silently fail or get
    truncated for long text, so the share button also copies the full text to the clipboard
    as a guaranteed fallback the user can paste in if the pre-fill doesn't come through.
    """
    b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
    # st.markdown(unsafe_allow_html=True) sanitizes out inline event handlers (onclick etc.),
    # so this needs an actual iframe via st.iframe instead, which allows real JS.
    st.iframe(
        f"""
        <div style="display:flex; gap:8px; font-family:sans-serif;">
          <button onclick="
            (function(btn){{
              const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
              const decoded = new TextDecoder('utf-8').decode(bytes);
              navigator.clipboard.writeText(decoded).then(() => {{
                const orig = btn.innerText;
                btn.innerText = '✅ Copied!';
                setTimeout(() => {{ btn.innerText = orig; }}, 1500);
              }});
            }})(this)
          " style="padding:6px 14px; border-radius:6px; border:1px solid #999; background:#f0f2f6; color:#31333F; cursor:pointer; font-size:14px;">📋 Copy</button>
          <button onclick="
            (function(btn){{
              const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
              const decoded = new TextDecoder('utf-8').decode(bytes);
              navigator.clipboard.writeText(decoded);
              window.open('https://api.whatsapp.com/send?text=' + encodeURIComponent(decoded), '_blank');
            }})(this)
          " style="padding:6px 14px; border-radius:6px; border:1px solid #25D366; background:#25D366; color:white; cursor:pointer; font-size:14px;">📤 Share on WhatsApp</button>
        </div>
        """,
        height=45,
    )


def render_navigate_links():
    """Lets the user pick their stops, then renders one Google Maps link with the whole route:
    current location -> stop(s) -> trip destination, using Maps' waypoints so every selected stop is
    set automatically in one tap instead of navigating to each place separately.

    Picking now means choosing one option per category from the plan actually presented (the same
    categories/options rendered as cards above), not a flat dropdown of every place the app happened
    to look up -- so the choice you make here is the same choice you just read about, not a second,
    disconnected selection step. Falls back to the old flat multiselect of every discovered place
    only if there's no structured plan to read categories from (e.g. structured output didn't parse
    this turn -- see the preview-feature note above CONCIERGE_RESPONSE_SCHEMA)."""
    plan = st.session_state.get('latest_plan')
    places = st.session_state.get('discovered_places', {})
    if not places:
        return

    st.caption("🗺️ Build your route:")

    # selected holds (display_name, place_id_or_None) pairs -- place_id is how a choice gets
    # resolved to a real address below. Matching by name alone doesn't work reliably: the model's
    # structured option.name (e.g. "Public Toilet (Court Road, Gulzarpet)") is often a more
    # descriptive rewrite of the raw Places displayName stored in discovered_places (e.g. plain
    # "TOILET"), so an exact-string lookup silently drops real selections -- observed directly
    # while testing this, not a theoretical concern.
    selected = []
    if plan and plan.get('stop_categories'):
        for category in plan['stop_categories']:
            options = category.get('options') or []
            if not options:
                continue
            option_names = [opt.get('name', 'Unknown') for opt in options]
            choice = st.radio(
                category.get('title', 'Choose one'),
                options=option_names,
                index=None,
                horizontal=True,
                key=f"stop_choice_{category.get('title', '')}",
            )
            if choice:
                chosen = next((o for o in options if o.get('name', 'Unknown') == choice), {})
                selected.append((choice, chosen.get('place_id')))
    else:
        labels = [info['name'] for info in places.values()]
        picked = st.multiselect(
            "Pick stops to include, in the order you'll visit them",
            options=labels,
            key="route_stops_selected",
        )
        selected = [(name, None) for name in picked]

    if not selected:
        st.caption("Choose a stop above for each category you need, then get one link with your whole route set up in Maps.")
        return

    name_to_place = {info['name']: info for info in places.values()}
    waypoint_parts = []
    for name, place_id in selected:
        place = places.get(place_id) or name_to_place.get(name)
        # Fall back to the plan's own descriptive name as the waypoint text itself when neither
        # place_id nor an exact name match resolves -- Maps can usually still geocode a specific
        # description like "Public Toilet, Court Road, Gulzarpet", it's just less precise than a
        # verified formatted address, and it beats dropping the stop the user actually picked.
        waypoint_parts.append(
            requests.utils.quote(f"{place['name']}, {place['vicinity']}") if place
            else requests.utils.quote(f"{name}, near {st.session_state.get('destination', '')}")
        )
    waypoints = "|".join(waypoint_parts)
    destination = requests.utils.quote(st.session_state.get('destination', ''))
    maps_url = (
        f"https://www.google.com/maps/dir/?api=1&destination={destination}"
        f"&waypoints={waypoints}&travelmode=driving"
    )
    st.markdown(
        f'<a href="{maps_url}" target="_blank" style="display:inline-block; margin-top:4px; padding:8px 16px; '
        'border-radius:6px; border:1px solid #4285F4; background:#4285F4; color:white; text-decoration:none; '
        'font-weight:600; font-size:14px;">🗺️ Get Directions with Selected Stops</a>',
        unsafe_allow_html=True,
    )


def render_route_map():
    """Draws the calculated route (decoded from its polyline) with a marker for every discovered
    place, so the user can see the trip and stops at a glance instead of only reading about them."""
    polyline = st.session_state.get('route_polyline')
    if not polyline:
        return
    path = decode_polyline(polyline)
    if not path:
        return

    layers = [pdk.Layer(
        "PathLayer",
        data=[{"path": [[lng, lat] for lat, lng in path]}],
        get_path="path",
        get_width=5,
        get_color=[66, 133, 244],
        width_min_pixels=3,
    )]

    places = st.session_state.get('discovered_places', {})
    marker_data = [
        {"lat": info['lat'], "lng": info['lng'], "name": info['name']}
        for info in places.values() if info.get('lat') is not None
    ]
    if marker_data:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=marker_data,
            get_position=["lng", "lat"],
            get_fill_color=[234, 67, 53],
            get_radius=300,
            pickable=True,
        ))

    mid_lat, mid_lng = path[len(path) // 2]
    st.caption("🗺️ Route Map:")
    st.pydeck_chart(pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(latitude=mid_lat, longitude=mid_lng, zoom=8),
        layers=layers,
        tooltip={"text": "{name}"} if marker_data else None,
    ))


def render_place_photos():
    """Shows a photo for each discovered place that has one. Fetched server-side (not linked
    directly as an <img src>) so the Maps API key is never exposed to the browser."""
    places = st.session_state.get('discovered_places', {})
    photo_entries = [(info['name'], info['photo_name']) for info in places.values() if info.get('photo_name')]
    api_key = st.session_state.get('google_maps_api_key')
    if not photo_entries or not api_key:
        return

    st.caption("📸 Photos:")
    cols = st.columns(min(len(photo_entries), 4))
    for i, (name, photo_name) in enumerate(photo_entries[:4]):
        try:
            resp = requests.get(
                f"https://places.googleapis.com/v1/{photo_name}/media",
                params={"maxWidthPx": 400, "key": api_key},
                timeout=5,
            )
            if resp.ok:
                with cols[i % len(cols)]:
                    st.image(resp.content, caption=name, width='stretch')
        except Exception:
            pass


# Generic "spirit of travel" illustrations shown when a place has no Wikipedia thumbnail -- an
# uncommon town, a disambiguation-only title, or just a lookup miss shouldn't make the region panel
# go blank for a real trip someone is actually planning. Two distinct scenes (not one repeated) so
# origin and destination don't show the identical fallback when both lack a photo. Kept as plain
# self-contained SVG -- no external asset, no license question, renders anywhere.
_TRAVEL_SPIRIT_SVGS = [
    # Open road at dawn: the drive itself.
    """<svg viewBox="0 0 400 240" xmlns="http://www.w3.org/2000/svg">
        <defs><linearGradient id="sky1" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#fcd9a8"/><stop offset="55%" stop-color="#f2a25c"/><stop offset="100%" stop-color="#d9714a"/>
        </linearGradient></defs>
        <rect width="400" height="240" fill="url(#sky1)"/>
        <circle cx="200" cy="150" r="34" fill="#fff3d6" opacity="0.9"/>
        <polygon points="0,240 0,190 130,150 0,240" fill="#3a2a1f" opacity="0.85"/>
        <polygon points="400,240 400,190 270,150 400,240" fill="#241a12" opacity="0.9"/>
        <polygon points="130,150 270,150 340,240 60,240" fill="#171310"/>
        <g fill="#f5d9a0"><rect x="196" y="168" width="8" height="14"/><rect x="190" y="192" width="10" height="16"/><rect x="180" y="218" width="14" height="18"/></g>
        <path d="M56 62 q10 -8 20 0 q10 -8 20 0" stroke="#241a12" stroke-width="2" fill="none" opacity="0.6"/>
        <path d="M300 42 q10 -8 20 0 q10 -8 20 0" stroke="#241a12" stroke-width="2" fill="none" opacity="0.6"/>
    </svg>""",
    # Compass and route: the planning itself.
    """<svg viewBox="0 0 400 240" xmlns="http://www.w3.org/2000/svg">
        <rect width="400" height="240" fill="#eef1ea"/>
        <path d="M40 190 C 120 60, 260 220, 360 70" fill="none" stroke="#c9603f" stroke-width="3" stroke-dasharray="2 10" stroke-linecap="round"/>
        <circle cx="40" cy="190" r="6" fill="#123f30"/><circle cx="360" cy="70" r="6" fill="#c9603f"/>
        <g transform="translate(200,120)">
          <circle r="46" fill="none" stroke="#5b6b62" stroke-width="2"/><circle r="3" fill="#5b6b62"/>
          <polygon points="0,-40 8,-4 0,-10 -8,-4" fill="#c9603f"/><polygon points="0,40 8,4 0,10 -8,4" fill="#5b6b62"/>
          <text x="0" y="-52" text-anchor="middle" font-family="monospace" font-size="10" fill="#5b6b62">N</text>
        </g>
    </svg>""",
]

# Shown on the landing page, before any trip is planned. One iconic destination (the Hoysaleswara
# twin shrines at Halebeedu, on their signature stepped star-shaped plinth) and one legendary human
# creation for travel itself (the dockyard at Lothal, a Harappan port city -- among the earliest
# known dockyards in the world, ~2400 BCE, a brick-lined basin linked to the sea by an inlet channel,
# with the warehouse platform's grid of storage bays alongside it). Same reasoning as the fallback
# set above: original illustration, not a licensing question, self-contained.
# Wide banner format (not boxed postcards) meant to sit as a slim decorative strip, not a captioned
# photo -- the density comes from SVG <pattern> fills (running-bond brick coursing, three distinct
# carved-frieze bands) rather than a handful of individual shapes, which is what actually reads as
# "stonework" / "brickwork" at a glance instead of a few dots on a line.
_HOME_ILLUSTRATIONS = [
    # Halebeedu: rhythmic colonnade of lathe-turned pillars over three stacked carved-frieze bands
    # (Hoysala temples layer several distinct narrative bands -- beading, foliage, fretwork -- around
    # the base), rising from the temple's signature stepped star-shaped plinth.
    """<svg viewBox="0 0 1200 220" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="skyGoldenWide" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#f7e2b8"/><stop offset="55%" stop-color="#eab06a"/><stop offset="100%" stop-color="#c97a4f"/>
          </linearGradient>
          <pattern id="frieze1" width="20" height="12" patternUnits="userSpaceOnUse">
            <rect width="20" height="12" fill="#3a3b36"/><circle cx="10" cy="6" r="2.6" fill="#171814"/>
          </pattern>
          <pattern id="frieze2" width="18" height="12" patternUnits="userSpaceOnUse">
            <rect width="18" height="12" fill="#2a2b28"/>
            <path d="M0 9 Q4.5 2 9 9 T18 9" stroke="#171814" stroke-width="1.8" fill="none"/>
          </pattern>
          <pattern id="frieze3" width="16" height="12" patternUnits="userSpaceOnUse">
            <rect width="16" height="12" fill="#3a3b36"/>
            <polygon points="8,1 14,6 8,11 2,6" fill="none" stroke="#171814" stroke-width="1.2"/>
          </pattern>
        </defs>
        <rect width="1200" height="220" fill="url(#skyGoldenWide)"/>
        <circle cx="1080" cy="64" r="38" fill="#fbe7c2" opacity="0.85"/>
        <rect x="0" y="176" width="1200" height="44" fill="#20211d"/>
        <rect x="0" y="122" width="1200" height="12" fill="url(#frieze1)"/>
        <rect x="0" y="134" width="1200" height="12" fill="url(#frieze2)"/>
        <rect x="0" y="146" width="1200" height="12" fill="url(#frieze3)"/>
        <polygon points="0,176 30,162 60,176 90,162 120,176 150,162 180,176 210,162 240,176 270,162 300,176
                          330,162 360,176 390,162 420,176 450,162 480,176 510,162 540,176 570,162 600,176
                          630,162 660,176 690,162 720,176 750,162 780,176 810,162 840,176 870,162 900,176
                          930,162 960,176 990,162 1020,176 1050,162 1080,176 1110,162 1140,176 1170,162 1200,176
                          1200,220 0,220" fill="#171814"/>
        <!-- pillars: inlined at each x (not <use href>) so this renders in any SVG viewer, not just
             browsers that support SVG2 bare href on <use> -- xlink:href would work everywhere too,
             but duplicating nine small shapes costs nothing and needs no namespace declaration. -->
        <g fill="#171814">
          <g transform="translate(60,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(160,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(260,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(360,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(460,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(860,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(950,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(1040,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
          <g transform="translate(1130,122)"><rect x="-3" y="0" width="6" height="58"/><ellipse cx="0" cy="14" rx="6.5" ry="4"/><ellipse cx="0" cy="27" rx="7.5" ry="4.6"/><ellipse cx="0" cy="40" rx="6" ry="3.6"/></g>
        </g>
        <g fill="#0f100d">
          <polygon points="640,122 590,122 604,80 626,80"/>
          <polygon points="598,80 632,80 622,52 608,52"/>
          <polygon points="608,52 622,52 618,32"/>
          <circle cx="615" cy="27" r="4"/>
          <polygon points="800,122 750,122 764,80 786,80"/>
          <polygon points="758,80 792,80 782,52 768,52"/>
          <polygon points="768,52 782,52 778,32"/>
          <circle cx="775" cy="27" r="4"/>
          <polygon points="720,122 660,122 690,66"/>
          <rect x="668" y="100" width="5" height="22"/><rect x="686" y="100" width="5" height="22"/><rect x="704" y="100" width="5" height="22"/>
        </g>
    </svg>""",
    # Lothal: a full site-plan composition (citadel with buildings, warehouse grid, lower-town
    # blocks, perimeter wall, river with green banks, dock with several boats, small figures and
    # elephants for scale) -- not just a dock silhouette. Same flat-vector language as the rest of
    # this set, not a painterly rendering.
    """<svg viewBox="0 0 1400 320" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="skyDayLothal" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#bfe0ee"/><stop offset="55%" stop-color="#e7f0e0"/><stop offset="100%" stop-color="#e7c98a"/>
          </linearGradient>
          <pattern id="brickL" width="22" height="11" patternUnits="userSpaceOnUse">
            <rect width="22" height="11" fill="#a15a34"/>
            <rect x="0" y="0" width="10" height="4.5" fill="#7a3f24"/><rect x="11" y="0" width="10" height="4.5" fill="#7a3f24"/>
            <rect x="5.5" y="5.5" width="10" height="4.5" fill="#7a3f24"/><rect x="16.5" y="5.5" width="5" height="4.5" fill="#7a3f24"/>
            <rect x="-5.5" y="5.5" width="5" height="4.5" fill="#7a3f24"/>
          </pattern>
        </defs>
        <rect width="1400" height="320" fill="url(#skyDayLothal)"/>
        <circle cx="1250" cy="46" r="26" fill="#fff3d2" opacity="0.9"/>
        <rect x="0" y="20" width="1400" height="50" fill="#bcd9a0" opacity="0.7"/>
        <g stroke="#9fc084" stroke-width="1.5" opacity="0.6"><line x1="0" y1="35" x2="1400" y2="35"/><line x1="0" y1="52" x2="1400" y2="52"/></g>

        <!-- river along the right, feeding the dock -->
        <path d="M1400,60 C1300,90 1320,150 1260,200 C1220,230 1230,270 1200,300 L1400,320 Z" fill="#5ea3bd" opacity="0.85"/>
        <rect x="1150" y="60" width="60" height="260" fill="#a9cf8e" opacity="0.5"/>

        <!-- settlement ground -->
        <rect x="0" y="70" width="1180" height="250" fill="#d9b483"/>
        <!-- perimeter wall -->
        <polygon points="40,90 1000,80 1150,120 1150,300 40,300" fill="none" stroke="#6b3820" stroke-width="4"/>

        <!-- citadel: stepped brick platform with buildings on top -->
        <polygon points="60,220 60,150 120,110 380,110 440,150 440,220" fill="url(#brickL)" stroke="#6b3820" stroke-width="2"/>
        <polygon points="90,220 90,170 130,140 350,140 390,170 390,220" fill="url(#brickL)" stroke="#6b3820" stroke-width="1.5"/>
        <g fill="#7a3f24" stroke="#5c2f1c" stroke-width="1.5">
          <rect x="140" y="90" width="60" height="55"/><rect x="205" y="78" width="90" height="67"/><rect x="300" y="95" width="55" height="50"/>
        </g>
        <g fill="#5c2f1c">
          <rect x="155" y="105" width="8" height="14"/><rect x="175" y="105" width="8" height="14"/>
          <rect x="225" y="95" width="8" height="16"/><rect x="248" y="95" width="8" height="16"/><rect x="271" y="95" width="8" height="16"/>
          <rect x="315" y="110" width="8" height="14"/>
        </g>

        <!-- warehouse: brick platform with its grid of storage blocks -->
        <rect x="480" y="150" width="380" height="110" fill="url(#brickL)" stroke="#6b3820" stroke-width="2.5"/>
        <g stroke="#6b3820" stroke-width="1.3">
          <line x1="527" y1="150" x2="527" y2="260"/><line x1="575" y1="150" x2="575" y2="260"/>
          <line x1="623" y1="150" x2="623" y2="260"/><line x1="670" y1="150" x2="670" y2="260"/>
          <line x1="718" y1="150" x2="718" y2="260"/><line x1="765" y1="150" x2="765" y2="260"/><line x1="813" y1="150" x2="813" y2="260"/>
          <line x1="480" y1="205" x2="860" y2="205"/>
        </g>

        <!-- lower town: grid of small building blocks -->
        <g fill="#c99a63" stroke="#8a5a34" stroke-width="1">
          <rect x="500" y="270" width="34" height="26"/><rect x="540" y="270" width="34" height="26"/><rect x="580" y="270" width="34" height="26"/>
          <rect x="620" y="270" width="34" height="26"/><rect x="660" y="270" width="34" height="26"/><rect x="700" y="270" width="34" height="26"/>
          <rect x="740" y="270" width="34" height="26"/><rect x="780" y="270" width="34" height="26"/><rect x="820" y="270" width="34" height="26"/>
          <rect x="860" y="270" width="34" height="26"/><rect x="900" y="270" width="34" height="26"/><rect x="940" y="270" width="34" height="26"/>
        </g>

        <!-- dock basin fed by the river, with several boats -->
        <polygon points="960,240 1150,232 1180,300 930,310" fill="url(#brickL)" stroke="#6b3820" stroke-width="3"/>
        <polygon points="978,248 1130,241 1155,292 950,300" fill="#4c8fa6" opacity="0.8"/>
        <g transform="translate(1010,262)">
          <path d="M-20 7 Q0 15 20 7 L16 12 Q0 17 -16 12 Z" fill="#3a2416"/>
          <rect x="-1.5" y="-20" width="2.5" height="26" fill="#3a2416"/>
          <polygon points="1,-20 1,-4 19,-8" fill="#e8d3a6" opacity="0.92"/>
        </g>
        <g transform="translate(1075,272)">
          <path d="M-15 5 Q0 11 15 5 L12 9 Q0 13 -12 9 Z" fill="#3a2416"/>
          <rect x="-1.2" y="-14" width="2" height="18" fill="#3a2416"/>
          <polygon points="0.8,-14 0.8,-2 13,-5" fill="#e8d3a6" opacity="0.92"/>
        </g>
        <g transform="translate(1260,220)">
          <path d="M-16 6 Q0 12 16 6 L13 10 Q0 14 -13 10 Z" fill="#3a2416"/>
          <rect x="-1.3" y="-16" width="2.2" height="20" fill="#3a2416"/>
          <polygon points="0.9,-16 0.9,-3 15,-6" fill="#e8d3a6" opacity="0.92"/>
        </g>

        <!-- small figures and a pack elephant for narrative life -->
        <g fill="#3a2416" opacity="0.85">
          <g transform="translate(440,225)"><circle r="3"/><rect x="-1" y="3" width="2" height="10"/></g>
          <g transform="translate(465,230)"><circle r="3"/><rect x="-1" y="3" width="2" height="10"/></g>
          <g transform="translate(870,220)"><circle r="3"/><rect x="-1" y="3" width="2" height="10"/></g>
          <g transform="translate(895,225)"><circle r="3"/><rect x="-1" y="3" width="2" height="10"/></g>
        </g>
        <g transform="translate(60,235)" fill="#6b5644">
          <ellipse cx="0" cy="10" rx="16" ry="10"/><ellipse cx="15" cy="4" rx="8" ry="7"/>
          <path d="M22 4 Q30 8 27 16" stroke="#6b5644" stroke-width="3" fill="none"/>
          <rect x="-9" y="16" width="4" height="10"/><rect x="4" y="16" width="4" height="10"/>
        </g>
    </svg>""",
]


def _svg_data_uri(svg: str) -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _travel_spirit_data_uri(index: int) -> str:
    return _svg_data_uri(_TRAVEL_SPIRIT_SVGS[index % len(_TRAVEL_SPIRIT_SVGS)])


def render_home_illustrations():
    """Shown only before a trip is planned -- once planning_triggered is set, the actual plan
    (and its own imagery) takes over and this shouldn't linger.

    Deliberately full-width strips with no caption underneath, not boxed side-by-side postcards --
    these are meant to read as an ambient decorative band behind the header, not as two labeled
    tourist photos."""
    if st.session_state.get('planning_triggered'):
        return
    for svg in _HOME_ILLUSTRATIONS:
        st.markdown(
            f'<img src="{_svg_data_uri(svg)}" style="width:100%; height:110px; object-fit:cover; '
            'display:block; margin-bottom:2px;">',
            unsafe_allow_html=True,
        )


def render_region_postcards():
    """Shows real Wikipedia imagery for the origin and destination the user actually searched for,
    in the sidebar -- Streamlit's existing side panel, reused for this instead of building a custom
    layout. Genuinely dynamic per trip (unlike a fixed demo image set): it looks up whatever
    origin/destination is in session state, so it changes with every search. A place with no
    Wikipedia thumbnail falls back to a generic travel illustration instead of leaving a hole."""
    origin = st.session_state.get('origin', '')
    destination = st.session_state.get('destination', '')
    places = [p for p in (origin, destination) if p]
    if not places:
        return

    seen_titles = set()
    with st.sidebar:
        st.markdown("---")
        st.caption("🖼️ Along your route")
        for i, place in enumerate(places):
            card = get_wikipedia_thumbnail(place)
            if card and card['title'] not in seen_titles:
                seen_titles.add(card['title'])
                st.image(card['thumbnail_url'], width='stretch')
                if card.get('page_url'):
                    st.caption(f"[{card['title']}]({card['page_url']}) · Wikipedia")
                else:
                    st.caption(card['title'])
            elif not card:
                st.markdown(
                    f'<img src="{_travel_spirit_data_uri(i)}" style="width:100%; border-radius:6px;">',
                    unsafe_allow_html=True,
                )
                st.caption(place.split(",")[0].strip())


# --- Structured response schema & rendering ---
#
# Gemini 3 models support combining function calling with response_schema/response_mime_type --
# the model still calls tools freely mid-turn, but its final text turn (once it's done calling
# tools) is forced into this JSON shape instead of whatever ad-hoc Markdown it feels like that run.
# This was added because two runs on the same route/preferences produced completely different
# layouts (a Markdown table one time, a numbered list the next) even though the underlying content
# was equally good -- the *shape* wasn't reliable, so the app couldn't build any stable UI around
# it. The app renders this JSON into Markdown itself (render_structured_response below), so layout
# is now the app's job, not the model's whim, while the actual wording inside each string field
# (verdicts, proactive notes, review takeaways) stays genuinely model-written, conversational text.
#
# response_type lets the same schema cover both a full trip plan and a plain conversational
# follow-up ("why did you suggest that one?") without forcing every follow-up answer into the full
# itinerary shape.
#
# As of this writing Google documents structured-output + tools together as a preview feature for
# Gemini 3 models, not yet guaranteed -- parse_structured_response() below falls back to showing the
# raw response text untouched if the model doesn't return valid JSON, so a preview-feature hiccup
# degrades to the old plain-Markdown behavior instead of breaking the app.
_OPTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "name": {"type": "STRING"},
        "place_id": {"type": "STRING", "nullable": True, "description": "The place_id from get_place_details_and_reviews, if this option came from a tool result."},
        "rating_text": {"type": "STRING", "nullable": True, "description": "e.g. '4.0 (2,082 reviews)'"},
        "price_level": {"type": "STRING", "nullable": True, "description": "e.g. 'Budget', 'Moderate', 'Expensive'"},
        "hours_status": {"type": "STRING", "nullable": True, "description": "e.g. 'Open now, closes 11:00 PM'"},
        "parking": {"type": "STRING", "nullable": True},
        "elder_suitability": {"type": "STRING", "nullable": True, "description": "Only for food stops when the trip involves elderly travelers."},
        "review_snippet": {"type": "STRING", "nullable": True},
        "review_recency": {"type": "STRING", "nullable": True, "description": "REQUIRED whenever this option came from get_place_details_and_reviews: copy most_recent_review.relative_time verbatim (e.g. '3 weeks ago'). This is what lets the user judge whether the star rating still reflects the place today, not just what it was years ago. Only null for an option with no tool result to read it from."},
        "critical_review_snippet": {"type": "STRING", "nullable": True, "description": "REQUIRED field (value may be null, but the field itself must always be set, never omitted): copy critical_review.text verbatim if get_place_details_and_reviews returned a critical_review for this place, so the user sees a real downside alongside the positive quote -- set explicitly to null only when critical_review was actually null (every review Google returned was positive), never left out just because a bad review would make the option look worse."},
        "verdict": {"type": "STRING", "description": "The concierge's honest, specific take on this option -- pros/cons, not a single winner declaration."},
    },
    "required": ["name", "verdict", "review_recency", "critical_review_snippet"],
}

_STOP_CATEGORY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "emoji": {"type": "STRING"},
        "title": {"type": "STRING", "description": "e.g. 'Pure Veg Food Stops', 'Fuel / Petrol Options'"},
        "options": {"type": "ARRAY", "items": _OPTION_SCHEMA},
    },
    "required": ["title", "options"],
}

_PLAN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intro_text": {"type": "STRING", "nullable": True, "description": "A short, conversational opening line."},
        "trip_overview": {
            "type": "OBJECT",
            "properties": {
                "distance_text": {"type": "STRING"},
                "duration_text": {"type": "STRING"},
                "toll_cost_text": {"type": "STRING", "nullable": True},
                "departure_time_text": {"type": "STRING"},
                "arrival_time_text": {"type": "STRING"},
            },
            "required": ["distance_text", "duration_text", "departure_time_text", "arrival_time_text"],
        },
        "itinerary_timeline": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "time": {"type": "STRING"},
                    "label": {"type": "STRING"},
                },
                "required": ["time", "label"],
            },
        },
        "proactive_notes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "emoji": {"type": "STRING"},
                    "title": {"type": "STRING"},
                    "text": {"type": "STRING"},
                },
                "required": ["title", "text"],
            },
        },
        "stop_categories": {"type": "ARRAY", "items": _STOP_CATEGORY_SCHEMA},
    },
    "required": ["trip_overview", "itinerary_timeline", "stop_categories"],
}

CONCIERGE_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "response_type": {"type": "STRING", "enum": ["plan", "answer"]},
        "plan": {**_PLAN_SCHEMA, "nullable": True},
        "answer_text": {"type": "STRING", "nullable": True, "description": "Used when response_type is 'answer' -- a plain conversational reply, not a full plan."},
    },
    "required": ["response_type"],
}


def parse_structured_response(response_text: str) -> dict | None:
    """Parses the model's JSON text into a dict, tolerating a ```json fenced code block (some
    models wrap JSON output in one even when told not to). Returns None if it's not valid JSON --
    the caller falls back to showing the raw text as plain Markdown."""
    text = response_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _format_option_markdown(option: dict, index: int | None = None) -> list[str]:
    """Markdown lines for one option -- shared by render_structured_response (historical/fallback
    Markdown rendering) and render_plan_cards (the live plan's actual card UI) so the two never
    drift out of sync with each other."""
    name = option.get("name", "Unknown")
    header = f"**{index}. {name}**" if index is not None else f"**{name}**"
    if option.get("rating_text"):
        header += f" — {option['rating_text']}"
    lines = [header]
    details = []
    if option.get("price_level"):
        details.append(f"Price: {option['price_level']}")
    if option.get("parking"):
        details.append(f"Parking: {option['parking']}")
    if option.get("hours_status"):
        details.append(f"Hours: {option['hours_status']}")
    if details:
        lines.append("- " + " | ".join(details))
    if option.get("elder_suitability"):
        lines.append(f"- Elder-friendly: {option['elder_suitability']}")
    if option.get("review_snippet"):
        recency = f" (most recent review: {option['review_recency']})" if option.get("review_recency") else ""
        lines.append(f"- _\"{option['review_snippet']}\"_{recency}")
    if option.get("critical_review_snippet"):
        lines.append(f"- ⚠️ _\"{option['critical_review_snippet']}\"_")
    if option.get("verdict"):
        lines.append(f"- **Take:** {option['verdict']}")
    return lines


def _format_option_plain(option: dict) -> str:
    """Plain-text version of one option, for the per-option Copy/WhatsApp button -- deliberately not
    just the Markdown lines joined together, since '**' shows up as literal asterisks once pasted
    into WhatsApp or a text message instead of rendering as bold."""
    name = option.get("name", "Unknown")
    header = name + (f" — {option['rating_text']}" if option.get("rating_text") else "")
    lines = [header]
    details = []
    if option.get("price_level"):
        details.append(f"Price: {option['price_level']}")
    if option.get("parking"):
        details.append(f"Parking: {option['parking']}")
    if option.get("hours_status"):
        details.append(f"Hours: {option['hours_status']}")
    if details:
        lines.append(" | ".join(details))
    if option.get("elder_suitability"):
        lines.append(f"Elder-friendly: {option['elder_suitability']}")
    if option.get("review_snippet"):
        recency = f" (most recent review: {option['review_recency']})" if option.get("review_recency") else ""
        lines.append(f"\"{option['review_snippet']}\"{recency}")
    if option.get("critical_review_snippet"):
        lines.append(f"Unfavorable review: \"{option['critical_review_snippet']}\"")
    if option.get("verdict"):
        lines.append(f"Take: {option['verdict']}")
    return "\n".join(lines)


def _google_maps_place_url(option: dict) -> str:
    """Google's documented Maps URLs API pattern for linking straight to one place: query_place_id
    pins the exact business (not just a text search that might land on the wrong branch/duplicate
    listing), with `query` as the required fallback search text if the place_id ever fails to
    resolve. Falls back to a plain name search when there's no place_id -- e.g. a follow-up answer
    that references a place from an earlier turn without re-running the search tools."""
    name = option.get("name", "")
    query = requests.utils.quote(name)
    if option.get("place_id"):
        return f"https://www.google.com/maps/search/?api=1&query={query}&query_place_id={option['place_id']}"
    return f"https://www.google.com/maps/search/?api=1&query={query}"


def render_structured_response(data: dict) -> str:
    """Renders the structured JSON response into Markdown -- this is now where the app's visual
    layout is decided, not left up to the model's formatting choice on any given run."""
    if data.get("response_type") == "answer":
        return data.get("answer_text") or "_(No answer text returned.)_"

    plan = data.get("plan")
    if not plan:
        return "_(The model marked this as a plan but returned no plan data.)_"

    lines = []
    if plan.get("intro_text"):
        lines.append(plan["intro_text"])
        lines.append("")

    overview = plan.get("trip_overview", {})
    lines.append("### 🚗 Trip Overview")
    lines.append(f"- **Distance:** {overview.get('distance_text', '—')}")
    lines.append(f"- **Drive Duration:** {overview.get('duration_text', '—')}")
    if overview.get("toll_cost_text"):
        lines.append(f"- **Estimated Toll Cost:** {overview['toll_cost_text']}")
    lines.append(f"- **Departure:** {overview.get('departure_time_text', '—')}")
    lines.append(f"- **Estimated Arrival:** {overview.get('arrival_time_text', '—')}")
    lines.append("")

    timeline = plan.get("itinerary_timeline") or []
    if timeline:
        lines.append("### ⏱️ Itinerary Timeline")
        lines.append("| Time | Stop |")
        lines.append("|---|---|")
        for item in timeline:
            lines.append(f"| {item.get('time', '')} | {item.get('label', '')} |")
        lines.append("")

    notes = plan.get("proactive_notes") or []
    if notes:
        lines.append("### 💡 Proactive Notes")
        for note in notes:
            title = note.get("title", "")
            emoji = note.get("emoji", "💡")
            lines.append(f"**{emoji} {title}:** {note.get('text', '')}")
            lines.append("")

    for category in plan.get("stop_categories") or []:
        lines.append(f"### {category.get('emoji', '')} {category.get('title', '')}".strip())
        lines.append("")
        for i, option in enumerate(category.get("options") or [], start=1):
            lines.extend(_format_option_markdown(option, index=i))
            lines.append("")

    return "\n".join(lines).strip()


def render_print_button():
    """window.print() has to target the parent document, not the sandboxed iframe it runs in --
    st.iframe's content is same-origin but still its own document, so a bare print() here would
    print just this little button, not the actual page."""
    st.iframe(
        """
        <button onclick="window.parent.print()" style="padding:6px 14px; border-radius:6px; border:1px solid #999;
        background:#f0f2f6; color:#31333F; cursor:pointer; font-size:14px;">🖨️ Print</button>
        """,
        height=45,
    )


# "The Strip" -- the route-native layout explored via an Artifact mockup and documented in
# DESIGN_CONCEPTS.md, built here against real data instead of hand-authored HTML. Self-contained
# palette (not reconciled with the rest of the app's theme -- see DESIGN.md's own note that nothing
# in this app is on one shared token set yet), but does respect light/dark via prefers-color-scheme.
_STRIP_CSS = """
<style>
.strip {
    --strip-ink: #1a1a1a; --strip-paper: #f5f4f0; --strip-paper-raised: #ffffff;
    --strip-line: #b8b6ae; --strip-mute: #75726a; --strip-accent: #b3502c;
    position: relative; display: flex; flex-direction: column; padding: 18px 16px 18px 46px;
    min-height: 440px; margin: 4px 0 18px; background: var(--strip-paper); border-radius: 8px;
}
@media (prefers-color-scheme: dark) {
    .strip {
        --strip-ink: #ece9e2; --strip-paper: #1c1b18; --strip-paper-raised: #262521;
        --strip-line: #45433c; --strip-mute: #a29d90; --strip-accent: #e08a5c;
    }
}
.strip::before {
    content: ""; position: absolute; left: 18px; top: 22px; bottom: 22px; width: 4px;
    background: repeating-linear-gradient(var(--strip-ink) 0 14px, transparent 14px 22px);
}
.strip-node { position: relative; flex: 0 0 auto; color: var(--strip-ink); }
.strip-node::before {
    content: ""; position: absolute; left: -34px; top: 4px; width: 12px; height: 12px; border-radius: 50%;
    background: var(--strip-paper); border: 3px solid var(--strip-ink);
}
.strip-node.decision::before { border-color: var(--strip-accent); }
.strip-km {
    position: absolute; left: -30px; top: -13px; white-space: nowrap; font-size: 0.68rem;
    color: var(--strip-mute); font-variant-numeric: tabular-nums;
}
.strip-row { padding: 6px 0 4px; }
.strip-title { font-weight: 700; }
.strip-time { color: var(--strip-mute); font-size: 0.9rem; margin-left: 8px; }
.strip-gap { flex-shrink: 0; min-height: 20px; position: relative; display: flex; align-items: center; }
.strip-gap-label { font-size: 0.66rem; color: var(--strip-mute); font-style: italic; }
.strip-cluster {
    border-left: 2px solid var(--strip-accent); margin-top: 6px; padding-left: 14px;
    display: flex; flex-wrap: wrap; gap: 6px;
}
.strip-chip {
    font-size: 0.82rem; padding: 6px 10px; border-radius: 4px; border: 1px solid var(--strip-line);
    background: var(--strip-paper-raised); color: var(--strip-ink); max-width: 240px;
}
.strip-chip-rating { color: var(--strip-mute); font-size: 0.78rem; }
.strip-chip-verdict { font-size: 0.74rem; color: var(--strip-mute); margin-top: 2px; }
</style>
"""


def render_the_strip(plan: dict) -> bool:
    """Route-native itinerary visualization: vertical position is real km along the route, segment
    length between stops is flex-grow(sqrt(gap_km)) against a fixed track (see DESIGN_CONCEPTS.md
    for why sqrt, not linear km or hand-placed pixels), computed by matching each stop's actual
    coordinates against the decoded route polyline -- not a guessed or model-estimated position.

    Each stop_categories entry becomes one decision cluster, positioned at the average real
    distance-along-route of its options that have a resolvable place_id/coordinate (most won't
    differ by more than a km or two in practice, since a category's candidates are all searched
    along the same stretch of road). A category with no resolvable coordinate at all is silently
    skipped -- it simply can't be placed on a route-native layout without one.

    Returns True if it actually rendered something, so the caller can fall back to the plain
    itinerary table when there's no polyline yet (e.g. structured output degraded to plain text for
    this turn) instead of the trip silently having no itinerary visualization at all."""
    polyline = st.session_state.get('route_polyline')
    places = st.session_state.get('discovered_places', {})
    if not polyline:
        return False

    path = decode_polyline(polyline)
    if len(path) < 2:
        return False
    cum = route_cumulative_km(path)
    total_km = cum[-1]
    if total_km <= 0:
        return False

    overview = plan.get('trip_overview', {})
    origin = st.session_state.get('origin', 'Origin')
    destination = st.session_state.get('destination', 'Destination')

    nodes = [{
        "km": 0.0, "title": f"Depart {origin}", "time": overview.get('departure_time_text', ''),
        "decision": False, "options": None,
    }]
    for category in plan.get('stop_categories') or []:
        options = category.get('options') or []
        positions = []
        for opt in options:
            place = places.get(opt.get('place_id'))
            if place and place.get('lat') is not None and place.get('lng') is not None:
                positions.append(distance_along_route_km(path, cum, place['lat'], place['lng']))
        if not positions:
            continue
        nodes.append({
            "km": sum(positions) / len(positions),
            "title": f"{category.get('emoji', '')} {category.get('title', '')}".strip(),
            "time": None, "decision": True, "options": options,
        })
    nodes.append({
        "km": total_km, "title": f"Arrive {destination}", "time": overview.get('arrival_time_text', ''),
        "decision": False, "options": None,
    })
    nodes.sort(key=lambda n: n["km"])

    if len(nodes) <= 2:
        # Only depart/arrive, no placeable category -- the plain table is more useful than an
        # almost-empty Strip with nothing but two endpoints on it.
        return False

    def esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = []
    for i, node in enumerate(nodes):
        node_class = "strip-node decision" if node["decision"] else "strip-node"
        km_label = f"{node['km']:.0f}km" if node["km"] >= 0.5 else "0km"
        time_html = f'<span class="strip-time">{esc(node["time"])}</span>' if node.get("time") else ""
        title_html = f'<span class="strip-title">{esc(node["title"])}</span>'

        options_html = ""
        if node["options"]:
            chips = []
            for opt in node["options"][:3]:
                name = esc(opt.get("name", "Unknown"))
                rating = esc(opt.get("rating_text", ""))
                verdict = esc(opt.get("verdict", ""))
                rating_html = f'<span class="strip-chip-rating"> · {rating}</span>' if rating else ""
                verdict_html = f'<div class="strip-chip-verdict">{verdict}</div>' if verdict else ""
                chips.append(f'<div class="strip-chip"><b>{name}</b>{rating_html}{verdict_html}</div>')
            options_html = f'<div class="strip-cluster">{"".join(chips)}</div>'

        parts.append(
            f'<div class="{node_class}"><span class="strip-km">{km_label}</span>'
            f'<div class="strip-row">{title_html}{time_html}{options_html}</div></div>'
        )

        if i < len(nodes) - 1:
            gap_km = max(nodes[i + 1]["km"] - node["km"], 0.01)
            flex = max(math.sqrt(gap_km), 0.3)
            gap_label = f"{gap_km:.0f} km" if gap_km >= 1 else f"{gap_km * 1000:.0f} m"
            parts.append(
                f'<div class="strip-gap" style="flex-grow:{flex:.2f}">'
                f'<span class="strip-gap-label">{gap_label}</span></div>'
            )

    st.markdown(_STRIP_CSS + f'<div class="strip">{"".join(parts)}</div>', unsafe_allow_html=True)
    return True


def render_plan_cards(plan: dict):
    """Renders the current active plan as real Streamlit elements -- a card per option with its own
    copy/share control, plus a whole-itinerary share/print section -- instead of one flat Markdown
    blob. A plain Markdown string has no seams to hang per-option controls off of, which is why this
    exists as a separate path from render_structured_response.

    Only used for the LATEST plan message (see latest_plan_message_index in the render loop below);
    older, superseded plans still in the chat history render as plain Markdown -- per-option
    controls on a plan that's no longer the active one would just be confusing, and the whole-
    itinerary share/print controls should only ever act on the current plan."""
    if plan.get("intro_text"):
        st.markdown(plan["intro_text"])

    overview = plan.get("trip_overview", {})
    overview_lines = [
        "### 🚗 Trip Overview",
        f"- **Distance:** {overview.get('distance_text', '—')}",
        f"- **Drive Duration:** {overview.get('duration_text', '—')}",
    ]
    if overview.get("toll_cost_text"):
        overview_lines.append(f"- **Estimated Toll Cost:** {overview['toll_cost_text']}")
    overview_lines.append(f"- **Departure:** {overview.get('departure_time_text', '—')}")
    overview_lines.append(f"- **Estimated Arrival:** {overview.get('arrival_time_text', '—')}")
    st.markdown("\n".join(overview_lines))

    st.markdown("### ⏱️ Itinerary")
    if not render_the_strip(plan):
        timeline = plan.get("itinerary_timeline") or []
        if timeline:
            rows = "\n".join(f"| {item.get('time', '')} | {item.get('label', '')} |" for item in timeline)
            st.markdown("| Time | Stop |\n|---|---|\n" + rows)

    notes = plan.get("proactive_notes") or []
    if notes:
        st.markdown("### 💡 Proactive Notes")
        for note in notes:
            st.markdown(f"**{note.get('emoji', '💡')} {note.get('title', '')}:** {note.get('text', '')}")

    for category in plan.get("stop_categories") or []:
        st.markdown(f"### {category.get('emoji', '')} {category.get('title', '')}".strip())
        for i, option in enumerate(category.get("options") or [], start=1):
            with st.container(border=True):
                st.markdown("\n\n".join(_format_option_markdown(option, index=i)))
                render_copy_and_share(_format_option_plain(option))
                st.markdown(
                    f'<a href="{_google_maps_place_url(option)}" target="_blank" rel="noopener" '
                    'style="font-size:13px; color:#4285F4; text-decoration:none;">🔗 View on Google Maps ↗</a>',
                    unsafe_allow_html=True,
                )

    st.markdown("---")
    st.markdown("##### 📤 Share or print the full itinerary")
    share_col, print_col = st.columns([3, 1])
    with share_col:
        render_copy_and_share(render_structured_response({"response_type": "plan", "plan": plan}))
    with print_col:
        render_print_button()


def response_to_markdown(response_text: str) -> tuple[str, dict]:
    """Turns the model's raw response text into the Markdown actually shown in chat -- structured
    JSON if it parsed and rendered cleanly, otherwise the raw text untouched (see the preview-feature
    note above CONCIERGE_RESPONSE_SCHEMA for why this fallback exists).

    Also returns a {structured_ok, response_type} dict for log_usage_event -- this is the only place
    that knows whether structured output actually worked on this turn, so it's the natural place to
    capture that as a performance signal instead of re-deriving it at the call site.

    As a side effect, stashes data['plan'] into st.session_state.latest_plan whenever this turn is a
    successfully-parsed 'plan' response -- that's what lets render_plan_cards, the stop-selector, and
    the share/print controls act on the plan the user is actually looking at, not just its rendered
    Markdown text. A conversational 'answer' turn leaves the previous plan in place deliberately: the
    active plan hasn't changed just because the user asked a question about it."""
    data = parse_structured_response(response_text)
    if data is None:
        logger.warning("structured response parse failed, showing raw text (len=%d)", len(response_text))
        return response_text, {"structured_ok": False, "response_type": None}
    try:
        markdown = render_structured_response(data)
        if data.get("response_type") == "plan" and data.get("plan"):
            st.session_state.latest_plan = data["plan"]
        return markdown, {"structured_ok": True, "response_type": data.get("response_type")}
    except Exception:
        logger.exception("structured response rendering failed, showing raw text")
        return response_text, {"structured_ok": False, "response_type": data.get("response_type")}


# --- Streamlit UI ---
st.set_page_config(page_title="🧭 Journey Concierge", layout="wide")

st.title("🧭 Journey Concierge")
st.caption(
    "Live, verified stops for food, fuel, and rest — planned along your actual route and verified "
    "live on Google Maps."
)
render_home_illustrations()

# .streamlit/config.toml's theme.font/headingFont correctly set the CSS font-family (confirmed via
# computed style -- "Karla" and "Saira Condensed" both show up as the primary font), but on this
# installed Streamlit build (1.62.0) it never actually fetches the font file: no <link>, no
# @font-face rule, no network request, despite the docs bundled with this exact package describing
# "name:url" as loading the font automatically. Verified the URL itself is fine (fetched clean
# @font-face CSS directly). So the font-family is declared but nothing on the page can render it,
# and every element silently falls back to Streamlit's default font. This adds only the missing
# stylesheet link -- config.toml still owns which font-family is requested.
st.markdown(
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Karla:ital,wght@0,400;0,500;0,700;1,400'
    '&family=Saira+Condensed:wght@500;600;700&display=swap" rel="stylesheet">',
    unsafe_allow_html=True,
)

# Printing the page as-is would include the sidebar, the trip-details inputs, the chat input box,
# and every interactive control (copy/share/print buttons themselves) -- none of that belongs on a
# printout of the itinerary. This only hides the chrome that's reliably identifiable by a stable
# data-testid across reruns; the trip-details form itself isn't wrapped in anything targetable yet,
# so it still prints for now (acceptable as context, not ideal).
st.markdown(
    """<style>
    @media print {
        [data-testid="stSidebar"], [data-testid="stChatInput"], [data-testid="stHeader"],
        [data-testid="stStatusWidget"], .stButton, iframe { display: none !important; }
    }
    </style>""",
    unsafe_allow_html=True,
)

# API keys always come from the server environment now -- this stopped being a shared workshop
# deployment where each attendee needed to bring their own key, so the manual entry UI was just
# clutter (and removing it frees up the sidebar for the route imagery below instead of key inputs).
st.session_state.google_maps_api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
st.session_state.gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
if not st.session_state.google_maps_api_key or not st.session_state.gemini_api_key:
    st.error(
        "This deployment is missing its server-side API keys (GOOGLE_MAPS_API_KEY / GEMINI_API_KEY) "
        "-- set them as environment variables (or in .env for local dev) to use the app."
    )

# Main Page Inputs
st.header("Trip Details")

col1, col2 = st.columns(2)

with col1:
    # Was Anantapur -> Kurnool (this app's original testing route, per HANDOFF.md -- "this app's
    # home turf"). Bengaluru -> Ooty is a classic weekend road trip with friends -- a widely
    # recognizable, genuinely popular hill-station corridor -- a better first impression for someone
    # who's never used the app before, and pairs naturally with the "road trip with friends" default
    # preferences text below.
    origin_search = st.text_input("Search Origin", value="Bengaluru")
    if st.session_state.get("google_maps_api_key") and origin_search:
        origin_options = get_place_predictions(origin_search, st.session_state.google_maps_api_key)
    else:
        origin_options = [origin_search]
    origin = st.selectbox("📍 Confirmed Origin (Google Maps)", options=origin_options)

with col2:
    dest_search = st.text_input("Search Destination", value="Ooty")
    if st.session_state.get("google_maps_api_key") and dest_search:
        dest_options = get_place_predictions(dest_search, st.session_state.google_maps_api_key)
    else:
        dest_options = [dest_search]
    destination = st.selectbox("🎯 Confirmed Destination (Google Maps)", options=dest_options)

origin_tz_name = get_timezone_for_location(origin, st.session_state.get("google_maps_api_key", ""))
origin_tz = ZoneInfo(origin_tz_name)
now_local = datetime.now(origin_tz)
st.caption(f"🕐 Times below are local time at your origin ({origin_tz_name.replace('_', ' ')}).")

st.markdown("**Departure Date**")
# Pattern for all three quick-select button rows in this file (date, then time below): writing to
# st.session_state[key] BEFORE the widget with that key is instantiated overrides its value for
# this rerun. Doing it the other way around (setting state after the widget call) raises, since
# Streamlit already owns that key by then -- the buttons must run first in the script.
date_col1, date_col2, date_col3 = st.columns(3)
with date_col1:
    if st.button("Today", width='stretch'):
        st.session_state.departure_date = now_local.date()
with date_col2:
    if st.button("Tomorrow", width='stretch'):
        st.session_state.departure_date = now_local.date() + timedelta(days=1)
with date_col3:
    if st.button("Day after", width='stretch'):
        st.session_state.departure_date = now_local.date() + timedelta(days=2)
departure_date = st.date_input(
    "Departure date", key="departure_date", value=now_local.date(), label_visibility="collapsed"
)

def _format_time_12h(t):
    return t.strftime("%I:%M %p").lstrip("0")

st.markdown("**Departure Time**")
time_col1, time_col2, time_col3 = st.columns(3)
with time_col1:
    if st.button("Now", width='stretch'):
        st.session_state.departure_time_text = _format_time_12h(now_local)
with time_col2:
    if st.button("1 hr from now", width='stretch'):
        st.session_state.departure_time_text = _format_time_12h(now_local + timedelta(hours=1))
with time_col3:
    st.button("Custom", width='stretch', disabled=True, help="Type any time below, e.g. '630pm' or '6:30 PM'")
departure_time_str = st.text_input(
    "Departure time", key="departure_time_text", value=_format_time_12h(now_local),
    label_visibility="collapsed"
)

# Accept compact times like "630pm" or "630 pm" by inserting the colon dateutil expects.
_compact_time_match = re.fullmatch(r'(\d{1,2})(\d{2})\s*([AaPp][Mm])', departure_time_str.strip())
if _compact_time_match:
    hour, minute, meridiem = _compact_time_match.groups()
    departure_time_str = f"{hour}:{minute} {meridiem}"

st.markdown("**Quick Preferences** (optional — combined with the notes below)")
qp_col1, qp_col2, qp_col3, qp_col4 = st.columns(4)
with qp_col1:
    want_veg = st.checkbox("🥗 Pure Veg")
with qp_col2:
    want_fuel = st.checkbox("⛽ Fuel Stop")
with qp_col3:
    want_restroom = st.checkbox("🚻 Restroom Break")
with qp_col4:
    want_snacks = st.checkbox("🍿 Snacks/Drinks")

preferences_notes = st.text_area(
    "Preferences / Notes",
    # Was "Traveling with elderly parents..." -- a specific caregiving scenario from early testing,
    # not a representative default. Road trip with friends pairs naturally with the Bengaluru ->
    # Ooty default route below, a genuinely popular weekend-getaway corridor for exactly that.
    "Road trip with friends — prefer vegetarian food and clean restrooms along the way"
)

_quick_prefs = []
if want_veg:
    _quick_prefs.append("Pure vegetarian food only.")
if want_fuel:
    _quick_prefs.append("Need a fuel/petrol stop along the way.")
if want_restroom:
    _quick_prefs.append("Need a restroom break stop.")
if want_snacks:
    _quick_prefs.append("Need to pick up snacks and drinks.")
preferences = (" ".join(_quick_prefs) + " " + preferences_notes).strip() if _quick_prefs else preferences_notes

try:
    departure_time_val = parser.parse(departure_time_str).time()
    departure_datetime = datetime.combine(departure_date, departure_time_val, tzinfo=origin_tz)
    # The "Now" default is only fresh at page load -- if the page has been open a while and the
    # picked date/time has quietly drifted into the past, treat it as "as soon as possible" instead
    # of sending an invalid past timestamp to the Routes API.
    if departure_datetime < now_local:
        departure_datetime = now_local + timedelta(minutes=1)
        st.caption(f"⏱️ That time has passed — using {_format_time_12h(departure_datetime.time())} instead.")
    departure_time_iso = departure_datetime.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
except Exception as e:
    st.error(f"Invalid time format: {e}")
    departure_time_iso = None

if st.button("Plan My Trip", width='stretch'):
    if not st.session_state.get("google_maps_api_key") or not st.session_state.get("gemini_api_key"):
        st.warning("This deployment's API keys aren't configured -- see the error above.")
    elif not departure_time_iso:
        st.warning("Please fix the departure time format.")
    else:
        st.session_state.planning_triggered = True
        st.session_state.origin = origin
        st.session_state.destination = destination
        st.session_state.departure_time_iso = departure_time_iso
        st.session_state.preferences = preferences
        # Starting a new plan resets any prior conversation and everything the UI derived from it
        # (map, photos, navigate links all key off discovered_places/route_polyline) -- without this,
        # stale places/route from a previous trip would linger into the new one's map and stop list.
        st.session_state.chat = None
        st.session_state.chat_messages = []
        st.session_state.discovered_places = {}
        st.session_state.route_stops_selected = []
        st.session_state.route_polyline = None
        st.session_state.latest_plan = None
        st.session_state.latest_plan_message_index = None
        st.session_state.need_new_plan = True

if st.session_state.get('planning_triggered', False):
    if not st.session_state.get('gemini_api_key'):
        st.error("GEMINI_API_KEY is not configured on the server.")
        st.stop()

    # Initialize the new Google GenAI Client
    client = genai.Client(api_key=st.session_state.gemini_api_key)

    # Function list for tools
    gemini_tools = [
        calculate_route_and_etas,
        search_places_along_route,
        get_place_details_and_reviews,
        types.Tool(google_search=types.GoogleSearch())
    ]

    # This prompt has been hardened against specific failure modes actually observed while building
    # this app, not written speculatively -- each numbered point below maps to a paragraph/bullet in
    # the text and is worth understanding before editing it:
    #   1. Early versions were framed entirely around "highway food/restroom pitstops," so the model
    #      defaulted every request (even "buy snacks for a friend visit") into a restaurant-shaped
    #      answer. The "don't default to restaurants" + "think like a concierge, not a search tool"
    #      framing exists specifically to break that bias -- see also search_places_along_route's
    #      required (no-default) `category` param above, which was the other half of that fix.
    #   2. When search_places_along_route failed, the model would quietly substitute restaurant names
    #      recalled from its own training data or a raw google_search instead of admitting the live
    #      search failed. Verified against live Places data: of 5 such recalled names, 3 were
    #      permanently closed and 1 was in the wrong city/state entirely -- i.e. this is a real,
    #      demonstrated risk for someone about to drive there with elderly parents, not a
    #      theoretical one. The explicit "do not fall back to naming places from your own knowledge"
    #      instruction exists specifically to prevent that.
    #   3. google_search is scoped narrowly (only to double-check a place search already found with
    #      few reviews) for the same reason -- without that scoping, the model would reach for it as
    #      a substitute discovery mechanism whenever the primary tool had a rough result.
    #   4. The proactive-timing paragraph (meal windows / biobreaks / late-night driving) was added
    #      because the app only reacted to what was explicitly asked for, missing the kind of
    #      forward-thinking a real concierge would offer unprompted (e.g. noting a very late arrival
    #      time, or that dinner-hour timing means suggesting food even if only fuel was requested).
    #   5. A second round of "think like an actual human planner, not a search tool" additions:
    #      applying stated constraints (veg, elderly, budget) across every stop category instead of
    #      just the one they were first mentioned for; surfacing price_level, which
    #      get_place_details_and_reviews already returns but nothing in the prompt ever told the model
    #      to use; actively looking to combine needs into fewer physical stops the way a person
    #      planning their own trip would, instead of always treating "food" and "fuel" as unrelated;
    #      generalizing the late-night-only fatigue note to any long drive, since tiredness doesn't
    #      wait for 10pm; and telling follow-up turns to build on what was already suggested instead
    #      of answering as if the conversation started fresh.
    #   6. The final answer is now forced into CONCIERGE_RESPONSE_SCHEMA (see above) instead of free
    #      Markdown -- two runs on identical input used to come back in visibly different layouts (a
    #      table one time, a numbered list the next), so the app couldn't render anything consistent.
    #      The prompt below now talks about populating JSON fields, not writing Markdown directly;
    #      the actual layout is generated by render_structured_response() from that JSON.
    system_instruction = (
        "You are a Thoughtful Indian Journey Concierge. Your goal is to plan an optimal trip for the user -- "
        "whether it's a long highway drive between cities or a short trip across town -- and help with "
        "whatever stops they actually need along the way — this can include "
        "food, restrooms, fuel, pharmacies, ATMs, or errands like picking up snacks, drinks, or groceries. "
        "You are not just a search tool stringing together API results -- think like an actual concierge who "
        "knows the route and anticipates needs before being asked. Consider the trip holistically, not just as "
        "a list of isolated stops: Is the arrival time reasonable for who's traveling -- would you personally "
        "flag a very late-night arrival with elderly parents, or suggest an earlier departure or an overnight "
        "stop for a very long drive? Are the stops you're suggesting sensibly spaced along the actual route "
        "and ordered the way you'd actually hit them while driving, not just listed in search-result order? "
        "For every kind of stop you recommend, present 2-3 of the real candidates side by side (fewer only if "
        "fewer genuinely good options exist) and let the user make the final call -- do not narrow it down to a "
        "single pick yourself. Give real, specific opinions on each option the way a knowledgeable local friend "
        "would -- e.g. 'X has the better thali but a smaller lot, Y is slower but has the most reliable parking, "
        "Z is the closest to your ETA if you're short on time' -- so the differences between them are actually "
        "useful for deciding, rather than a mechanical, uniform list or a single verdict. "
        "Read the user's request carefully and search for the specific kind of place that matches it "
        "(e.g. a request for snacks and drinks means a grocery/convenience/liquor store, not a restaurant). "
        "Do not default to restaurants unless the user is actually asking about a meal. "
        "Apply every stated constraint (dietary needs, elderly/accessibility considerations, budget) across "
        "the whole trip, not just the stop category it was first mentioned for -- e.g. if the user said pure "
        "veg for the trip, a snack/grocery stop should be veg-friendly too, not just the restaurant stops. "
        "Your final turn (once you're done calling tools) must be a JSON object matching the provided response "
        "schema, not free-form Markdown -- set response_type to 'plan' for a full trip plan, or 'answer' for a "
        "plain conversational reply to a follow-up that doesn't need the full itinerary structure (e.g. 'why did "
        "you suggest that one?' or a simple factual question). The schema controls the layout; your job is the "
        "words inside each field -- write them the same way you'd write a real answer: specific, opinionated, and "
        "in a friendly, conversational tone, like a knowledgeable local guide, not generic filler. "
        "Be extremely helpful and empathetic. "
        "Do not make up information. Only use the tools provided to gather information. "
        "If a tool call returns an error, do not retry it with guessed or reformatted inputs and do not invent "
        "place IDs or details — report the limitation to the user instead. "
        "This applies to search_places_along_route specifically: if it errors, do not fall back to naming "
        "specific restaurants/places from your own knowledge or a general google_search, even with a caveat -- "
        "unverified place names routinely turn out to be permanently closed, in the wrong city, or simply "
        "nonexistent, which is worse than no recommendation for someone actually about to drive there. Instead, "
        "tell the user the live place search hit a temporary issue and suggest they try again. "
        "google_search is only for double-checking a place get_place_details_and_reviews already returned with "
        "very few reviews, not for discovering new candidate places when search_places_along_route fails. "
        "When calculating ETAs, consider the 'departure_time_iso' for traffic. "
        "Always try to find multiple suitable options so you have real candidates to present as choices (see above). "
        "Call search_places_along_route exactly once per plan, passing every distinct kind of stop the trip needs "
        "together in its categories list (e.g. ['vegetarian restaurant', 'clean public restroom', 'petrol pump']) "
        "instead of calling it separately per category -- each call is a slow round trip through your own "
        "reasoning, so batching every category into one call is what keeps the plan fast. "
        "Call get_place_details_and_reviews exactly once, passing the place_ids of every candidate place "
        "you want details for together in one list, instead of calling it separately per place. "
        "Every option that came from a real tool result has a real place_id from that tool response -- always "
        "set the option's 'place_id' field to that exact value. It's used to build the 'get directions' link "
        "and the 'view on Google Maps' link for that exact business, so leaving it out or inventing one breaks "
        "those -- omit the field entirely only for an option that didn't come from a tool result at all. "
        "Fill 'itinerary_timeline' with departure and every stop's arrival time, and set 'toll_cost_text' in "
        "'trip_overview' if calculate_route_and_etas returned an estimate. "
        "Use emojis (🟢 Good, 🟡 Moderate, ⚠️ Red Flag) inside field text for quick-scan ratings where it helps. "
        "Provide actual ratings and review snippets in the relevant option fields. For every option, also set "
        "'review_recency' from get_place_details_and_reviews's most_recent_review.relative_time -- a 4.5-star "
        "rating built on reviews from years ago is a different, weaker signal than the same rating with reviews "
        "from last month, and the user can't tell the difference unless you say so. Also set "
        "'critical_review_snippet' from critical_review.text whenever get_place_details_and_reviews returned one "
        "for that place -- a genuinely balanced take shows the downside too, not just the best quote available; "
        "leave it null only when critical_review was actually null (nothing <=3 stars in what Google returned), "
        "never omit it just because it makes the option look worse. "
        "The following Trip Stop Rubric applies specifically when evaluating FOOD stops (skip it for "
        "non-food stops like shops, fuel, or pharmacies, and instead just note hours, ratings, and anything "
        "relevant from reviews): explicitly state if it's 'Pure Veg', 'Veg & Non-Veg', or 'Fast Food/Chains'; "
        "if traveling with elders, flag places with no traditional Indian meals (Roti/Dal/Thali) as ⚠️; "
        "verify the kitchen is open and serving the appropriate meal (Breakfast/Lunch/Snacks/Dinner) at the calculated ETA. "
        "For every option presented, mention parking availability using the 'parking_available' field from "
        "get_place_details_and_reviews, and its price level (using the 'price_level' field -- e.g. Budget/ "
        "Moderate/Expensive -- when available) since cost is part of a real comparison between options. "
        "Think proactively about the journey's timing using the duration and ETAs from calculate_route_and_etas, "
        "and volunteer relevant suggestions even when the user didn't explicitly ask for them -- put these in "
        "'proactive_notes' entries (separate from the options/categories the user actually asked for) so they "
        "don't crowd out what was actually asked: "
        "- Compare the departure time and ETAs against typical Indian meal windows (breakfast ~7-10am, lunch "
        "~12:30-3pm, dinner ~7:30-10:30pm). If the journey overlaps one, proactively suggest a food stop timed to "
        "that point even if the user only asked for something else like fuel or snacks -- people traveling around "
        "mealtimes usually want to eat too. "
        "- Include a query like 'clean public restroom' or 'rest area' in the categories you pass to "
        "search_places_along_route -- and include a distinct stop_categories entry (title like '🚻 Restroom "
        "Stops') with real results from it -- only when "
        "the total drive duration exceeds 2 hours, or when the user specifically asked for restrooms regardless "
        "of trip length. Don't run "
        "this search for short trips unless it was actually requested. When it does apply, don't consider a food "
        "stop's restroom sufficient on its own, since it may not land at a convenient point in the drive; for 4+ "
        "hour trips, look for more than one restroom option spaced through the journey rather than one near the start. "
        "- When the trip needs more than one kind of stop (e.g. food and fuel), check whether one location can "
        "reasonably cover more than one need (e.g. a fuel stop with an attached food court, or a restaurant near "
        "a pharmacy) before treating them as fully separate stops -- a real planner minimizes the number of "
        "physical stops where it doesn't compromise quality, and calls it out in a proactive_notes entry when it "
        "applies (e.g. 'X covers both your fuel and snack stop in one place'). "
        "- Independent of time of day, for any drive over about 3 hours, proactively suggest at least one short "
        "break purely for driver alertness (stretch, tea/coffee) roughly every 2-3 hours, even if the user didn't "
        "ask for a restroom or food stop -- fatigue risk doesn't wait for night to set in. "
        "- If departure or a significant part of the drive falls late at night (roughly 10pm-5am), additionally "
        "note that fewer places will be open and consider suggesting a tea/coffee stop for driver alertness. "
        "If a promising place has very few reviews (roughly under 10) and you're unsure it's reliable -- e.g. it "
        "might be new or low-quality -- use the google_search tool to check for other information about it (news, "
        "blog mentions, its own website) before recommending it, and say in the plan that you double-checked it "
        "this way since Google reviews were sparse. "
        "When answering a follow-up that asks for a change or a different option (e.g. 'suggest a different "
        "restaurant', 'what about something cheaper'), use response_type 'plan' again with the updated "
        "stop_categories/options, and build on what you already suggested instead of answering as if the "
        "conversation just started -- mention how the new answer differs from or improves on the earlier one "
        "(e.g. in intro_text or a verdict), and reuse place details you already have from this conversation if "
        "they satisfy the new ask rather than re-searching from scratch. For a purely conversational follow-up "
        "that isn't asking for a plan change (e.g. 'why did you suggest that one?'), use response_type 'answer' "
        "instead."
    )

    # Set up configuration with tools and system instruction
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=gemini_tools,
        # Required to mix our custom function-calling tools with the built-in Google Search tool --
        # omitting this gives a 400 telling you to set exactly this flag.
        tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
        # Gemini 3 models support combining tools with structured output: the model still calls
        # tools freely, but its final (non-tool-call) turn is forced into CONCIERGE_RESPONSE_SCHEMA
        # instead of whatever ad-hoc Markdown it feels like that run -- see the schema/rendering
        # comment above render_structured_response() for why. Documented as a preview capability as
        # of this writing; parse_structured_response() falls back to raw text if it's ever not
        # honored, so this degrades gracefully rather than breaking the app.
        response_mime_type="application/json",
        response_schema=CONCIERGE_RESPONSE_SCHEMA,
        # The SDK default is 10. A typical plan now needs ~3-6 calls (route + 1-2 category searches
        # + one batched details call), well under that -- this is headroom for multi-need requests
        # (e.g. food + fuel + restrooms all in one trip) plus the occasional google_search
        # verification, not a response to normal usage running out. If the AFC loop exhausts this
        # budget mid-plan, the model's last turn is left holding an unresolved function_call with no
        # text response, which surfaces in the UI as a literal "None" -- if that recurs, the fix is
        # more budget here or fewer categories per plan, not a UI-side workaround.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=15
        )
    )

    # Reuse the same chat session across reruns so follow-up questions share context.
    if st.session_state.get('chat') is None:
        st.session_state.chat = client.chats.create(
            model='gemini-3.6-flash',
            config=config
        )
    chat = st.session_state.chat

    if 'chat_messages' not in st.session_state:
        st.session_state.chat_messages = []

    if st.session_state.get('need_new_plan', False):
        st.session_state.need_new_plan = False
        prompt = (
            f"Plan a trip from {st.session_state.origin} to {st.session_state.destination}. "
            f"My departure time is {st.session_state.departure_time_iso}. "
            f"Here are my preferences/notes: {st.session_state.preferences}. "
            "First, calculate the route and ETAs. Next, based on what I actually need (see my preferences/notes "
            "above), search along the route polyline for the appropriate kind of stop -- this might be food, "
            "restrooms, fuel, or an errand like buying snacks/drinks/groceries; don't assume it's a restaurant "
            "unless my notes actually ask for one. Finally, fetch place details/reviews for the best options and "
            "evaluate them appropriately for what I asked for."
        )
        status = st.status("Planning your trip and evaluating live stops...", expanded=True)
        st.session_state._progress_status = status
        st.session_state._tool_trace = []
        st.session_state._places_api_stats = {}
        st.session_state._routes_api_stats = {}
        start = time.monotonic()
        response = chat.send_message(prompt)
        duration_s = time.monotonic() - start
        status.update(label=f"✅ Plan ready in {duration_s:.1f}s", state="complete")
        content, response_meta = response_to_markdown(response.text)
        log_usage_event("plan", st.session_state.origin, st.session_state.destination,
                         st.session_state.preferences, duration_s, st.session_state._tool_trace, response_meta,
                         st.session_state._places_api_stats, st.session_state._routes_api_stats)
        st.session_state._progress_status = None
        st.session_state.chat_messages.append({"role": "assistant", "content": content})
        if response_meta.get("structured_ok") and response_meta.get("response_type") == "plan":
            st.session_state.latest_plan_message_index = len(st.session_state.chat_messages) - 1

    st.subheader("Your Personalized Journey Plan")
    for i, message in enumerate(st.session_state.chat_messages):
        with st.chat_message(message["role"]):
            is_latest_plan = (
                i == st.session_state.get("latest_plan_message_index")
                and st.session_state.get("latest_plan")
            )
            if is_latest_plan:
                render_plan_cards(st.session_state.latest_plan)
            else:
                st.markdown(message["content"])
                if message["role"] == "assistant":
                    render_copy_and_share(message["content"])

    render_route_map()
    render_place_photos()
    render_navigate_links()
    render_region_postcards()

    followup = st.chat_input("Ask a follow-up — e.g. 'suggest a different restaurant' or 'what about the return trip?'")
    if followup:
        st.session_state.chat_messages.append({"role": "user", "content": followup})
        status = st.status("Thinking...", expanded=True)
        st.session_state._progress_status = status
        st.session_state._tool_trace = []
        st.session_state._places_api_stats = {}
        st.session_state._routes_api_stats = {}
        start = time.monotonic()
        response = chat.send_message(followup)
        duration_s = time.monotonic() - start
        status.update(label=f"✅ Answered in {duration_s:.1f}s", state="complete")
        content, response_meta = response_to_markdown(response.text)
        log_usage_event("followup", st.session_state.origin, st.session_state.destination,
                         followup, duration_s, st.session_state._tool_trace, response_meta,
                         st.session_state._places_api_stats, st.session_state._routes_api_stats)
        st.session_state._progress_status = None
        st.session_state.chat_messages.append({"role": "assistant", "content": content})
        if response_meta.get("structured_ok") and response_meta.get("response_type") == "plan":
            st.session_state.latest_plan_message_index = len(st.session_state.chat_messages) - 1
        st.rerun()

# Always at the very end of the page, regardless of whether a trip has been planned yet -- someone
# might want to say "the search box is confusing" without ever getting as far as a plan.
st.divider()
st.subheader("💬 Feedback")
st.caption("Tell us what worked, what didn't, or what you wish this did.")
if st.session_state.get("_feedback_submitted"):
    st.success("Thanks for the feedback!")
    st.session_state._feedback_submitted = False
    # Must happen before the widgets below are instantiated this run -- writing to a widget's key
    # after it already exists raises (same rule as the date/time quick-select buttons elsewhere).
    st.session_state.feedback_comment = ""
    st.session_state.feedback_rating = None
feedback_rating = st.feedback("thumbs", key="feedback_rating")
feedback_comment = st.text_area(
    "Comments (optional)", key="feedback_comment",
    placeholder="e.g. the restroom suggestions were spot on, but I wish I could filter by price...",
)
if st.button("Submit Feedback"):
    log_feedback(feedback_rating, feedback_comment)
    st.session_state._feedback_submitted = True
    st.rerun()
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
from dataclasses import dataclass, field
import base64
import csv
import functools
import json
import logging
import math
import os
import re
import threading
import time
import uuid
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
    "places_cache_fresh", "places_cache_verified", "places_cache_drift",
    "error_detail",
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
    didn't run, e.g. a follow-up that didn't need a fresh lookup). places_api_stats also carries
    'cache_fresh'/'cache_verified'/'cache_verify_failed' from get_place_details_and_reviews's
    place-details cache -- this is what makes the cache's actual payoff (how often a place lookup
    was free or half-price instead of a full paid call) a real, trackable number instead of an
    assumed win. response_meta may also carry 'error_detail' (a short f"{type}: {message}" string)
    when chat.send_message itself failed -- this is what lets a failure be diagnosed straight from
    the Sheet (e.g. "ServerError: 503 UNAVAILABLE...") instead of requiring someone to manually
    fetch and read Streamlit Cloud's own ephemeral server logs, which is otherwise the only place
    that detail exists.

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
    error_detail = response_meta.get("error_detail", "")
    logger.info(
        "usage event=%s origin=%r destination=%r duration_s=%.2f tool_calls=%d tool_errors=%d "
        "places_api_new=%d places_api_legacy=%d places_api_failed=%d "
        "routes_api_new=%d routes_api_legacy=%d routes_api_failed=%d "
        "places_cache_fresh=%d places_cache_verified=%d places_cache_drift=%d "
        "structured_ok=%s response_type=%s error_detail=%r [%s]",
        event_type, origin, destination, duration_s, len(tool_trace), tool_errors,
        places_api_stats.get('new', 0), places_api_stats.get('legacy', 0), places_api_stats.get('failed', 0),
        routes_api_stats.get('new', 0), routes_api_stats.get('legacy', 0), routes_api_stats.get('failed', 0),
        places_api_stats.get('cache_fresh', 0), places_api_stats.get('cache_verified', 0),
        places_api_stats.get('cache_verify_failed', 0),
        response_meta.get("structured_ok"), response_meta.get("response_type"), error_detail, tool_summary,
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
        places_api_stats.get('cache_fresh', 0),
        places_api_stats.get('cache_verified', 0),
        places_api_stats.get('cache_verify_failed', 0),
        error_detail,
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

# --- Background plan jobs (survive the requesting browser tab disconnecting) ---
#
# Streamlit deliberately stops a session's own script execution when its WebSocket disconnects
# (a documented, intentional behavior in the Streamlit codebase itself, not a bug) -- confirmed
# directly against this app's real failure: a user navigated away mid-plan, and by the time they
# came back the request had vanished with no trace, not even a usage-log row, because the whole
# script (including the code that would have logged it) had been torn down along with the socket.
# Plans here routinely take 40 seconds to several minutes -- long enough that navigating away
# during the wait is completely normal user behavior, not an edge case.
#
# The fix: the actual chat.send_message() call and everything downstream of it now runs on a
# process-wide (not per-session) ThreadPoolExecutor, obtained via @st.cache_resource with its
# default *global* scope -- global-scoped cache_resource objects are shared across every session
# and are NOT torn down when one session disconnects (only session-scoped resources are); only a
# ThreadPoolExecutor, not a ProcessPoolExecutor, is supported inside Streamlit. A disconnect now
# only interrupts the lightweight polling loop that checks "is my job done yet", never the actual
# work, which keeps running regardless of whether anyone is still connected to watch it. The job's
# id is stashed in the URL (st.query_params), so any browser tab that loads with that id -- the
# same one reconnecting, a different tab, even after the whole page was closed and reopened --
# finds the same job and either sees live progress or the finished result.
#
# A background worker thread has no ScriptRunContext, so it cannot touch st.session_state at all
# (the same hard rule already established for the ThreadPoolExecutor fan-outs inside the tool
# functions themselves, just now applying to the *outer* call too). Everything a plan needs to
# read or write while running -- the API key, the route polyline, discovered places, live progress
# lines -- goes through the PlanJob object below instead, reached from inside a tool function via
# a thread-local (so the tool functions' own signatures don't change -- the genai SDK introspects
# those signatures directly to build Gemini's function-calling schema, so adding a "job" parameter
# to them would break that). Once a job finishes, its data is copied into st.session_state exactly
# once by the (main-thread, session-state-safe) polling code below, and every existing render
# function keeps working completely unmodified from that point on.


@dataclass
class PlanJob:
    """Everything one plan (or follow-up) request needs while running in the background, and the
    result once it's done. Deliberately mirrors the shape of what used to live directly in
    st.session_state -- this is that same data, just addressed through a job object a background
    thread can safely touch instead of through session state, which it can't."""
    status: str = "running"  # "running" | "done" | "error"
    event_type: str = "plan"  # "plan" | "followup" -- for usage-log event_type
    origin: str = ""
    destination: str = ""
    preferences: str = ""
    user_message: str = ""  # the human-visible chat bubble, e.g. a follow-up question; blank for
                             # an initial plan, which never showed one even before background jobs
    started_at: float = field(default_factory=time.monotonic)
    progress: list = field(default_factory=list)
    tool_trace: list = field(default_factory=list)
    places_api_stats: dict = field(default_factory=dict)
    routes_api_stats: dict = field(default_factory=dict)
    route_polyline: str | None = None
    route_waypoints: list = field(default_factory=list)
    discovered_places: dict = field(default_factory=dict)
    chat: object = None  # the genai chat session -- kept so a follow-up can continue it
    content: str | None = None
    response_meta: dict = field(default_factory=dict)
    # response_to_markdown's side effect of stashing a parsed plan for render_plan_cards used to
    # write st.session_state.latest_plan directly -- unsafe now that it can run on a worker thread
    # with no ScriptRunContext, so it lands here instead and the merge step below copies it over.
    latest_plan: dict | None = None


# Thread-local, not a plain global -- multiple plan jobs can genuinely run concurrently (different
# users, or a user firing off a follow-up while another tab of theirs is still polling an earlier
# one), and each worker thread must only ever see its own job.
_job_local = threading.local()


def _current_job() -> "PlanJob | None":
    return getattr(_job_local, "job", None)


@st.cache_resource(show_spinner=False)
def _get_job_executor() -> ThreadPoolExecutor:
    """Process-wide thread pool for running plan jobs -- see the module comment above for why this
    specifically needs to be a global-scoped (the @st.cache_resource default) resource, not
    something created per-session."""
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="plan_job")


@st.cache_resource(show_spinner=False)
def _get_job_registry() -> dict:
    """Process-wide {'lock': threading.Lock(), 'jobs': {job_id: PlanJob}}. The lock guards against
    the rare but real race of a worker thread finishing a job at the same moment a browser tab's
    polling code reads it."""
    return {"lock": threading.Lock(), "jobs": {}}


def _maps_api_key() -> str:
    """Server-side key, identical for every user/session -- reading it directly from the
    environment (instead of st.session_state, which tool functions can no longer safely touch once
    they're running inside a background job thread) sidesteps the whole session-state-thread-safety
    question for something that was never actually per-session data in the first place."""
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")

def _api_stats(job_attr: str, session_key: str) -> dict:
    """The mutable {'new': n, 'legacy': n, 'failed': n} tally for the current job when running in a
    background job thread (can't touch st.session_state there), else the same tally on
    st.session_state directly -- same dual-path fallback as everywhere else in this block."""
    job = _current_job()
    if job is not None:
        stats = getattr(job, job_attr)
        if not stats:
            stats.update({'new': 0, 'legacy': 0, 'failed': 0})
        return stats
    return st.session_state.setdefault(session_key, {'new': 0, 'legacy': 0, 'failed': 0})

def _route_polyline() -> str | None:
    """Reads the encoded route polyline calculate_route_and_etas stashed earlier this same request
    -- from the current job when running in a background job thread (both calculate_route_and_etas
    and search_places_along_route run as the same job, just possibly different calls into it), else
    st.session_state directly."""
    job = _current_job()
    if job is not None:
        return job.route_polyline
    return st.session_state.get('route_polyline')

def _route_waypoints() -> list:
    """Same dual-path fallback as _route_polyline, for the traffic-timed waypoints."""
    job = _current_job()
    if job is not None:
        return job.route_waypoints
    return st.session_state.get('route_waypoints') or []

def _discovered_places() -> dict:
    """The mutable discovered-places dict for the current job when running in a background job
    thread, else st.session_state's copy -- same dual-path fallback pattern."""
    job = _current_job()
    if job is not None:
        return job.discovered_places
    if 'discovered_places' not in st.session_state:
        st.session_state.discovered_places = {}
    return st.session_state.discovered_places


@st.cache_resource(show_spinner=False)
def _get_plan_results_worksheet():
    """Durable backstop for a finished job's result, in case the whole server process restarts
    (a redeploy, a crash) between when a job finishes and when its browser tab reconnects --
    @st.cache_resource's in-memory job registry above doesn't survive that, only a real restart of
    a single session does. Returns None (the in-memory registry becomes the only source of truth,
    same as before this existed) when Sheets credentials aren't configured or the Sheet can't be
    reached."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON")
    sheet_id = os.environ.get("USAGE_SHEET_ID")
    if not creds_json or not sheet_id:
        return None
    try:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(creds_json))
        spreadsheet = gc.open_by_key(sheet_id)
        try:
            sheet = spreadsheet.worksheet("plan_results")
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title="plan_results", rows=2000, cols=8)
            sheet.append_row(["job_id", "timestamp_utc", "version", "origin", "destination",
                               "status", "content", "response_meta_json", "extra_json"])
        return sheet
    except Exception:
        logger.exception("failed to connect to the plan_results worksheet")
        return None


def _persist_job_result(job_id: str, job: "PlanJob"):
    """Best-effort durable copy of a finished job -- never allowed to raise into the worker thread
    that calls it, since a failure here should never be confused with the plan itself failing."""
    sheet = _get_plan_results_worksheet()
    if sheet is None:
        return
    try:
        sheet.append_row([
            job_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            APP_VERSION,
            job.origin,
            job.destination,
            job.status,
            job.content or "",
            json.dumps(job.response_meta or {}, ensure_ascii=False),
            json.dumps(
                {
                    "route_polyline": job.route_polyline,
                    "discovered_places": job.discovered_places,
                    "latest_plan": job.latest_plan,
                    "user_message": job.user_message,
                },
                ensure_ascii=False,
            ),
        ])
    except Exception:
        logger.exception(f"failed to persist plan_results row for job {job_id}")


def _load_job_result_from_sheet(job_id: str) -> "PlanJob | None":
    """Reconstructs a minimal, read-only PlanJob from the durable backstop -- used only when a job
    id from the URL isn't in the in-memory registry (the server process restarted since it
    finished). No 'chat' object survives this path, since a genai chat session isn't serializable
    -- a restored plan can be viewed, but a follow-up on it starts a fresh conversation rather than
    continuing the old one. Returns None if the Sheet isn't configured, unreachable, or has no
    matching row."""
    sheet = _get_plan_results_worksheet()
    if sheet is None:
        return None
    try:
        rows = sheet.get_all_records()
    except Exception:
        logger.exception("failed to read the plan_results worksheet")
        return None
    for row in reversed(rows):
        if str(row.get("job_id")) != job_id:
            continue
        try:
            extra = json.loads(row.get("extra_json") or "{}")
            response_meta = json.loads(row.get("response_meta_json") or "{}")
        except json.JSONDecodeError:
            extra, response_meta = {}, {}
        job = PlanJob(
            status=row.get("status") or "done",
            origin=row.get("origin", ""),
            destination=row.get("destination", ""),
            content=row.get("content") or None,
            response_meta=response_meta,
            route_polyline=extra.get("route_polyline"),
            discovered_places=extra.get("discovered_places") or {},
            latest_plan=extra.get("latest_plan"),
            user_message=extra.get("user_message") or "",
        )
        return job
    return None


def _run_plan_job(job_id: str, prompt: str, existing_chat):
    """Runs entirely on a background worker thread (submitted via _get_job_executor) -- independent
    of whatever browser session requested it, per the module comment above. existing_chat is always
    the caller's already-created genai chat session (created eagerly, main-thread-only, before any
    job is ever submitted -- see the "Reuse the same chat session" comment in the UI code below), so
    a plan's first request and every follow-up after it share one real conversation."""
    registry = _get_job_registry()
    with registry["lock"]:
        job = registry["jobs"][job_id]
    _job_local.job = job
    try:
        # Stashed on the job, not just left as the caller's own reference -- a reconnect wipes
        # st.session_state entirely, so this copy is the only way a later poll can keep the same
        # conversation going rather than losing it.
        job.chat = existing_chat
        response = existing_chat.send_message(prompt)
        content, response_meta = response_to_markdown(response.text)
        with registry["lock"]:
            job.content = content
            job.response_meta = response_meta
            job.status = "done"
    except Exception as exc:
        # Deliberately broad -- see the matching comment that used to sit at the old inline call
        # site (git history / PROCESS.md) for why: this is the single outermost boundary of the
        # whole planning request, so anything that reaches here should degrade to a clear error
        # state rather than silently killing the worker thread with nothing recorded anywhere.
        with registry["lock"]:
            job.response_meta = {
                "structured_ok": False, "response_type": "error",
                "error_detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
            job.status = "error"
        logger.exception(f"plan job {job_id} failed")
    finally:
        duration_s = time.monotonic() - job.started_at
        log_usage_event(job.event_type, job.origin, job.destination, job.preferences, duration_s,
                         job.tool_trace, job.response_meta or {}, job.places_api_stats, job.routes_api_stats)
        _persist_job_result(job_id, job)
        _job_local.job = None


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
        line = f"{_TOOL_LABELS.get(func.__name__, func.__name__)}{detail}..."
        # Tool functions now always run inside a background job's worker thread (see the module
        # comment above PlanJob), which has no ScriptRunContext and so cannot touch
        # st.session_state -- _current_job() is the thread-local reached from there. The
        # session_state branch is kept as a harmless fallback, not the expected path anymore.
        job = _current_job()
        if job is not None:
            job.progress.append(line)
        else:
            status = st.session_state.get("_progress_status")
            if status is not None:
                status.write(line)

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
            entry = {"name": func.__name__, "detail": detail, "duration_s": duration_s, "ok": ok}
            if job is not None:
                job.tool_trace.append(entry)
            else:
                trace = st.session_state.get("_tool_trace")
                if trace is not None:
                    trace.append(entry)
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


# Curated toll-plaza database for corridors this app has actually been tested on -- NOT exhaustive
# nationwide coverage. Built after Google's Routes API TOLLS extraComputation was verified (against
# NHAI-sourced third-party toll calculators) to overestimate by ~2.7x on a real route: Hyderabad<->
# Bengaluru came back Rs 1950 from Google, Rs 715 from NHAI-sourced calculators, cross-checked
# against two independent sources. Data below is sourced the same way (NHAI-backed calculators),
# then geocoded via Places API to real coordinates so plazas can be matched against a route's
# actual polyline instead of trusted from Google's own (demonstrably unreliable, for Indian tolls)
# estimate.
#
# Coverage is intentionally bounded, not automated -- an unmaintained "cover everything" table goes
# stale exactly as fast as a small one, just less visibly. REVISE THIS TABLE EVERY 6 MONTHS (next
# due ~2027-02) -- NHAI toll rates change periodically (WPI-linked revisions); data below gathered
# 2026-08. Routes outside this table's coverage fall back to a live lookup, then to Google's
# estimate with an explicit "unverified" caveat -- see _toll_for_route below.
TOLL_PLAZAS = [
    # NH44, Hyderabad <-> Bengaluru corridor
    {"name": "Marur", "lat": 14.5020692, "lng": 77.6312836, "car_inr": 145, "highway": "NH44"},
    {"name": "Kasepalli", "lat": 15.0617571, "lng": 77.6303444, "car_inr": 130, "highway": "NH44"},
    {"name": "Amakathadu", "lat": 15.4865638, "lng": 77.9011596, "car_inr": 140, "highway": "NH44"},
    {"name": "Pullur", "lat": 15.88775, "lng": 78.0169762, "car_inr": 145, "highway": "NH44"},
    {"name": "Shakapur", "lat": 16.5356813, "lng": 77.944372, "car_inr": 75, "highway": "NH44"},
    {"name": "Raikal", "lat": 17.0059277, "lng": 78.194337, "car_inr": 80, "highway": "NH44"},
    # NH44, Hyderabad -> Nagpur corridor
    {"name": "Manoharabad", "lat": 17.8010886, "lng": 78.471757, "car_inr": 90, "highway": "NH44"},
    {"name": "Indalwai", "lat": 18.5383087, "lng": 78.2396172, "car_inr": 85, "highway": "NH44"},
    {"name": "Pippalwada", "lat": 19.7809141, "lng": 78.5657746, "car_inr": 95, "highway": "NH44"},
    {"name": "Kelapur", "lat": 20.0193237, "lng": 78.5402198, "car_inr": 105, "highway": "NH44"},
    {"name": "Daroda", "lat": 20.4506267, "lng": 78.7552876, "car_inr": 115, "highway": "NH44"},
    {"name": "Borkhedi (Nagpur bypass)", "lat": 20.8560864, "lng": 78.9636019, "car_inr": 150, "highway": "NH44"},
    # NH275, Bengaluru <-> Mysuru/Ooty corridor
    {"name": "Kaniminike (Bidadi)", "lat": 12.8595709, "lng": 77.4311079, "car_inr": 165, "highway": "NH275"},
    {"name": "KN Hundy", "lat": 12.2130339, "lng": 76.6629785, "car_inr": 55, "highway": "NH275"},
]

# Chains recognized to hold reasonably consistent standards across locations in India -- used only
# as a soft confidence signal folded into the model's verdict wording (e.g. "a recognized chain,
# consistent standards across locations"). Deliberately NOT a substitute for real per-place data --
# this must never be used to claim or imply a specific fact (restroom, hours, parking) that isn't
# separately confirmed by get_place_details_and_reviews. That's the exact mistake this app is meant
# to avoid (see the restroom_available fix above): a place-level claim needs place-level evidence,
# brand reputation alone isn't it.
RECOGNIZED_CHAINS = [
    # Pure veg / multi-cuisine restaurant chains
    "Kailash Parbat", "Saravana Bhavan", "Adyar Ananda Bhavan", "A2B",
    "Haldiram's", "Haldiram", "Sagar Ratna",
    # Cafes
    "Chaayos", "Third Wave Coffee", "Cafe Coffee Day", "Café Coffee Day", "Starbucks",
    # Fuel stations -- deliberately limited to the two brands actually asked for, not every PSU/
    # private operator, since brand alone here is standing in for "generally well-maintained
    # facilities," which doesn't hold evenly across every fuel brand in India.
    "Jio-bp", "Jio BP", "Shell",
    # Highway wayside amenity operators -- purpose-built rest stops combining fuel, multi-cuisine
    # food, and restrooms under one roof, explicitly built around consistent hygiene standards
    # (unlike a single independent roadside dhaba/pump, which is exactly the kind of place this
    # list should NOT cover on brand alone).
    "Big Bay", "Cube Stop", "PATH Recharge", "Highway Star",
]

# India's emergency numbers -- real, nationwide constants, not something to look up per trip or
# trust the model to remember to mention. 112 is the unified ERSS number (police/fire/ambulance/
# other services in one line, confirmed live via web search, operational in every state/UT since
# 2019); 100/101/108 are the older separate lines some people still know by heart. Rendered directly
# by code (render_plan_cards below), not routed through the model or the response schema at all --
# static data has no business depending on a language model to reproduce it correctly every time.
EMERGENCY_NUMBERS = [
    ("112", "All-in-one emergency number (police, fire, ambulance) -- works everywhere in India"),
    ("100", "Police"),
    ("101", "Fire"),
    ("108", "Ambulance"),
]


def _match_recognized_chain(place_name: str) -> str | None:
    """Returns the matched brand name from RECOGNIZED_CHAINS if place_name contains one (case
    -insensitive substring match), else None. A plain name match, not a claim about that specific
    location -- see the caveat on RECOGNIZED_CHAINS above."""
    name_lower = place_name.lower()
    for brand in RECOGNIZED_CHAINS:
        if brand.lower() in name_lower:
            return brand
    return None


@st.cache_resource(show_spinner=False)
def _get_toll_plazas_worksheet():
    """Opens (creating if needed) a 'toll_plazas' tab inside the shared usage-log spreadsheet --
    reuses that spreadsheet rather than asking for a whole new one, since the same service account
    already has access to it. Persists plazas discovered by _toll_from_live_lookup below, so a
    route researched once is reused on every later request for the same corridor instead of being
    re-scraped and re-geocoded from scratch each time -- this is what lets TOLL_PLAZAS's effective
    coverage grow over time without a code change/redeploy. Returns None (falls back to running the
    live lookup fresh every time, never persisting) when credentials/USAGE_SHEET_ID aren't set or
    the Sheet can't be reached."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON")
    sheet_id = os.environ.get("USAGE_SHEET_ID")
    if not creds_json or not sheet_id:
        return None
    try:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(creds_json))
        spreadsheet = gc.open_by_key(sheet_id)
        try:
            sheet = spreadsheet.worksheet("toll_plazas")
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title="toll_plazas", rows=200, cols=6)
            sheet.append_row(["name", "lat", "lng", "car_inr", "highway", "discovered_at"])
        return sheet
    except Exception:
        logger.exception("failed to connect to the toll_plazas worksheet")
        return None


@st.cache_resource(show_spinner=False)
def _get_learned_toll_plazas() -> list[dict]:
    """Toll plazas discovered by past live lookups (see _toll_from_live_lookup), read once per
    server process. Additive to TOLL_PLAZAS, never load-bearing: returns [] if the Sheet isn't
    configured, can't be reached, or has a row that doesn't parse -- the live lookup just runs
    again for that route next time rather than the app breaking."""
    sheet = _get_toll_plazas_worksheet()
    if sheet is None:
        return []
    try:
        learned = []
        for row in sheet.get_all_records():
            try:
                learned.append({
                    "name": row["name"], "lat": float(row["lat"]), "lng": float(row["lng"]),
                    "car_inr": int(row["car_inr"]), "highway": row.get("highway", "unknown"),
                })
            except (KeyError, ValueError):
                continue
        return learned
    except Exception:
        logger.exception("failed to load learned toll plazas from the Google Sheet")
        return []


def _persist_learned_plazas(new_plazas: list[dict]):
    """Appends newly live-looked-up plazas to the toll_plazas Sheet, skipping any name already
    there. Best-effort: a failure here just means this route gets re-looked-up next time, not a
    broken plan -- never let this raise into the caller."""
    sheet = _get_toll_plazas_worksheet()
    if sheet is None or not new_plazas:
        return
    try:
        existing_names = {row.get("name") for row in sheet.get_all_records()}
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for plaza in new_plazas:
            if plaza["name"] in existing_names:
                continue
            sheet.append_row([plaza["name"], plaza["lat"], plaza["lng"], plaza["car_inr"],
                               plaza.get("highway", "unknown"), timestamp])
    except Exception:
        logger.exception("failed to persist learned toll plazas")


# get_place_details_and_reviews's shared cache, in the same spreadsheet as the toll-plaza data
# above -- reuses the same infra rather than standing up separate storage. Freshness policy:
#   - under 30 days old: reuse the cached result outright, no API call at all.
#   - 30-90 days old: one cheap recheck call (businessStatus + rating only, Pro/Enterprise-tier
#     pricing -- roughly half the cost of the full Enterprise+Atmosphere details lookup) to see if
#     anything looks different; only pay for a full refresh if it does.
#   - over 90 days old, or never cached: full fetch, same as today, then cached for next time.
# This is what makes "another user asks about the same place a week later" fast and cheap instead
# of re-paying for the same $40/1K lookup -- place_id is a stable, real identity to key on, unlike
# search_places_along_route's break points, which shift with every route.
_PLACE_CACHE_FRESH_DAYS = 30
_PLACE_CACHE_VERIFY_DAYS = 90
_PLACE_CACHE_RATING_DRIFT_THRESHOLD = 0.3  # a rating move bigger than this is treated as "changed"


@st.cache_resource(show_spinner=False)
def _get_place_cache_worksheet():
    """Opens (creating if needed) a 'place_details_cache' tab in the shared usage-log spreadsheet.
    Returns None (falls back to fetching fresh every time, never caching) when credentials/
    USAGE_SHEET_ID aren't set or the Sheet can't be reached -- same fallback philosophy as the toll
    plaza cache above."""
    creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON")
    sheet_id = os.environ.get("USAGE_SHEET_ID")
    if not creds_json or not sheet_id:
        return None
    try:
        import gspread
        gc = gspread.service_account_from_dict(json.loads(creds_json))
        spreadsheet = gc.open_by_key(sheet_id)
        try:
            sheet = spreadsheet.worksheet("place_details_cache")
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title="place_details_cache", rows=2000, cols=5)
            sheet.append_row(["place_id", "name", "cached_at", "rating_at_cache", "details_json"])
        return sheet
    except Exception:
        logger.exception("failed to connect to the place_details_cache worksheet")
        return None


def _load_place_details_cache() -> dict:
    """Reads the whole place_details_cache sheet fresh on every call (deliberately NOT
    @st.cache_resource, unlike the toll plaza reads above) -- unlike toll plazas, which are a
    slow-changing supplementary table, this cache's whole value is reflecting writes from other
    users' recent requests, not just whatever this server process saw since it last restarted.
    Returns {} on any failure or if the Sheet isn't configured -- every caller already treats a
    cache miss as "just fetch it," so an empty cache degrades to today's always-fetch behavior."""
    sheet = _get_place_cache_worksheet()
    if sheet is None:
        return {}
    try:
        cache = {}
        all_values = sheet.get_all_values()
        for row_index, row in enumerate(all_values[1:], start=2):  # row 1 is the header
            if len(row) < 5:
                continue
            place_id, name, cached_at_str, rating_str, details_json = row[:5]
            try:
                cached_at = datetime.fromisoformat(cached_at_str)
                details = json.loads(details_json)
            except (ValueError, json.JSONDecodeError):
                continue
            rating_at_cache = float(rating_str) if rating_str else None
            cache[place_id] = {
                "row": row_index, "name": name, "cached_at": cached_at,
                "rating_at_cache": rating_at_cache, "details": details,
            }
        return cache
    except Exception:
        logger.exception("failed to load the place_details_cache Sheet")
        return {}


def _persist_place_details_cache(cache: dict, entries: list[dict]):
    """Upserts entries (each {'place_id', 'name', 'cached_at' (datetime), 'rating_at_cache',
    'details'}) into the place_details_cache Sheet -- updates the existing row if 'cache' (from
    _load_place_details_cache) already had one for that place_id, else appends a new row.
    Best-effort: a failure here just means this place gets fetched fresh again next time, not a
    broken plan -- never let this raise into the caller."""
    sheet = _get_place_cache_worksheet()
    if sheet is None or not entries:
        return
    try:
        for entry in entries:
            row_values = [
                entry["place_id"], entry.get("name", ""),
                entry["cached_at"].isoformat(timespec="seconds"),
                entry["rating_at_cache"] if entry.get("rating_at_cache") is not None else "",
                json.dumps(entry["details"], ensure_ascii=False),
            ]
            existing = cache.get(entry["place_id"])
            if existing:
                sheet.update(f"A{existing['row']}:E{existing['row']}", [row_values])
            else:
                sheet.append_row(row_values)
    except Exception:
        logger.exception("failed to persist the place_details_cache Sheet")


# tolltax.in uses older/common city names in its URLs, not always what Google's formatted address
# returns (e.g. "bangalore", not "bengaluru") -- extend this as more mismatches are found.
_TOLL_CITY_SLUG_ALIASES = {"bengaluru": "bangalore"}


def _city_slug(address: str) -> str:
    city = address.split(",")[0].strip().lower()
    city = _TOLL_CITY_SLUG_ALIASES.get(city, city)
    return re.sub(r"[^a-z0-9]+", "-", city).strip("-")


def _toll_from_live_lookup(origin: str, destination: str, encoded_polyline: str,
                            path: list[tuple[float, float]]) -> dict | None:
    """Best-effort live toll lookup for a route not yet in TOLL_PLAZAS/_get_learned_toll_plazas,
    scraping tolltax.in's NHAI-sourced per-route calculator (predictable URL:
    {origin-city}-to-{destination-city}-toll-tax.php). Every plaza it names is geocoded with an
    along-route Places search (the same searchAlongRouteParameters mechanism
    search_places_along_route uses) rather than a blind global text search -- a plain "{name} toll
    plaza India" text search was observed matching a same-named business nowhere near the actual
    route (an auto-repair shop 18km off-route, for a real plaza name); constraining the search to
    the route corridor itself fixes that at the source, and the polyline distance check below (5km)
    stays on as defense in depth rather than the only safeguard.

    Returns None on any failure (site down or restructured, no plazas found, nothing survives the
    checks) -- this is a nice-to-have enhancement layered on top of the reliable static table, never
    allowed to block route calculation beyond its own short timeout, and never allowed to raise into
    the caller.
    """
    try:
        url = f"https://www.tolltax.in/charges/{_city_slug(origin)}-to-{_city_slug(destination)}-toll-tax.php"
        response = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if not response.ok:
            return None
        rows = re.findall(r"title='See all toll rates of ([^']+)'>[^<]*</td>\s*<td>(\d+)</td>", response.text)
        candidates = [(name, int(rate)) for name, rate in rows if int(rate) > 0]
        if not candidates:
            return None

        api_key = _maps_api_key()
        if not api_key:
            return None
        geocode_headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName.text,places.location",
        }

        found = []
        for name, rate in candidates:
            try:
                gr = requests.post(
                    "https://places.googleapis.com/v1/places:searchText",
                    headers=geocode_headers,
                    json={
                        "textQuery": f"{name} toll",
                        "searchAlongRouteParameters": {"polyline": {"encodedPolyline": encoded_polyline}},
                        "pageSize": 1,
                        "languageCode": "en-US",
                    },
                    timeout=5,
                )
            except requests.RequestException:
                continue
            if not gr.ok:
                continue
            places = gr.json().get("places", [])
            if not places:
                continue
            loc = places[0].get("location", {})
            lat, lng = loc.get("latitude"), loc.get("longitude")
            if lat is None or lng is None:
                continue
            if min(_haversine_km(plat, plng, lat, lng) for plat, plng in path) > 5.0:
                continue
            found.append({"name": name, "lat": lat, "lng": lng, "car_inr": rate, "highway": "unknown"})

        if not found:
            return None
        return {"total_inr": sum(p["car_inr"] for p in found), "plazas": [p["name"] for p in found],
                "new_plazas": found}
    except Exception:
        logger.exception("live toll lookup failed for %r -> %r", origin, destination)
        return None


def _toll_for_route(path: list[tuple[float, float]]) -> dict | None:
    """Sums known toll-plaza rates for plazas the route polyline actually passes near (within 2km
    -- toll plazas sit directly on the highway, so a route using that highway passes very close to
    the plaza's own coordinates, and 2km comfortably covers real-world geocoding slop without
    picking up a plaza on a genuinely different nearby road). Checks both the hand-curated
    TOLL_PLAZAS table and plazas learned from past live lookups.

    Returns None (not zero) when no known plaza matches -- that means this route isn't in
    TOLL_PLAZAS's coverage yet, not that the route is toll-free. Callers should fall back to a
    live lookup or Google's (caveated) estimate rather than claim Rs 0.
    """
    if not path:
        return None
    all_plazas = TOLL_PLAZAS + _get_learned_toll_plazas()
    matched = [
        plaza for plaza in all_plazas
        if min(_haversine_km(lat, lng, plaza["lat"], plaza["lng"]) for lat, lng in path) <= 2.0
    ]
    if not matched:
        return None
    return {"total_inr": sum(p["car_inr"] for p in matched), "plazas": [p["name"] for p in matched]}


@timed_tool
def calculate_route_and_etas(origin: str, destination: str, departure_time_iso: str) -> dict:
    """
    Calculates the route between an origin and destination, providing total duration, distance,
    estimated toll cost, and estimated arrival times (ETAs) for major milestones, considering traffic.

    Also returns 'waypoints': a handful of real points along the route (one roughly every ~2 hours
    of actual driving, not distance), each with a real, traffic-aware 'estimated_arrival_iso' from a
    second Routes API call -- not the model's own arithmetic from total trip duration. Use these, not a guess, whenever
    reasoning about what time you'd actually reach a specific stretch of the route (e.g. deciding
    whether a stop lands in a meal window) -- traffic can move a stop's real arrival time by 10-20+
    minutes versus a flat proportional estimate, which is exactly the gap between "looks like a great
    lunch spot" and "you'll actually get there at 2:30pm." May be null/empty if this enrichment call
    failed; the rest of the response is unaffected either way.
    """
    api_key = _maps_api_key()
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
            stats = _api_stats('routes_api_stats', '_routes_api_stats')
            stats['failed'] = stats.get('failed', 0) + 1
            return {"error": f"Both Routes APIs failed -- new: {error}; legacy: {legacy_error}"}
    stats = _api_stats('routes_api_stats', '_routes_api_stats')
    stats[source] = stats.get(source, 0) + 1

    if not routes_data.get('routes'):
        return {"total_duration_seconds": 0, "total_distance_meters": 0, "legs": []}

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
        })

    # Show both numbers side by side rather than silently picking one -- verified directly against
    # the live API that they can disagree a lot: Google's Routes API TOLLS extraComputation
    # returned Rs 1950 for Hyderabad<->Bengaluru against NHAI-sourced calculators' Rs 715, a ~2.7x
    # overestimate. Concierge Estimate is our own curated, NHAI-sourced toll-plaza data
    # (TOLL_PLAZAS above), present only for the corridors it covers; Google Estimate is always
    # included when Google returns one, labeled as Google's own figure so neither is passed off as
    # the other.
    route_path = decode_polyline(route['polyline']['encodedPolyline'])
    known_toll = _toll_for_route(route_path)
    if not known_toll:
        # No corridor match in TOLL_PLAZAS or what's been learned so far -- try a live lookup
        # before falling back to Google's estimate. Successful discoveries get persisted (see
        # _persist_learned_plazas) so this exact corridor doesn't need re-discovering next time.
        live_toll = _toll_from_live_lookup(origin, destination, route['polyline']['encodedPolyline'], route_path)
        if live_toll:
            known_toll = {"total_inr": live_toll["total_inr"], "plazas": live_toll["plazas"]}
            _persist_learned_plazas(live_toll["new_plazas"])

    toll_prices = route.get('travelAdvisory', {}).get('tollInfo', {}).get('estimatedPrice', [])
    google_toll = None
    if toll_prices:
        price = toll_prices[0]
        google_toll = f"{price.get('units', '0')}.{price.get('nanos', 0) // 10_000_000:02d} {price.get('currencyCode', '')}".strip()

    toll_parts = []
    if known_toll:
        toll_parts.append(f"Concierge Estimate: Rs {known_toll['total_inr']} "
                           f"(verified against known toll plazas: {', '.join(known_toll['plazas'])})")
    if google_toll:
        toll_parts.append(f"Google Estimate: {google_toll}")
    estimated_toll = " | ".join(toll_parts) if toll_parts else None

    # Stashed for the UI (route map) and for search_places_along_route to read directly, rather
    # than sent to the model as a return value -- see search_places_along_route's docstring for why
    # (the model corrupting this ~8-9KB opaque string when passing it back as an argument). Goes on
    # the current PlanJob, not st.session_state -- this runs on a background job's worker thread,
    # which has no ScriptRunContext (see the module comment above PlanJob).
    job = _current_job()
    if job is not None:
        job.route_polyline = route['polyline']['encodedPolyline']
    else:
        st.session_state.route_polyline = route['polyline']['encodedPolyline']

    # A second, real Routes API call for real traffic-aware timing at a few points along the route
    # -- not just the trip's start/end. Two calls, not the model's own guesswork: without this, the
    # per-stop times a plan shows (e.g. "reach lunch at 1pm") were the model estimating from total
    # trip duration alone, which is exactly the kind of unverified claim this app is trying to get
    # away from -- verified directly: at 7:30pm on a real Bengaluru->Ooty route, one leg's real
    # traffic-aware duration differed from its no-traffic estimate by over 12 minutes. This also
    # replaces guessing *where* to search for places (see search_places_along_route) with genuinely
    # time-informed positions instead of a flat km-based assumption.
    route_cum = route_cumulative_km(route_path)
    route_total_km = route_cum[-1] if route_cum else 0.0
    total_duration_seconds = int(route['duration'].replace('s', ''))
    waypoints = []
    if route_path and route_total_km > 0:
        # ~2-hour driving segments, not distance -- a 267km/5h40m trip gets 2 midpoints (3
        # segments), a slower 267km ghat-heavy trip would get more than a flat highway trip
        # covering the same distance in less time. This is only used to pick HOW MANY points to
        # ask about; WHERE those points sit still has to use distance fraction along the route
        # (no finer-grained per-point timing exists before the second call below runs) -- only the
        # *count* of segments is duration-based now, not the positions themselves.
        interval_seconds = 2 * 3600
        n_mid = max(0, min(6, round(total_duration_seconds / interval_seconds) - 1))
        guess_fractions = [(i + 1) / (n_mid + 1) for i in range(n_mid)]
        guess_coords, seen_km = [], []
        for frac in guess_fractions:
            target_km = route_total_km * frac
            if any(abs(target_km - k) < 15 for k in seen_km):
                continue
            idx = min(range(len(route_cum)), key=lambda j: abs(route_cum[j] - target_km))
            guess_coords.append(route_path[idx])
            seen_km.append(route_cum[idx])

        if guess_coords:
            waypoints_url = "https://routes.googleapis.com/directions/v2:computeRoutes"
            waypoints_headers = {
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "routes.legs.duration,routes.legs.staticDuration,routes.legs.distanceMeters",
            }
            waypoints_data = {
                "origin": {"address": origin},
                "destination": {"address": destination},
                "intermediates": [{"location": {"latLng": {"latitude": lat, "longitude": lng}}} for lat, lng in guess_coords],
                "departureTime": departure_time_iso,
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_AWARE",
                "languageCode": "en-US",
                "units": "METRIC",
            }
            try:
                wp_response = _api_request("POST", waypoints_url, headers=waypoints_headers, json_body=waypoints_data)
                if wp_response.ok:
                    wp_legs = wp_response.json().get('routes', [{}])[0].get('legs', [])
                    cursor = parsed_departure if parsed_departure and parsed_departure > now else datetime.fromisoformat(departure_time_iso.replace('Z', '+00:00'))
                    cursor_km = 0.0
                    for (lat, lng), leg in zip(guess_coords, wp_legs):
                        leg_seconds = int(leg.get('duration', '0s').rstrip('s'))
                        cursor = cursor + timedelta(seconds=leg_seconds)
                        cursor_km += leg.get('distanceMeters', 0) / 1000.0
                        waypoints.append({
                            "lat": lat, "lng": lng, "km_from_origin": round(cursor_km),
                            "estimated_arrival_iso": cursor.isoformat().replace('+00:00', 'Z'),
                        })
                else:
                    print(f"[calculate_route_and_etas] waypoint timing call failed: {wp_response.status_code}: {wp_response.text}", flush=True)
            except requests.RequestException as exc:
                print(f"[calculate_route_and_etas] waypoint timing call failed: {exc}", flush=True)
    # Best-effort: if this failed or found nothing, search_places_along_route falls back to its own
    # distance-only guess (see its _fallback_break_points) rather than the whole plan failing over a
    # secondary enrichment call.
    if job is not None:
        job.route_waypoints = waypoints
    else:
        st.session_state.route_waypoints = waypoints

    return {
        "total_duration_seconds": total_duration_seconds,
        "total_distance_meters": route['distanceMeters'],
        "legs": legs,
        "estimated_toll_cost": estimated_toll,
        "waypoints": [
            {"km_from_origin": w["km_from_origin"], "estimated_arrival_iso": w["estimated_arrival_iso"]}
            for w in waypoints
        ] or None,
    }


@timed_tool
def search_places_along_route(categories: list[str]) -> dict:
    """
    Searches for places near a handful of specific points chosen along the current route, for one
    or more free-text queries. Uses the route from the most recent calculate_route_and_etas call --
    call that first.

    There is no 'encoded_polyline' parameter on purpose: an earlier version had the model pass the
    route polyline as an argument, and it was observed corrupting the ~8-9KB opaque string in
    transit -- a model regenerates a function-call argument token by token rather than copying it
    verbatim, and periodically produced a string Places API (New) rejected with "Search Along Route
    requires a valid and non-empty polyline" on an otherwise-valid request. Reading it from
    st.session_state (stashed by calculate_route_and_etas as a side effect) instead of asking the
    model to carry data it doesn't need to reason about removes that failure mode at the source.

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
    30-80+ seconds apiece, versus ~1s for the underlying Places lookups themselves), so one call
    covering N categories is dramatically faster than N separate calls, the same reasoning
    get_place_details_and_reviews below already applies to batching place_ids.

    HOW RESULTS ARE FOUND (changed this pass -- see _break_points): rather than one broad query
    covering the whole route and hoping Google's own ranking happens to spread out (it doesn't --
    a single query was observed returning almost every result from the dense origin city, since it
    has vastly more high-rated places than sparse highway stretches further out), this first decides
    WHERE along the route actually makes sense to look -- roughly every ~2 hours of actual driving
    (traffic-aware when calculate_route_and_etas's second call succeeded, distance-approximated
    otherwise), plus the start and end -- then runs a real, separately-scoped search at each of
    those points. This is a real fix to the search itself, not a filter applied after the fact to
    whatever Google happened to return.

    ALWAYS include these four base categories in every call, regardless of what the user explicitly
    asked for, in addition to anything else the trip specifically needs (e.g. a pharmacy, an ATM,
    a grocery run): a food/restaurant query (phrased for any stated dietary preference, e.g. "pure
    vegetarian restaurant"), a fuel/petrol station query, a hospital/emergency care query, and a tea
    /snacks query. This exists because a plan that only covers what the user thought to ask for is
    exactly the kind of gap this app is meant to close -- nobody remembers to ask "are there
    hospitals nearby" until they need one. See the system prompt's Base Category Rubric for the
    per-category detail (what to look for, how to phrase each query, what fields matter for it).

    Returns {"results_by_category": {category: {"places": [...]} | {"error": "..."}}} -- one entry
    per requested category, each independently either a places list or an error, so one bad/empty
    category never blocks the results for the others. Each place includes 'distance_from_origin_km'
    (a real number computed from the route geometry, not a guess) -- copy it into the option's
    'location_text'/verdict (e.g. "~165 km into the trip" or "right at departure") so the user can
    tell at a glance whether a stop is actually along their route or just near where they're
    starting from.

    Each place also includes 'recognized_chain' -- non-null only when the place's name matched a
    known brand (see RECOGNIZED_CHAINS) known to hold reasonably consistent standards across
    locations in India. This is a soft, name-based signal for verdict tone (e.g. "a recognized
    chain, consistent standards across locations") -- NOT evidence for any specific fact. Never use
    it to state or imply restroom availability, hours, or anything else that get_place_details_and_
    reviews's real per-place fields already cover -- those facts, when available, always win.
    """
    encoded_polyline = _route_polyline()
    if not encoded_polyline:
        return {"error": "No route has been calculated yet -- call calculate_route_and_etas first."}

    # Reuses route_cumulative_km/distance_along_route_km already built for The Strip's own
    # visualization -- same geometry, different use here (choosing where to search).
    route_path = decode_polyline(encoded_polyline)
    route_cum = route_cumulative_km(route_path)
    route_total_km = route_cum[-1] if route_cum else 0.0

    api_key = _maps_api_key()
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    def _fallback_break_points() -> list[tuple[float, float, float]]:
        """Used only if calculate_route_and_etas's real, traffic-timed waypoints aren't available
        (its second Routes API call failed) -- a distance-only guess at the same ~2-hour-segment
        idea, always including start and end, but with no real timing behind it: there's no per-leg
        duration to work from here, so the ~2hr target is approximated via an assumed 55 km/h
        average Indian highway speed (accounting for traffic/tolls/curves, not a free-flow speed)
        -- i.e. roughly every ~110km. Points closer than ~15km apart collapse to one. Returns
        (lat, lng, km_from_origin) tuples."""
        if not route_path or route_total_km <= 0:
            return []
        assumed_kmh = 55.0
        interval_km = assumed_kmh * 2.0
        n_mid = max(0, min(6, round(route_total_km / interval_km) - 1))
        fractions = [0.0] + [(i + 1) / (n_mid + 1) for i in range(n_mid)] + [1.0]
        points, seen_km = [], []
        for frac in fractions:
            target_km = route_total_km * frac
            if any(abs(target_km - k) < 15 for k in seen_km):
                continue
            idx = min(range(len(route_cum)), key=lambda j: abs(route_cum[j] - target_km))
            lat, lng = route_path[idx]
            points.append((lat, lng, route_cum[idx]))
            seen_km.append(route_cum[idx])
        return points

    # Prefer the real, traffic-timed waypoints calculate_route_and_etas already computed (a second
    # Routes API call with real arrival times) over guessing again from scratch here -- those were
    # validated against real traffic, this fallback wasn't. Always add the start and end as anchors
    # too (a genuine "eat before you leave" / "near arrival" option is legitimate), since those
    # waypoints only cover the intermediate points, not the trip's own start/end.
    stored_waypoints = _route_waypoints()
    if stored_waypoints and route_path:
        break_points = [(route_path[0][0], route_path[0][1], 0.0)]
        break_points += [(w['lat'], w['lng'], w['km_from_origin']) for w in stored_waypoints]
        break_points.append((route_path[-1][0], route_path[-1][1], route_total_km))
    else:
        break_points = _fallback_break_points()

    # There is no dedicated "search near a point" endpoint distinct from ordinary Text Search in
    # the Places API (New) -- locationBias is just a parameter on the same searchText call
    # search_places_along_route used to use with searchAlongRouteParameters instead.
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName.text,places.rating,places.userRatingCount,places.location,places.types,places.formattedAddress"
    }

    def _search_new(category: str, lat: float, lng: float):
        data = {
            "textQuery": category,
            "locationBias": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": 12000.0}},
            "pageSize": 8,
            "languageCode": "en-US",
            "minRating": 3.5,
        }
        try:
            response = _api_request("POST", url, headers=headers, json_body=data)
        except requests.RequestException as exc:
            print(f"[search_places_along_route] category={category!r} point=({lat:.4f},{lng:.4f}) request failed: {exc}", flush=True)
            return None, f"Places API (New) request failed: {exc}"
        if not response.ok:
            # _api_request already retried transient 5xx a couple of times -- this is logged (error
            # path only, not per-call) so a recurrence past those retries still shows in server logs.
            print(f"[search_places_along_route] category={category!r} point=({lat:.4f},{lng:.4f}) HTTP {response.status_code}: {response.text}", flush=True)
            return None, f"Places API (New) error {response.status_code}: {response.text}"
        return response.json().get('places', []), None

    def _search_legacy(category: str, lat: float, lng: float):
        # Fallback for when Places API (New) is down/failing -- the legacy Nearby Search endpoint
        # does the same "search near this point" job with a different request/response shape.
        # Normalizes into the exact same shape _search_new returns (a New-API-style 'places' list)
        # so every line below this point stays identical regardless of which tier served the data.
        params = {"location": f"{lat},{lng}", "radius": 12000, "keyword": category, "key": api_key}
        try:
            response = _api_request("GET", "https://maps.googleapis.com/maps/api/place/nearbysearch/json", params=params)
        except requests.RequestException as exc:
            return None, f"Places API (legacy) request failed: {exc}"
        if not response.ok:
            return None, f"Places API (legacy) error {response.status_code}: {response.text}"
        places = []
        for r in response.json().get('results', []):
            place_id = r.get('place_id')
            if not place_id or (r.get('rating') or 0) < 3.5:
                continue
            loc = r.get('geometry', {}).get('location', {})
            places.append({
                "id": place_id,
                "displayName": {"text": r.get('name', '')},
                "rating": r.get('rating'),
                "userRatingCount": r.get('user_ratings_total'),
                "formattedAddress": r.get('vicinity', ''),
                "types": r.get('types', []),
                "location": {"latitude": loc.get('lat'), "longitude": loc.get('lng')},
            })
        if not places:
            return None, "legacy Places API returned no results either"
        return places, None

    def _fetch_at_point(category: str, lat: float, lng: float, km: float):
        # Runs in a worker thread -- network I/O only, no st.session_state access (see the note in
        # get_place_details_and_reviews below for why that matters).
        places, error = _search_new(category, lat, lng)
        source = "new"
        if error:
            print(f"[search_places_along_route] category={category!r} point=({lat:.4f},{lng:.4f}) falling back to legacy Places API after: {error}", flush=True)
            places, legacy_error = _search_legacy(category, lat, lng)
            source = "legacy"
            if legacy_error:
                return category, km, None, f"Both Places APIs failed -- new: {error}; legacy: {legacy_error}", "failed"
        return category, km, places, None, source

    # One flat batch across every (category, break point) pair -- e.g. 2 categories x 4 points is
    # 8 parallel requests, but still zero extra round trips through the model's own reasoning, since
    # this is all still inside the single AFC call this function represents.
    work_items = [(category, lat, lng, km) for category in categories for (lat, lng, km) in break_points]
    if not work_items:
        return {"results_by_category": {c: {"error": "route has no usable geometry to search along"} for c in categories}}

    with ThreadPoolExecutor(max_workers=min(8, len(work_items)) or 1) as executor:
        fetched = list(executor.map(lambda w: _fetch_at_point(*w), work_items))

    discovered_places = _discovered_places()

    # Tracks which tier (new/legacy/failed) actually served each category's results this request --
    # read by log_usage_event so "how often is the fallback needed, how often do both fail" is a
    # real, trackable number instead of something only visible by reading server logs (see HANDOFF.md).
    api_stats = _api_stats('places_api_stats', '_places_api_stats')

    by_category: dict[str, list[tuple[float, list[dict] | None, str | None, str]]] = {c: [] for c in categories}
    for category, km, places_data, error, source in fetched:
        api_stats[source] = api_stats.get(source, 0) + 1
        by_category[category].append((km, places_data, error, source))

    results_by_category = {}
    for category, point_results in by_category.items():
        if all(error for _km, _places, error, _source in point_results):
            results_by_category[category] = {"error": "; ".join(error for *_, error, _ in point_results if error)}
            continue

        places = []
        seen_ids = set()
        for km, places_data, error, source in point_results:
            if error or not places_data:
                continue
            # Best-rated candidate at this specific point -- one option per break point per
            # category, so results stay spread across the route instead of clustering wherever one
            # point happened to return the most/highest-rated raw candidates.
            candidates = [p for p in places_data if p.get('id') not in seen_ids]
            if not candidates:
                continue
            candidates.sort(key=lambda p: -(p.get('rating') or 0))
            p_data = candidates[0]
            seen_ids.add(p_data['id'])

            # The place's own real coordinates give a more precise distance-along-route than the
            # break point's own km -- they'll usually be close, but the actual place can be a couple
            # of km off from the point that was searched around.
            loc = p_data.get('location', {})
            lat, lng = loc.get('latitude'), loc.get('longitude')
            precise_km = distance_along_route_km(route_path, route_cum, lat, lng) if lat is not None and lng is not None else km

            places.append({
                "place_id": p_data['id'],
                "name": p_data['displayName']['text'],
                "rating": p_data.get('rating'),
                "user_ratings_total": p_data.get('userRatingCount'),
                "vicinity": p_data.get('formattedAddress', ''),
                "types": p_data.get('types', [])[:4],
                # Real, computed position along the route -- not a guess. round()'d to the nearest
                # km since sub-km precision implies an accuracy nearest-vertex matching doesn't have.
                "distance_from_origin_km": round(precise_km),
                # A name match only, not a per-place fact -- see RECOGNIZED_CHAINS' caveat. Null
                # unless this exact place's name matched one of the listed brands.
                "recognized_chain": _match_recognized_chain(p_data['displayName']['text']),
            })

            # Track every discovered place so the UI can offer a "Navigate" link and a map marker
            # for it later -- the chat response is free-form text, so this is the only reliable
            # source of real place_ids and coordinates. Lat/lng isn't sent to the model, just
            # stashed for the UI.
            discovered_places[p_data['id']] = {
                "name": p_data['displayName']['text'],
                "vicinity": p_data.get('formattedAddress', ''),
                "lat": lat, "lng": lng,
            }

        if not places:
            results_by_category[category] = {"error": "no results at any searched point along the route"}
            continue
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

    Also includes 'restroom_available' -- Google's own per-place restroom signal, not a guess. This
    exists because a place's category or cuisine was previously being used to imply restroom
    availability across a whole list of options (e.g. "we picked restaurants with clean restrooms")
    even when most individual places never actually confirmed it -- restroom_available must be read
    and stated per place, never assumed from category, chain, or a restroom search run elsewhere.

    Results are cached across users/sessions by place_id (see _PLACE_CACHE_FRESH_DAYS and friends
    above) -- a place looked up recently is reused outright, one looked up a while ago gets a cheap
    recheck before being trusted again, and only a genuinely stale or never-seen place pays for the
    full lookup below. Entirely transparent to the caller: the shape of what's returned is identical
    either way.

    Also includes 'phone' -- Google's own listed number for that exact place, nullable if Google
    doesn't have one. Matters most for hospital/emergency options, where a name and a rating alone
    aren't actually useful in an emergency.
    """
    api_key = _maps_api_key()
    if not api_key:
        return {"error": "GOOGLE_MAPS_API_KEY is not configured on the server."}

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        # currentOpeningStatus is NOT a real field on the Place resource (it 400s) -- the actual
        # field for "is it open right now" is currentOpeningHours.openNow, mapped below.
        "X-Goog-FieldMask": "id,displayName.text,rating,userRatingCount,formattedAddress,nationalPhoneNumber,websiteUri,currentOpeningHours,priceLevel,regularOpeningHours,reviews,servesBreakfast,servesLunch,servesDinner,servesVegetarianFood,parkingOptions,photos,restroom"
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
            "restroom": None,
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

    def _process(details_data: dict) -> tuple[dict, str | None]:
        """Turns a raw Places API response into (model-visible dict, photo_name) -- factored out of
        the fetch loop so a cache write can store exactly what a future cache hit will replay,
        instead of the two ever being able to drift apart."""
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

        # Google's own confirmed signal, not a guess -- 'restroom' is present/absent on the Place
        # resource itself. Missing from the response means Google hasn't confirmed either way, which
        # is a different, weaker claim than "no restroom" and needs to read as unconfirmed, not absent.
        restroom_flag = details_data.get('restroom')
        if restroom_flag is True:
            restroom_text = "Confirmed by Google"
        elif restroom_flag is False:
            restroom_text = "Google indicates no restroom at this location"
        else:
            restroom_text = "Not confirmed by Google -- verify locally if this matters"

        model_dict = {
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
            "parking_available": available_parking if available_parking else "Not listed by Google -- mention this is unverified if parking matters for this trip",
            "restroom_available": restroom_text,
            # Google's own listed number for this exact place -- not looked up separately, just
            # finally surfaced (the field was already in the mask, unused). Matters most for
            # hospitals/emergency options, where a name and a rating alone aren't actually useful.
            "phone": details_data.get('nationalPhoneNumber'),
        }
        photos = details_data.get('photos', [])
        photo_name = photos[0]['name'] if photos else None
        return model_dict, photo_name

    # See the matching comment in search_places_along_route -- same tracked-tier pattern, same key,
    # so one request's stats cover both tools' underlying Places calls together.
    api_stats = _api_stats('places_api_stats', '_places_api_stats')
    now = datetime.now(timezone.utc)
    cache = _load_place_details_cache()

    def _emit(place_id: str, model_dict: dict, photo_name: str | None):
        """Appends the model-visible result and, if this session's discovered_places already knows
        about this place_id (populated by search_places_along_route earlier in this same request),
        stashes its photo for the UI -- shared by every path (cache-fresh, cache-verified, freshly
        fetched) so this bookkeeping can't accidentally be skipped for a cache hit."""
        results.append({"place_id": place_id, **model_dict})
        discovered_places = _discovered_places()
        if photo_name and place_id in discovered_places:
            discovered_places[place_id]['photo_name'] = photo_name

    results = []
    cache_writes = []
    to_fetch = []
    to_verify = []
    for place_id in place_ids:
        entry = cache.get(place_id)
        if not entry:
            to_fetch.append(place_id)
            continue
        age_days = (now - entry['cached_at']).days
        if age_days <= _PLACE_CACHE_FRESH_DAYS:
            _emit(place_id, entry['details']['model'], entry['details'].get('photo_name'))
            api_stats['cache_fresh'] = api_stats.get('cache_fresh', 0) + 1
        elif age_days <= _PLACE_CACHE_VERIFY_DAYS:
            to_verify.append((place_id, entry))
        else:
            to_fetch.append(place_id)

    # Cheap recheck pass for aging-but-not-yet-stale entries -- one small GET per place (Pro/
    # Enterprise-tier pricing, no reviews/photos/hours requested), in parallel. Any failure here
    # (network error, non-200, an actual change) falls through to a full fetch rather than risking
    # trusting stale data -- this recheck's whole job is to catch drift, so it must never silently
    # swallow a case where it couldn't actually confirm nothing changed.
    def _cheap_recheck(place_id: str, entry: dict):
        verify_headers = {
            "Content-Type": "application/json", "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "businessStatus,rating",
        }
        try:
            response = _api_request(
                "GET", f"https://places.googleapis.com/v1/places/{place_id}", headers=verify_headers
            )
        except requests.RequestException:
            return place_id, entry, False
        if not response.ok:
            return place_id, entry, False
        data = response.json()
        if data.get('businessStatus', 'OPERATIONAL') != 'OPERATIONAL':
            return place_id, entry, False
        new_rating, old_rating = data.get('rating'), entry.get('rating_at_cache')
        if new_rating is not None and old_rating is not None and abs(new_rating - old_rating) > _PLACE_CACHE_RATING_DRIFT_THRESHOLD:
            return place_id, entry, False
        return place_id, entry, True

    if to_verify:
        with ThreadPoolExecutor(max_workers=min(8, len(to_verify)) or 1) as executor:
            verify_results = list(executor.map(lambda pe: _cheap_recheck(*pe), to_verify))
        for place_id, entry, unchanged in verify_results:
            api_stats['cache_verified' if unchanged else 'cache_verify_failed'] = (
                api_stats.get('cache_verified' if unchanged else 'cache_verify_failed', 0) + 1
            )
            if unchanged:
                _emit(place_id, entry['details']['model'], entry['details'].get('photo_name'))
                # Nothing changed -- just refresh cached_at so this entry doesn't get re-verified
                # again immediately next time; keep the same rating/details already on file.
                cache_writes.append({
                    "place_id": place_id, "name": entry.get('name', ''), "cached_at": now,
                    "rating_at_cache": entry.get('rating_at_cache'), "details": entry['details'],
                })
            else:
                to_fetch.append(place_id)

    # Full fetch for whatever's genuinely stale, never seen, or failed its cheap recheck --
    # identical to the original always-fetch behavior below.
    if to_fetch:
        with ThreadPoolExecutor(max_workers=min(8, len(to_fetch)) or 1) as executor:
            fetched = list(executor.map(_fetch, to_fetch))

        for place_id, details_data, error, source in fetched:
            api_stats[source] = api_stats.get(source, 0) + 1
            if error:
                results.append({"place_id": place_id, "error": error})
                continue
            model_dict, photo_name = _process(details_data)
            _emit(place_id, model_dict, photo_name)
            cache_writes.append({
                "place_id": place_id, "name": details_data.get('displayName', {}).get('text', ''),
                "cached_at": now, "rating_at_cache": details_data.get('rating'),
                "details": {"model": model_dict, "photo_name": photo_name},
            })

    _persist_place_details_cache(cache, cache_writes)
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
        <style>
          :root {{ --ink: #1a1a1a; --paper-raised: #ffffff; --line: #b8b6ae; --accent: #b3502c; }}
          @media (prefers-color-scheme: dark) {{
            :root {{ --ink: #ece9e2; --paper-raised: #262521; --line: #45433c; --accent: #e08a5c; }}
          }}
          .copy-btn {{
            padding:6px 14px; border-radius:6px; border:1px solid var(--line);
            background:var(--paper-raised); color:var(--ink); cursor:pointer; font-size:14px;
          }}
          .copy-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
        </style>
        <div style="display:flex; gap:8px; font-family:sans-serif;">
          <button class="copy-btn" onclick="
            (function(btn){{
              const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
              const decoded = new TextDecoder('utf-8').decode(bytes);
              navigator.clipboard.writeText(decoded).then(() => {{
                const orig = btn.innerText;
                btn.innerText = '✅ Copied!';
                setTimeout(() => {{ btn.innerText = orig; }}, 1500);
              }});
            }})(this)
          ">📋 Copy</button>
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
    place, so the user can see the trip and stops at a glance instead of only reading about them.

    Wrapped in a broad try/except: decode_polyline's inner loop has no bounds check, so a
    genuinely truncated/corrupted polyline raises IndexError -- and since this is called
    unconditionally after every plan renders, an uncaught exception here wouldn't just break the
    map, it would crash the ENTIRE page on every single rerun until a brand new plan is started,
    even though the plan itself succeeded. Found during a review after a similar unguarded-
    external-data crash was fixed elsewhere (see chat.send_message's error handling). Degrading to
    no map is a far smaller loss than losing an otherwise-working plan."""
    try:
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
    except Exception:
        logger.exception("render_route_map failed")
        st.caption("🗺️ Route map unavailable for this plan.")


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


@st.cache_data(show_spinner=False)
def _lothal_illustration_data_uri() -> str | None:
    """Base64-encodes lothal_bg.jpg once per server process, not once per rerun -- Streamlit
    re-executes the whole script on every interaction, so without caching this would re-read and
    re-encode a ~163KB file on every single click. Returns None if the asset is missing so the
    caller degrades to no background instead of crashing.

    lothal_bg.jpg is a downscaled/re-compressed JPEG derived from the user's own shared
    illustration of Lothal (originally a 6MB, 2752x1536 PNG -- far too heavy to embed inline on
    every rerun). Resized to 1399px wide and re-encoded as JPEG at quality 62: a background shown
    at 14% opacity doesn't need full photographic fidelity, and this brought it down to ~163KB."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lothal_bg.jpg")
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def render_home_illustrations():
    """Shown only before a trip is planned -- once planning_triggered is set, the actual plan
    (and its own imagery) takes over and this shouldn't linger.

    A real background layer (CSS background-image on a negative-z-index pseudo-element sized to
    the whole app container, not a separate element in the page's normal document flow -- a plain
    full-width <img> here would still sit inside Streamlit's own stMainBlockContainer padding,
    verified live at 80px left/right, and read as a boxed panel rather than blending in).

    Uses the user's own shared illustration of Lothal (see _lothal_illustration_data_uri), not the
    hand-authored SVG illustrations in _HOME_ILLUSTRATIONS below -- explicit feedback after trying
    those: wanted the real shared artwork, not a generated one. _HOME_ILLUSTRATIONS is left defined,
    just unused here, in case it's wanted again later or reused elsewhere."""
    if st.session_state.get('planning_triggered'):
        return
    data_uri = _lothal_illustration_data_uri()
    if not data_uri:
        return
    st.markdown(
        f"""
        <style>
        [data-testid="stAppViewContainer"] {{ position: relative; }}
        [data-testid="stAppViewContainer"]::before {{
            content: "";
            position: absolute;
            inset: 0;
            background-image: url("{data_uri}");
            background-size: cover;
            background-position: center top;
            background-repeat: no-repeat;
            opacity: 0.14;
            z-index: -1;
            pointer-events: none;
        }}
        </style>
        """,
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
        "location_text": {"type": "STRING", "nullable": True, "description": "REQUIRED whenever this option came from a tool result: copy the 'vicinity' field from search_places_along_route verbatim (e.g. 'NH44, Thandavapura, Karnataka' or 'Magadi Road, Bengaluru') -- a name alone doesn't tell the user whether a stop is near the start of the trip or genuinely along the route ahead of them. Only null for an option with no tool result to read it from."},
        "place_id": {"type": "STRING", "nullable": True, "description": "The place_id from get_place_details_and_reviews, if this option came from a tool result."},
        "rating_text": {"type": "STRING", "nullable": True, "description": "e.g. '4.0 (2,082 reviews)'"},
        "price_level": {"type": "STRING", "nullable": True, "description": "e.g. 'Budget', 'Moderate', 'Expensive'"},
        "hours_status": {"type": "STRING", "nullable": True, "description": "e.g. 'Open now, closes 11:00 PM'"},
        "parking": {"type": "STRING", "nullable": True},
        "restroom": {"type": "STRING", "nullable": True, "description": "REQUIRED whenever this option came from get_place_details_and_reviews: copy that tool result's 'restroom_available' text verbatim (e.g. 'Confirmed by Google' or 'Not confirmed by Google -- verify locally if this matters'). This is a per-place Google signal -- never state or imply a place has a restroom because of its category, cuisine, chain, or because a restroom search ran elsewhere on the route. Only null for an option with no tool result to read it from."},
        "phone": {"type": "STRING", "nullable": True, "description": "Copy get_place_details_and_reviews's 'phone' field verbatim when non-null. Especially important for hospital/emergency options -- a name and rating alone aren't useful in an emergency. Null when Google doesn't list one."},
        "elder_suitability": {"type": "STRING", "nullable": True, "description": "Only for food stops when the trip involves elderly travelers."},
        "review_snippet": {"type": "STRING", "nullable": True},
        "review_recency": {"type": "STRING", "nullable": True, "description": "REQUIRED whenever this option came from get_place_details_and_reviews: copy most_recent_review.relative_time verbatim (e.g. '3 weeks ago'). This is what lets the user judge whether the star rating still reflects the place today, not just what it was years ago. Only null for an option with no tool result to read it from."},
        "critical_review_snippet": {"type": "STRING", "nullable": True, "description": "REQUIRED field (value may be null, but the field itself must always be set, never omitted): copy critical_review.text verbatim if get_place_details_and_reviews returned a critical_review for this place, so the user sees a real downside alongside the positive quote -- set explicitly to null only when critical_review was actually null (every review Google returned was positive), never left out just because a bad review would make the option look worse."},
        "verdict": {"type": "STRING", "description": "The concierge's honest, specific take on this option -- pros/cons, not a single winner declaration."},
    },
    "required": ["name", "verdict", "review_recency", "critical_review_snippet", "location_text", "restroom"],
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
    if option.get("location_text"):
        lines.append(f"- 📍 {option['location_text']}")
    details = []
    if option.get("price_level"):
        details.append(f"Price: {option['price_level']}")
    if option.get("parking"):
        details.append(f"Parking: {option['parking']}")
    if option.get("restroom"):
        details.append(f"Restroom: {option['restroom']}")
    if option.get("phone"):
        details.append(f"Phone: {option['phone']}")
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
    if option.get("location_text"):
        lines.append(f"📍 {option['location_text']}")
    details = []
    if option.get("price_level"):
        details.append(f"Price: {option['price_level']}")
    if option.get("parking"):
        details.append(f"Parking: {option['parking']}")
    if option.get("restroom"):
        details.append(f"Restroom: {option['restroom']}")
    if option.get("phone"):
        details.append(f"Phone: {option['phone']}")
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

    lines.append("### 🆘 Emergency Numbers")
    for number, label in EMERGENCY_NUMBERS:
        lines.append(f"- **{number}** — {label}")
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

    # Fixed, code-rendered, not model-generated -- see EMERGENCY_NUMBERS above for why this
    # deliberately bypasses the AI/schema entirely rather than trusting a prompt instruction.
    st.markdown(
        "### 🆘 Emergency Numbers\n" +
        "\n".join(f"- **{number}** — {label}" for number, label in EMERGENCY_NUMBERS)
    )

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

    As a side effect, stashes data['plan'] into the current PlanJob (or st.session_state.latest_plan
    directly if there isn't one -- see the module comment above PlanJob) whenever this turn is a
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
            job = _current_job()
            if job is not None:
                job.latest_plan = data["plan"]
            else:
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

# Extends The Strip's own ink/paper/accent palette (see _STRIP_CSS above) across the rest of the
# page, rather than leaving everything outside the Strip visualization itself on Streamlit's stock
# default theme -- same tokens, same light/dark split, so the Strip no longer looks like a
# differently-designed component dropped into an otherwise generic app. Targets real data-testid
# selectors confirmed against this Streamlit build (1.62.0) rather than guessed class names, since
# those are the one thing that's stayed stable across Streamlit versions when internal class names
# haven't. Doesn't reach the Copy/Print buttons or the WhatsApp/Google-Maps buttons -- the first two
# are restyled at their own call sites since they render inside an <iframe> (a separate document,
# outside this page-level stylesheet's reach); the brand-colored ones are left alone on purpose,
# same reasoning as DESIGN.md gives for keeping WhatsApp green / Google blue where they link out to
# those actual products.
st.markdown(
    """<style>
    :root {
        --ink: #1a1a1a; --paper: #f5f4f0; --paper-raised: #ffffff;
        --line: #b8b6ae; --mute: #75726a; --accent: #b3502c;
    }
    @media (prefers-color-scheme: dark) {
        :root {
            --ink: #ece9e2; --paper: #1c1b18; --paper-raised: #262521;
            --line: #45433c; --mute: #a29d90; --accent: #e08a5c;
        }
    }

    body, [data-testid="stAppViewContainer"], [data-testid="stMain"], [data-testid="stHeader"],
    [data-testid="stSidebar"], [data-testid="stBottomBlockContainer"] {
        background: var(--paper) !important; color: var(--ink);
    }
    [data-testid="stHeader"] { border-bottom: 1px solid var(--line); }
    [data-testid="stSidebar"] { border-right: 1px solid var(--line); }

    [data-testid="stHeading"] h1, [data-testid="stHeading"] h2, [data-testid="stHeading"] h3,
    [data-testid="stMarkdown"] p, [data-testid="stMarkdown"] li, [data-testid="stWidgetLabel"] p {
        color: var(--ink) !important;
    }
    [data-testid="stCaptionContainer"] { color: var(--mute) !important; }
    a, a:visited { color: var(--accent); }

    [data-testid^="stBaseButton"] {
        background: var(--paper-raised) !important; color: var(--ink) !important;
        border: 1px solid var(--line) !important; border-radius: 6px !important;
    }
    [data-testid^="stBaseButton"]:hover {
        border-color: var(--accent) !important; color: var(--accent) !important;
    }

    [data-testid="stTextInputField"], [data-testid="stTextAreaRootElement"] textarea,
    [data-testid="stDateInputField"], [data-testid="stSelectbox"] > div > div,
    [data-baseweb="select"] > div {
        background: var(--paper-raised) !important; color: var(--ink) !important;
        border-color: var(--line) !important;
    }
    [data-testid="stTextInputField"]:focus, [data-testid="stTextAreaRootElement"] textarea:focus {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 1px var(--accent) !important;
    }

    [data-testid="stCheckbox"] input { accent-color: var(--accent); }
    [data-testid="stFeedbackButton"] { color: var(--mute) !important; }
    [data-testid="stFeedbackButton"][aria-checked="true"] { color: var(--accent) !important; }

    hr, [data-testid="stDivider"] { border-color: var(--line) !important; }
    [data-testid="stStatusWidget"] { background: var(--paper-raised) !important; border-color: var(--line) !important; }
    </style>""",
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

if st.session_state.get('planning_triggered', False) or st.query_params.get('plan_id'):
    # The query-param check alongside the session-state flag matters specifically for the
    # amnesia fix (see the module comment above PlanJob): a browser tab reconnecting after being
    # away gets a brand-new session with planning_triggered unset, same as a tab that's never
    # planned anything -- the URL's plan_id is the only thing that survives that and says this
    # session should still enter this block to poll for/restore its job.
    st.session_state.planning_triggered = True
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
        "Base Category Rubric -- ALWAYS include these four in that same categories list, regardless of what the "
        "user explicitly asked for, on top of anything else the trip specifically needs (e.g. a pharmacy, an "
        "ATM, a grocery stop): "
        "1) a food/restaurant query, phrased for any stated dietary preference (e.g. 'pure vegetarian "
        "restaurant' if the user wants veg, a plain 'restaurant' otherwise); "
        "2) a fuel/petrol station query ('petrol pump' or 'fuel station'); "
        "3) a hospital/emergency-care query ('hospital' or 'multispecialty hospital') -- present these under "
        "their own stop_categories entry (e.g. '🏥 Hospitals / Emergency Care'), and for every option here set "
        "the 'phone' field from get_place_details_and_reviews's 'phone' -- a hospital listing with no way to "
        "call it isn't actually useful in an emergency; "
        "4) a tea/snacks query ('tea stall snacks shop' or 'cafe') for a quick break distinct from a full meal. "
        "This exists because a plan that only covers what the user thought to ask for has exactly the kind of "
        "gap this app exists to close -- nobody remembers to ask 'are there hospitals nearby' until they "
        "actually need one. Skip a base category only for a genuinely short trip where it plainly doesn't "
        "apply (e.g. hospital search for a 15-minute in-city errand), and say so briefly in intro_text rather "
        "than silently dropping it without explanation. "
        "Call get_place_details_and_reviews exactly once, passing the place_ids of every candidate place "
        "you want details for together in one list, instead of calling it separately per place. "
        "Every option that came from a real tool result has a real place_id from that tool response -- always "
        "set the option's 'place_id' field to that exact value. It's used to build the 'get directions' link "
        "and the 'view on Google Maps' link for that exact business, so leaving it out or inventing one breaks "
        "those -- omit the field entirely only for an option that didn't come from a tool result at all. "
        "Fill 'itinerary_timeline' with departure and every stop's arrival time, and set 'toll_cost_text' in "
        "'trip_overview' from calculate_route_and_etas's estimated_toll_cost. When it contains both a "
        "'Concierge Estimate' and a 'Google Estimate', present both labeled exactly that way (e.g. 'Concierge "
        "Estimate: Rs 715 | Google Estimate: Rs 1950') rather than picking one -- they can disagree "
        "significantly, and the user should see both rather than have one silently chosen for them. "
        "Use emojis (🟢 Good, 🟡 Moderate, ⚠️ Red Flag) inside field text for quick-scan ratings where it helps. "
        "Provide actual ratings and review snippets in the relevant option fields. For every option, also set "
        "'review_recency' from get_place_details_and_reviews's most_recent_review.relative_time -- a 4.5-star "
        "rating built on reviews from years ago is a different, weaker signal than the same rating with reviews "
        "from last month, and the user can't tell the difference unless you say so. Also set "
        "'critical_review_snippet' from critical_review.text whenever get_place_details_and_reviews returned one "
        "for that place -- a genuinely balanced take shows the downside too, not just the best quote available; "
        "leave it null only when critical_review was actually null (nothing <=3 stars in what Google returned), "
        "never omit it just because it makes the option look worse. "
        "Also set 'location_text' for every option from search_places_along_route's 'vicinity' field, and fold "
        "in that same result's 'distance_from_origin_km' as a natural phrase -- e.g. 'NH44, Thandavapura, "
        "Karnataka -- about 75 km into the trip' or 'Magadi Road, Bengaluru -- right at departure, before you "
        "leave'. A name alone doesn't tell the user whether a stop is actually along the highway ahead of them "
        "or just near where they're starting from -- this is what makes that honest at a glance instead of "
        "something they have to infer or that gets buried only in the verdict text. "
        "Also set 'restroom' for every option from get_place_details_and_reviews's 'restroom_available' text, "
        "copied verbatim -- this is Google's own confirmed signal for that specific place, not a guess. Never "
        "state or imply that a place has a restroom because of its category, cuisine, chain, or because a "
        "separate restroom search ran elsewhere on the route -- a claim like 'we picked restaurants with clean "
        "restrooms' describing a whole list of options, when only some or none of the individual places actually "
        "confirmed it, is exactly the kind of generic, unverified claim this app exists to avoid. If "
        "restroom_available says Google hasn't confirmed one, say so plainly rather than staying silent about it "
        "or letting the reader assume it's covered. "
        "When search_places_along_route's 'recognized_chain' is non-null for an option, you may note in the "
        "verdict that it's a recognized chain with generally consistent standards across locations -- this is "
        "tone/confidence context only, from a name match, never a substitute for a real per-place fact. Do not "
        "use it to state or imply restroom availability, hours, or anything else that has its own real field -- "
        "when those fields say 'not confirmed,' say 'not confirmed,' even for a recognized chain. "
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
        "- Compare each stop's real arrival time -- from calculate_route_and_etas's 'waypoints' "
        "(estimated_arrival_iso, traffic-aware), matched to that stop by nearest km_from_origin, not "
        "a proportional guess from total trip duration -- against typical Indian meal windows "
        "(breakfast ~7-10am, lunch ~12:30-3pm, dinner ~7:30-10:30pm). If the journey overlaps one, "
        "proactively suggest a food stop timed to that point even if the user only asked for "
        "something else like fuel or snacks -- people traveling around mealtimes usually want to eat "
        "too. Traffic can shift a stop's real arrival time well outside what a flat proportional "
        "estimate would show (a stop that 'looks like' lunchtime by simple math can, with traffic, "
        "actually be reached mid-afternoon) -- use the real waypoint time, not the simple math, for "
        "every itinerary_timeline entry and every meal-window judgment. "
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

    # Reuse the same chat session across reruns so follow-up questions share context. Also handed
    # to _run_plan_job as existing_chat below -- created here, eagerly and main-thread-only, so a
    # plan's first request and every follow-up after it share one real conversation regardless of
    # which background job thread is actually driving it at any given moment.
    if st.session_state.get('chat') is None:
        st.session_state.chat = client.chats.create(
            model='gemini-3.6-flash',
            config=config
        )
    chat = st.session_state.chat

    if 'chat_messages' not in st.session_state:
        st.session_state.chat_messages = []

    def _submit_job(event_type: str, prompt: str, user_message: str = ""):
        """Registers a new PlanJob, hands it to the process-wide executor, and points the URL at
        it -- see the module comment above PlanJob for why this, instead of just calling
        chat.send_message() directly here on the main thread, is what actually survives the
        requesting browser tab going away mid-request."""
        job_id = uuid.uuid4().hex
        job = PlanJob(
            event_type=event_type,
            origin=st.session_state.origin,
            destination=st.session_state.destination,
            preferences=st.session_state.preferences,
            user_message=user_message,
        )
        registry = _get_job_registry()
        with registry["lock"]:
            registry["jobs"][job_id] = job
        _get_job_executor().submit(_run_plan_job, job_id, prompt, chat)
        st.query_params["plan_id"] = job_id
        if user_message:
            st.session_state.chat_messages.append({"role": "user", "content": user_message})
        # Marks this bubble as already shown in THIS session, so the merge step below (which exists
        # for a browser tab that DIDN'T just submit this job -- a reconnect, or a second tab) knows
        # not to add a duplicate once the job shows up as done.
        st.session_state._user_bubble_job_id = job_id

    # --- Poll/restore the job named in the URL, if any (see the module comment above PlanJob) ---
    # Runs on every script execution, independent of whether a new plan/follow-up is also being
    # submitted this same run -- a tab that reconnects mid-job, or loads fresh with an old plan_id
    # still sitting in its URL, needs this to see the running/finished job either way.
    plan_job_id = st.query_params.get("plan_id")
    active_job = None
    if plan_job_id:
        registry = _get_job_registry()
        with registry["lock"]:
            active_job = registry["jobs"].get(plan_job_id)
        if active_job is None:
            # Not in the in-memory registry -- either it finished before a server restart, or this
            # browser session is brand new and never saw that registry to begin with.
            active_job = _load_job_result_from_sheet(plan_job_id)
        if active_job is not None:
            # A session restored purely from the URL (a genuine reconnect, or a server restart)
            # never ran the "Plan My Trip" button handler that normally sets these -- needed here,
            # not just for display, since a follow-up's own _submit_job call reads them directly.
            st.session_state.setdefault("origin", active_job.origin)
            st.session_state.setdefault("destination", active_job.destination)
            st.session_state.setdefault("preferences", active_job.preferences)

    if active_job is not None and active_job.status != "running":
        if active_job.user_message and st.session_state.get("_user_bubble_job_id") != plan_job_id:
            st.session_state.chat_messages.append({"role": "user", "content": active_job.user_message})
            st.session_state._user_bubble_job_id = plan_job_id
        if st.session_state.get("_merged_job_id") != plan_job_id:
            # Copy the finished (or errored) job's result into st.session_state exactly once --
            # every render function below reads st.session_state and needs no changes past this.
            if active_job.status == "done":
                if active_job.chat is not None:
                    st.session_state.chat = active_job.chat
                    chat = active_job.chat
                if active_job.route_polyline is not None:
                    st.session_state.route_polyline = active_job.route_polyline
                if active_job.route_waypoints:
                    st.session_state.route_waypoints = active_job.route_waypoints
                if active_job.discovered_places:
                    st.session_state.discovered_places = active_job.discovered_places
                if active_job.latest_plan is not None:
                    st.session_state.latest_plan = active_job.latest_plan
                st.session_state.chat_messages.append({"role": "assistant", "content": active_job.content})
                if active_job.response_meta.get("structured_ok") and active_job.response_meta.get("response_type") == "plan":
                    st.session_state.latest_plan_message_index = len(st.session_state.chat_messages) - 1
            else:  # "error" -- matches the wording the old inline error handling used
                st.error(
                    "That request hit a temporary error talking to Gemini. This usually clears up "
                    "on its own -- please try again in a moment."
                )
            st.session_state._merged_job_id = plan_job_id

    job_running = plan_job_id is not None and active_job is not None and active_job.status == "running"

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
        _submit_job("plan", prompt)
        st.rerun()

    if job_running:
        label = "Planning your trip and evaluating live stops..." if active_job.event_type == "plan" else "Thinking..."
        # A background job thread has no Streamlit widget to write live progress into (see the
        # module comment above PlanJob) -- this just renders its latest snapshot fresh each poll,
        # rather than growing a persistent status log the way the old inline version could.
        with st.status(label, expanded=True) as status:
            for line in active_job.progress[-8:]:
                status.write(line)

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

    if job_running:
        # Blocked, not just discouraged -- a second concurrent job would race the first one over
        # the same plan_id in the URL, and chat.send_message() itself isn't safe to call from two
        # threads on the same chat session at once.
        st.caption(
            "Still working on that -- feel free to switch tabs or come back later, this will be "
            "here when you do. (Follow-up questions unlock once it's done.)"
        )
    else:
        followup = st.chat_input("Ask a follow-up — e.g. 'suggest a different restaurant' or 'what about the return trip?'")
        if followup:
            _submit_job("followup", followup, user_message=followup)
            st.rerun()

    if job_running:
        # Lightweight polling, not the actual work -- see the module comment above PlanJob. If this
        # tab disconnects right now, only this sleep-and-rerun loop dies; _run_plan_job keeps going
        # on the process-wide executor regardless, and any tab that reloads this same URL (this one
        # reconnecting, or a different one entirely) picks the job back up exactly where it left off.
        time.sleep(2)
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
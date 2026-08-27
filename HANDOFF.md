# Journey Concierge — Session Handoff

Written to hand off context to a fresh conversation. This file lives in the repo so it travels
with the code regardless of which chat session is working on it.

## What this is

A single-file Streamlit app (`app.py`) that plans road trips (highway or short in-city) using
Gemini function-calling against live Google Maps data — real routes, real place searches along
the actual polyline, real reviews/hours/parking, not the model's recalled knowledge. Originally
"Highway Pitstop Concierge," renamed to **Journey Concierge** since it works for short in-city
trips too, not just highway drives.

## Where things live

- **Code**: `C:\Users\User.DESKTOP-HU2HPHC\Desktop\aiworkshop\travelconcierge-strip\app.py` (single
  file; a sibling repo to `travelconcierge`, detached from its `origin` remote, for A/B-testing an
  alternative itinerary UI — "The Strip", see `DESIGN_CONCEPTS.md`)
- **GitHub**: `https://github.com/sireeshy/travelconcierge-strip` (owner: `sireeshy`, **not**
  `sireeshyeshwantapur` — an earlier, different GitHub account on this machine; don't push there)
- **Deployed**: `https://travelconcierge-strip-kuk8ssmfvelakuvwmwwkj2.streamlit.app/` (Streamlit
  Community Cloud, auto-deploys on push to `main`) — the original app's own deployment is separate:
  `https://travelconcierge-jpvntakkkybhcssslxgbls.streamlit.app/`
- **Local dev**: `.claude/launch.json` in the `knowledge_livestock` working directory runs it via
  the project's `.venv`, port 8510
- **Local secrets**: `.env` in the travelconcierge-strip folder holds `GOOGLE_MAPS_API_KEY`,
  `GEMINI_API_KEY`, `GOOGLE_SHEETS_CREDENTIALS_JSON`, `USAGE_SHEET_ID`, `FEEDBACK_SHEET_ID`
  (gitignored). Streamlit Cloud has its own copies set as app secrets/env vars — they are **not**
  synced automatically; update both places if a key rotates. `USAGE_SHEET_ID`/`FEEDBACK_SHEET_ID`
  point at the same two shared Google Sheets `travelconcierge` uses (see `ARCHITECTURE.md` §11) —
  both apps write into the same Sheets, tagged by `APP_VERSION`, on purpose.

## Standing instruction from the user

**Always hold `git push` until the user explicitly says to deploy.** They test the live app
manually and frequent auto-redeploys disrupt that. Commit locally, verify against the local dev
server, then wait for a "deploy" go-ahead before pushing. (This preference is also saved in
Claude's memory file `feedback_deploy_confirmation.md`.)

## Architecture in one paragraph

Three Gemini tool functions (`calculate_route_and_etas`, `search_places_along_route`,
`get_place_details_and_reviews`) plus the built-in Google Search grounding tool, driven by
automatic function calling in a single `chat.send_message()` call per turn. Tool functions read
`st.session_state.google_maps_api_key` directly rather than taking it as a parameter (keeps it out
of the tool schema the model sees). Tool functions also side-channel UI data into
`st.session_state` (`discovered_places`, `route_polyline`) since the chat response is free-form
markdown text and the *only* reliable source of real place_ids/coordinates is what the tools
actually returned — the render functions after the chat loop read from there, not from parsing the
model's prose.

## Non-obvious things worth knowing before changing this file

Most of these are also commented in place in `app.py` — this is the condensed version:

1. **Field mask names that look right but 400/404**: `routes.legs.startAddress`/`endAddress`
   (legacy Directions API naming, not in Routes API v2), `currentOpeningStatus` (real field is
   `currentOpeningHours.openNow`), and a whole nonexistent `places:searchAlongRoute` endpoint
   (route-along-route search is a *parameter* on `places:searchText`, not its own URL). All three
   bit this app in production before being found and fixed.
2. **`category` in `search_places_along_route` has no default on purpose.** It used to default to
   `"restaurant"`, which silently biased every request toward food regardless of what was asked.
3. **When the live search tool fails, the model must not fall back to naming places from its own
   knowledge/web search.** This was verified as a real, not theoretical, risk: for one test route,
   3 of 5 restaurant names Gemini recalled from raw chat (no tools) were confirmed permanently
   closed via live Places data, and one was in the wrong state entirely.
4. **`get_place_details_and_reviews` takes a list, not a single place_id.** Batching all candidate
   places into one call was the single biggest fix for the app running out of its AFC call budget
   (see `maximum_remote_calls` in the code — currently 15, SDK default is 10).
5. **The map doesn't use Google's Static Maps API.** That API needs a separate Cloud Console
   enablement most keys won't have (this project hit that "one more API to enable" wall
   repeatedly — Time Zone API, legacy Places API, Static Maps API all needed separate
   enablement that wasn't there by default). The map instead decodes the polyline by hand
   (`decode_polyline`) and draws it with `pydeck`, which ships with Streamlit — zero extra
   Google API dependency.
6. **Photos are fetched server-side, never linked as a client-visible `<img src>`.** Doing the
   latter would put the Maps API key directly in the browser's page source.
7. **Departure time is origin-local, not hardcoded IST**, resolved via geocoding the origin
   (existing Geocoding API call, no new enablement) + `timezonefinder` (offline, no Time Zone API
   needed) → `zoneinfo.ZoneInfo`, correctly handling DST for non-Indian origins too.
8. **The departure time picker is free text, not `st.time_input`.** That widget renders as a
   24-hour-only segmented picker with no way to type "630pm" — a real regression the user caught
   after an earlier redesign. Free text + quick-select buttons (Now / 1hr from now) is what's live.
9. **Streamlit's `st.markdown(unsafe_allow_html=True)` strips `onclick` and other inline event
   handler attributes** (plain `href` links survive fine). Anything needing real JS — the
   copy/share buttons — has to go through `st.iframe(html_string, ...)` instead.
10. **Widget default-value quirk**: `st.session_state[key] = value` only works if written *before*
    the widget with that `key` is instantiated in the same script run (see the date/time
    quick-select buttons — they're placed above their corresponding widget for this reason).
    Writing to a widget's key after it's been created raises.

## Known issue, unresolved

`search_places_along_route` failed once with a real Places API error on a legitimate, well-formed
request (a 360km Hyderabad→Bellary route) — a direct retry of the *identical* request immediately
succeeded. Root cause not identified; treated as transient. An error-path-only `print()` was left
in place (not removed) so a recurrence surfaces enough detail in server logs (category, polyline
length, status, body) to tell a real reproducible bug apart from another one-off. If this recurs,
start there.

## Feature ideas raised but not built

- **YouTube video/comment search** for extra verification on low-review places — would need the
  YouTube Data API v3 (new API, new quota/key management), a meaningfully bigger lift than
  anything else added this session. Flagged as needing a separate scoping discussion.
- **Concurrent tool execution** — currently every tool call in a plan runs sequentially even when
  the model requests multiple in one turn (e.g. searching food + restrooms together), which is
  part of why longer/more complex trips take noticeably longer to plan. Speeding this up would
  mean disabling automatic function calling and writing a manual tool loop with a thread pool for
  independent calls — a real architecture change, not a quick tweak.
- Multi-stop waypoints via Routes API's native `intermediates` field (currently only origin→dest).
- PDF export, Hindi/regional-language output, trip history/persistence (no DB currently), EV
  charging stops.

## Current uncommitted state (as of this handoff)

`app.py` has **uncommitted local changes** on top of the last pushed/deployed commit
(`4d84e89`, "Add proactive timing awareness and genuine concierge judgment"). Not yet pushed. The
uncommitted work adds:
- Map (`render_route_map`, pydeck + hand-decoded polyline)
- Place photos (`render_place_photos`, server-side fetch)
- Multi-stop route builder replacing the old one-navigate-link-per-place UI
  (`render_navigate_links` now lets you pick stops and get one Maps link with waypoints in order,
  ending at the real trip destination)
- Restroom search now gated to >2hr trips *or* an explicit request (was: >2hr trips only,
  regardless of ask)
- Error-handling tightened so a `search_places_along_route` failure can never trigger a fallback
  to unverified place names (see "known non-obvious things" #3 above)
- Deprecation cleanup: `use_container_width` → `width='stretch'`, `st.components.v1.html` →
  `st.iframe`
- Removed dead `RouteResponse`/`SearchPlacesResponse`/etc. Pydantic models (never actually used —
  every tool returns a plain dict) and the unused `pydantic`/`json` imports
- This documentation pass itself (in-code comments + this file)

**Next step**: verify this batch against the local dev server one more time if picking this back
up fresh, then commit (one message, or split if that reads better) and wait for the user's
explicit go-ahead to push/deploy.

## requirements.txt additions this session

`timezonefinder` (+ its bundled data package, ~52MB — confirmed it builds fine on Streamlit
Community Cloud despite the size) and `tzdata` (for `zoneinfo` on platforms without system IANA
data). `python-dateutil` was briefly removed then restored (still needed for free-text time
parsing). `pydantic` was removed (only ever used by now-deleted dead code).

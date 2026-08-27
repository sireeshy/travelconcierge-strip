# Journey Concierge — Architecture

Written for a senior engineer/architect doing a critical review, not as onboarding material. It
states design decisions plainly, including ones a reviewer would reasonably push back on. Grounded
in `app.py` as of the working tree on 2026-08-27 (2,919 lines, single file, still uncommitted as of
this writing — see `HANDOFF.md` for why: pushes to the deployed app are held for explicit
confirmation) — line references will drift as the file changes; re-verify before relying on a
specific number. For what the app actually *does*, step by step, in plain language rather than this
document's engineering-critique framing, see `PROCESS.md`.

A second, code-identical-at-fork app variant (`travelconcierge-strip`, a sibling repo, detached from
this one's `origin` remote) exists for A/B-testing an alternative itinerary UI ("The Strip" — a
route-native visualization, see `DESIGN_CONCEPTS.md`). It shares this app's Gemini tools, reliability
fixes, and both Google Sheets (§2, new) verbatim; the two currently differ only in one rendering
function and an `APP_VERSION` constant (`"original"` vs `"strip"`) tagging every logged row. This
document describes `travelconcierge/app.py`; treat `travelconcierge-strip/app.py` as the same
architecture unless told otherwise.

## 1. What this is, in one paragraph

A Streamlit app that plans road trips (highway or short in-city) by giving Gemini three custom
tools (route calculation, place search along the route, place details/reviews) plus Google's
built-in web-search tool, driven by the SDK's automatic function-calling loop inside a single
`chat.send_message()` call per user turn. The model's final answer is constrained to a JSON schema
(not free Markdown), which the app renders into either interactive Streamlit UI (the active plan) or
plain Markdown (chat history). There is no backend beyond Streamlit itself and no database — every
fact the app knows about the current session lives in `st.session_state` and is gone when that
session ends, with one narrow exception: usage and feedback events are separately mirrored to a
durable Google Sheet (§11).

## 2. System context

```
Browser
   │  WebSocket (Streamlit's own protocol)
   ▼
Streamlit server — single Python process, app.py re-executed top-to-bottom on every interaction
   │
   ├──▶ Gemini API (google-genai SDK)
   │      chat session + 3 custom tools + built-in google_search;
   │      final turn constrained by response_schema
   │
   ├──▶ Google Maps Platform
   │      Routes API · Places API (New) · Places Autocomplete · Geocoding
   │      (all four behind one GOOGLE_MAPS_API_KEY)
   │
   ├──▶ Wikipedia REST API
   │      public, no key, requires a real User-Agent header
   │
   └──▶ Google Sheets (via a service account, gspread)
          two spreadsheets, shared with travelconcierge-strip. FEEDBACK_SHEET_ID holds one tab
          (rating + comment + per-app version). USAGE_SHEET_ID has grown into a small multi-tab
          store: sheet1 is the durable usage log (mirrors usage_log.csv); toll_plazas and
          place_details_cache (new, §12) are separate tabs in the same spreadsheet, opened by name
          via gspread rather than getting their own spreadsheet IDs
```

Two API keys, both server-side environment variables, no per-user credentials:
`GOOGLE_MAPS_API_KEY` (Routes API, Places API New, Places Autocomplete, Geocoding — one key covers
all four Maps Platform products used) and `GEMINI_API_KEY`. Wikipedia's REST API needs no key but
does require a compliant `User-Agent` header — Wikimedia's CDN silently 403s generic/missing user
agents, which is exactly how this was discovered (a plain `curl` test failed first). Google Sheets
access is a third, separate credential: a service-account JSON key
(`GOOGLE_SHEETS_CREDENTIALS_JSON`, the whole key file as one env var) plus two sheet IDs
(`USAGE_SHEET_ID`, `FEEDBACK_SHEET_ID`) — see §8's new rows and §§11-12.

## 3. Runtime model — why Streamlit's execution model matters here

Streamlit re-executes the *entire* `app.py` top to bottom on every user interaction (a button
click, a text input change, a radio selection). There's no persistent request/response cycle in the
traditional sense — "state" only survives across reruns via `st.session_state`, an in-memory dict
scoped to one browser session on one server process. This shapes several decisions documented
below:

- Tool functions can't `return` data to the UI in the normal sense, because the only thing that
  crosses back from `chat.send_message()` to the render code is the model's final text. Data the UI
  actually needs (discovered place coordinates, the route polyline, place photos) is written
  directly into `st.session_state` as a side effect *inside* the tool functions themselves
  (`search_places_along_route` at `app.py:559`, `calculate_route_and_etas` at `app.py:456`).
  This is a real coupling smell — a tool function reaching into global UI state — but it's close to
  unavoidable given the framework's execution model without a much bigger architectural change (see
  §10).
- Widget default values can only be set *before* the widget with that key is instantiated in the
  same script run — the quick-select date/time buttons (`app.py:1563` on) are ordered above their
  corresponding widgets specifically because of this; writing to a widget's session-state key after
  it's been created raises. The feedback form (§11) follows the same rule when clearing itself after
  submission.
- There's no concurrency primitive available *across* AFC's own tool-call loop — the SDK invokes
  tool calls one at a time, and that remains true (see §5, §10). Within a single tool function's own
  body, though, plain `concurrent.futures.ThreadPoolExecutor` works fine and is now used in two
  places (`search_places_along_route`'s per-category fan-out, `get_place_details_and_reviews`'s
  per-place fan-out) — with one hard rule: `st.session_state` is tied to the calling thread's
  ScriptRunContext, so worker threads may only do network I/O, never touch session state directly;
  results are gathered back into plain Python values first, and every `st.session_state` write
  happens after `executor.map()` returns, on the main thread. This parallelizes each tool call's own
  network I/O; it does not change how many round trips the AFC loop makes, which turned out to be the
  actual lever (§5, §8, §10).

## 4. Component map

Everything lives in one file. Logically, it separates into six layers, in this order in the file:

| Layer | Functions | Lines (approx) |
|---|---|---|
| **Logging/observability** | `log_usage_event`, `_get_usage_worksheet`, `USAGE_LOG_HEADER`, `log_feedback`, `_get_feedback_worksheet`, `FEEDBACK_LOG_HEADER`, `timed_tool` decorator, `_api_request` (shared timeout/retry wrapper) | 41-427 |
| **Standalone data helpers** | `decode_polyline`, `get_place_predictions`, `get_timezone_for_location`, `get_wikipedia_thumbnail`, `_haversine_km`, `_route_cumulative_km`, `_distance_along_route_km` | 222-477 |
| **Reference/cached data tables** (new layer since the last full pass) | `TOLL_PLAZAS` (curated) + `_get_toll_plazas_worksheet`/`_get_learned_toll_plazas`/`_persist_learned_plazas` (Sheet-learned, additive); `RECOGNIZED_CHAINS` + `_match_recognized_chain`; `_PLACE_CACHE_FRESH_DAYS`/`_PLACE_CACHE_VERIFY_DAYS`/`_PLACE_CACHE_RATING_DRIFT_THRESHOLD` + `_get_place_cache_worksheet`/`_load_place_details_cache`/`_persist_place_details_cache` (§12) | 479-710 |
| **Gemini tool functions** (the model's only way to get real data) | `calculate_route_and_etas` (now two Routes API calls, §5 step 4), `search_places_along_route` (break-point search, §5 step 3), `get_place_details_and_reviews` (now cache-aware, §12) | 712-1617 |
| **Rendering** (illustrations, maps, photos, structured-output → UI) | `render_copy_and_share`, `render_navigate_links`, `render_route_map`, `render_place_photos`, `render_home_illustrations`, `render_region_postcards`, `render_structured_response`, `render_plan_cards`, `render_print_button`, plus the schema constants and `_format_option_*` helpers | 1619-2407 |
| **Page script** (Streamlit UI, the system prompt, the AFC loop invocation, the feedback form) | everything from `st.set_page_config` to end of file | 2408-2919 |

There is no `src/` layout, no package, no `__init__.py`, no test directory. `requirements.txt` has
8 pinned dependencies (`streamlit`, `google-genai`, `requests`, `python-dateutil`,
`python-dotenv`, `timezonefinder`, `tzdata`, `gspread`) — unchanged since the last full pass; the
reference-data and caching layer added this pass reused `gspread`, no new dependency needed. There
is also, new this pass, one standalone maintenance script outside the Streamlit app itself:
`seed_brand_outlets.py` — a one-off (safely re-runnable) crawler that pre-populates
`place_details_cache` with real outlets of the `RECOGNIZED_CHAINS` brands along the app's covered
corridors. It duplicates `get_place_details_and_reviews`'s field mask and processing logic rather
than importing `app.py` (which isn't meaningfully importable outside a Streamlit runtime — it calls
`st.set_page_config` and reads `st.session_state` at module scope) — see §12 and `PROCESS.md`.

## 5. Request lifecycle — "Plan My Trip" end to end

1. User fills the form (origin/destination via Places Autocomplete-backed selectboxes, date/time,
   quick-preference checkboxes + free-text notes) and clicks the button.
2. The click handler resets *all* session state tied to a previous plan (`chat`, `chat_messages`,
   `discovered_places`, `route_polyline`, `route_waypoints`, `latest_plan`, etc.) and sets
   `need_new_plan = True` (`app.py:2598`), then the script reruns.
3. On the rerun, a new `genai.Client` and a new `chat` session are created (or reused if one exists
   — see §7), with `system_instruction`, all three custom tools + `google_search`,
   `response_schema=CONCIERGE_RESPONSE_SCHEMA`, and `automatic_function_calling.maximum_remote_calls
   = 15`.
4. `chat.send_message(prompt)` is one blocking call. Internally, the SDK's automatic function
   calling loop runs: model requests a tool call → SDK executes the actual Python function
   synchronously → result goes back to the model → repeat, until the model produces a final text
   turn with no more tool calls. This entire loop is **sequential** — the SDK executes one tool
   call, waits for the model's next turn, then executes the next. **Measured directly** (server
   logs, timestamp-to-timestamp): a single `generateContent` round trip inside this loop costs
   **30-80+ seconds of the model's own generation/reasoning time** — the tool call it triggers, by
   contrast, costs low single-digit seconds even doing real network I/O. In other words: **total
   latency is dominated by round-trip *count*, not by anything the tool functions do** —
   parallelizing a tool function's own I/O (§3) helps a little; cutting how many `generateContent`
   round trips a plan requires helps enormously. `search_places_along_route` was changed from one
   call per stop category to one call carrying every category the plan needs (`categories:
   list[str]`, fanned out to the Places API internally via a thread pool, still a single AFC round
   trip) — the same batching principle `get_place_details_and_reviews` already applied to
   `place_ids`. Total round trips for a typical plan are still 3: `calculate_route_and_etas` →
   `search_places_along_route` (all categories at once) → `get_place_details_and_reviews` (all
   candidate place_ids at once) — but what each of those three calls actually *does* internally has
   grown substantially since the round-trip-count measurement above was taken, without changing that
   count:
   - **`calculate_route_and_etas`** (`app.py:816`) now issues **two** Routes API calls, not one. The
     first gets the route (distance, polyline, non-traffic-aware total duration, toll advisory). The
     second re-requests the same origin/destination with a handful of intermediate coordinates
     (guessed from the first call's own route geometry) passed as `intermediates`, with
     `routingPreference: TRAFFIC_AWARE`. This returns real per-leg traffic-aware duration —
     confirmed, by direct comparison against the same route's `staticDuration`, to genuinely differ
     under real traffic — which gets cumulative-summed into real ISO arrival timestamps stashed in
     `st.session_state.route_waypoints`. This exists because a flat proportional estimate ("this
     stop is 40% of the way through, so 40% of the total time") can be meaningfully wrong: a stop
     that looks like lunchtime by flat math can, under traffic, actually be reached mid-afternoon —
     directly affecting which meal-timing suggestions are even correct. Toll cost is a separate
     three-tier fallback within the same call: a curated `TOLL_PLAZAS` table (hand-researched,
     NHAI-sourced, checked against a route's polyline) → a live-scraped fallback
     (`_toll_from_live_lookup`, persisted to the `toll_plazas` Sheet tab once discovered so a
     corridor is never re-scraped) → Google's own Routes API toll estimate as a last resort, with an
     explicit "unverified" caveat, because Google's own estimate was directly verified to
     overestimate by roughly 2.7x on a real Hyderabad↔Bengaluru route against two independent
     NHAI-sourced sources.
   - **`search_places_along_route`** (`app.py:1062`) no longer runs one broad along-route query per
     category. It first decides *where* along the route to look — plain code, not AI: roughly one
     point every ~100km of driving plus the start and end, using the real traffic-timed waypoints
     from the step above when available, falling back to distance-only points otherwise — then runs
     a real, separately-scoped `locationBias` Text Search at each of those points. This replaced an
     along-route search that was observed clustering almost every result near the dense origin city
     (it has vastly more high-rated places than sparse stretches further out), which is exactly the
     generic-slop failure mode this app is designed against. Each candidate place also gets a
     `recognized_chain` check (`_match_recognized_chain`, a plain name match against
     `RECOGNIZED_CHAINS`) — a soft tone/confidence signal for the model's verdict text, explicitly
     never a substitute for a real per-place fact (see §8's row on this).
   - **`get_place_details_and_reviews`** (`app.py:1323`) is now cache-aware (§12): a place looked up
     recently is reused with zero API calls; one looked up 30-90 days ago gets one cheap recheck
     call before being trusted again; only a genuinely stale or never-seen place pays for the full
     lookup this section originally described. This tool call remains one AFC round trip regardless
     — the cache changes what happens *inside* it, not how many round trips the plan costs.
5. Each tool call is wrapped by the `@timed_tool` decorator (`app.py:362`), which posts a
   human-readable line to a live `st.status` panel (the only reason the user gets any feedback
   during that latency) and records `{name, detail, duration_s, ok}` into
   `st.session_state._tool_trace` for later logging.
6. Once the model's final turn arrives, `response_to_markdown()` (`app.py:2379`, calling
   `parse_structured_response()` at `app.py:2164`) parses it as
   JSON against the schema shape. On success, it also stashes `data['plan']` into
   `st.session_state.latest_plan` as a side effect — this is what lets the later render step build
   an interactive card UI instead of just displaying text.
7. `log_usage_event()` writes one row to `usage_log.csv`, one line to stdout, and one row to a
   shared Google Sheet via `_get_usage_worksheet()` — covering duration, tool call trace, tool error
   count, whether structured output actually parsed, `APP_VERSION` (`"original"` or `"strip"`), and
   now (§12) `places_cache_fresh`/`places_cache_verified`/`places_cache_drift` — so the place-details
   cache's actual payoff (how often a lookup was free or half-price instead of a full paid call) is
   a trackable number, not an assumed win. The Sheet write is best-effort and additive: it's wrapped
   in its own `try/except` after the local CSV write already succeeded, so a Sheets outage or
   missing credentials never blocks or breaks a plan — see §11.
8. The message is appended to `st.session_state.chat_messages`; if it was a valid plan, the index is
   recorded in `latest_plan_message_index`.
9. The render loop (`app.py:2860` on) walks `chat_messages`. The message at
   `latest_plan_message_index` renders via `render_plan_cards()` (`app.py:2322`) — real Streamlit
   widgets/containers with per-option copy/share buttons, a Google Maps place link, and a
   category-by-category stop-picker. Every other message (older plans, follow-up answers) renders
   as plain `st.markdown()` of pre-formatted text.
10. After the message loop: the route map (pydeck), place photos (fetched server-side to keep the
    Maps key off the client), the multi-stop "Get Directions" builder, and the sidebar's Wikipedia
    region imagery all render, each reading from `st.session_state`.

A follow-up message repeats steps 4-9 on the *same* `chat` object, so conversation history is
preserved by the SDK's own chat-session mechanism, not re-sent manually.

11. Unconditionally, at the very end of the script (around `app.py:2912`) — regardless of whether a
    plan was ever generated in this session — a feedback form renders: `st.feedback("thumbs")` plus
    an optional comment box, submitting to `log_feedback()` (same local-CSV-then-Sheet pattern as
    `log_usage_event`). `origin`/`destination` on that row come from `st.session_state`, so they're
    genuinely empty if no plan was triggered first in that session — that's expected behavior, not a
    bug, and was verified as such during testing.

For the plain-language version of steps 4's three tool calls — what each one is actually for and
why it's shaped this way — see `PROCESS.md`.

## 6. State management — `st.session_state` inventory

Within one browser session, there is still no database or cache beyond Streamlit's own
`@st.cache_data`/`@st.cache_resource` (place autocomplete predictions are *not* cached; timezone
resolution and Wikipedia thumbnails are, at `ttl=3600`/`ttl=86400`; the three Sheets *worksheet
handles* — usage, toll plazas, place-details cache — are `@st.cache_resource`'d per server process,
distinct from their *contents*, which are read fresh on every call, see §12). What's new since the
last full pass is that the app's *cross-session, cross-user* memory is no longer limited to
telemetry: `place_details_cache` (§12) is real, load-bearing shared state that changes what a tool
call actually does, not just an observability mirror like the usage/feedback Sheets. Within one
session, everything is still `st.session_state`, process-memory, per-browser-session, and gone on
server restart or session expiry. Key entries:

| Key | Set by | Read by | Purpose |
|---|---|---|---|
| `chat` | main script | main script | The `google.genai` chat session object — holds full conversation history server-side (in the SDK's own memory, not Streamlit's) |
| `chat_messages` | main script | render loop | Display-ready `{role, content}` list |
| `latest_plan` | `response_to_markdown` | `render_plan_cards`, `render_navigate_links` | The parsed structured JSON of the *current* plan, not its rendered text |
| `latest_plan_message_index` | main script | render loop | Which `chat_messages` index gets the rich-card treatment vs. plain Markdown |
| `discovered_places` | `search_places_along_route`, `get_place_details_and_reviews` (side effect) | `render_route_map`, `render_place_photos`, `render_navigate_links` | place_id → {name, vicinity, lat, lng, photo_name} — the only source of real coordinates/addresses, since the model's JSON text isn't guaranteed to carry them faithfully |
| `route_polyline` | `calculate_route_and_etas` (side effect) | `render_route_map` | Encoded polyline for the map |
| `route_waypoints` (new) | `calculate_route_and_etas` (side effect, from its second Routes API call) | `search_places_along_route` | Real traffic-aware {lat, lng, km_from_origin, estimated_arrival_iso} per intermediate point — what lets break-point selection use real timed waypoints instead of falling back to distance-only guessing |
| `_tool_trace` / `_progress_status` | main script, read/written by `@timed_tool` | `log_usage_event`, live status panel | Per-request instrumentation, prefixed `_` as an internal/ephemeral convention |
| `_places_api_stats` / `_routes_api_stats` | `search_places_along_route`, `get_place_details_and_reviews`, `calculate_route_and_etas` | `log_usage_event` | Per-request counters (`new`/`legacy`/`failed` tier usage, plus `cache_fresh`/`cache_verified`/`cache_verify_failed`, new §12) — same prefix convention, reset each request |
| `google_maps_api_key` / `gemini_api_key` | main script, from `os.environ` | tool functions, main script | Re-derived from env vars on every rerun (not user input anymore — see §9) |

## 7. The structured-output design

This is the most consequential design decision in the app, and the one most worth an architect's
scrutiny.

**The problem it solves:** two runs of the same route/preferences used to produce visibly different
Markdown layouts (a table one time, a numbered list the next) — the underlying content was fine,
but there was no stable *shape* the app could build any UI around.

**The mechanism:** Gemini 3 models support combining function calling with `response_schema` +
`response_mime_type="application/json"` in the same `GenerateContentConfig` — the model still calls
tools freely mid-conversation, but its final (non-tool-call) turn must conform to
`CONCIERGE_RESPONSE_SCHEMA`. `response_type` (`"plan"` vs `"answer"`) lets the same schema cover
both a full itinerary and a plain conversational reply, so a follow-up like "why did you suggest
that one?" doesn't get forced into the full itinerary shape.

**The reliability problem, and how it's handled:** Google documents this feature combination as
*preview*, not guaranteed, as of this writing. Two concrete failure modes were hit and designed
around during development:

1. **The model can simply not return valid JSON.** `parse_structured_response()`
   (`app.py:1244-1254`) tolerates a fenced ` ```json ` block and falls back to `None` on any parse
   failure; `response_to_markdown()` falls back to showing the raw text untouched. `structured_ok`
   is logged every time specifically to make silent degradation of this preview feature visible
   over time (`log_usage_event`'s docstring is explicit about this).
2. **The model can silently omit an optional field even when real data exists for it.** This was
   observed directly, not theorized: `review_recency` and `critical_review_snippet` were added as
   `nullable: true` fields with descriptive prompt instructions, and the model omitted both for
   every option across multiple real runs — including for a restaurant with a verified, real 1-star
   review sitting in the tool's own response data. The fix was moving both into the schema's
   `required` array. `required` + `nullable: true` is a real, load-bearing pattern here: it forces
   the model to make an explicit decision (a real value or an explicit `null`) rather than silently
   skipping the field. This is a strong signal that **prompt instructions alone are not reliable
   for structured-output field population; schema constraints are.** Worth generalizing that lesson
   to any future field added to this schema.

**What this buys the app:** `render_plan_cards()` can put a real Copy/Share button and a real
"View on Google Maps" link (built from a `place_id` the model is now schema-required to carry
through) on *every individual option*, and a stop-picker can offer one radio choice per category
sourced directly from what was actually presented — none of that is possible against unstructured
Markdown text.

**What it costs:** the system prompt (`app.py:2652-2789`) is now **~2,025 words** of accumulated,
failure-driven instructions — nearly double its size at the last full pass (~1,100 words) — not
written speculatively, each instruction maps to a real bug that was observed and fixed (most
recently: the `restroom` field, `recognized_chain`, and `location_text` instructions, each added
after a specific, named failure — see §8's new rows and `PROCESS.md`'s "Data integrity principles").
This is a maintenance surface: it's dense, coupled to the schema's exact field names, and every
future schema change likely needs a corresponding prompt change to stay in sync (there's no
automated check that they agree). The prompt's own growth rate is itself worth watching — doubling
in one project's lifetime, with no sign the pattern (real bug → nullable-required field → prompt
paragraph explaining it) is slowing down.

## 8. Key architectural decisions (with tradeoffs)

| Decision | Rationale | Tradeoff / risk |
|---|---|---|
| Single-file, ~2,920-line `app.py` | Started as a workshop demo; never refactored as scope grew | No module boundaries between tool functions, rendering, and page script. Everything is globally importable/mutable within the one namespace. A senior reviewer would likely ask for at minimum a `tools.py` / `render.py` / `app.py` split — the case only gets stronger as the file keeps growing (up ~49% since the last full pass on this document). |
| Session-state side-channeling from tool functions | Streamlit's execution model gives no other way to get non-text data from a blocking `chat.send_message()` call back to the UI | Tool functions have a hidden dependency on Streamlit's runtime (`st.session_state`) — they are not pure functions and can't be unit-tested without mocking Streamlit. This is also why `seed_brand_outlets.py` (§12) had to duplicate rather than import `get_place_details_and_reviews`'s logic. |
| Automatic (sequential) function calling, not a manual loop | Simpler code, fewer moving parts | The AFC loop's round trips remain sequential (verified — see §5); round-trip *count*, not per-call concurrency, is what actually drives latency. A manual loop with concurrent tool dispatch (§10 #1) would be expected to buy little beyond what call-batching already captures, since the tool calls themselves were never the bottleneck. |
| `response_schema` used to force output shape | Fixed a real, demonstrated UI-consistency problem (see §7) | Depends on an explicitly preview-labeled Google feature. Has a designed fallback path, but that fallback (`response_text` shown raw) is materially worse UX and was hit in testing (a genuine Google Routes API 500 once caused two consecutive `"answer"`-typed error explanations instead of a plan — not a structured-output failure that time, but the same code path). |
| No fallback to model's own "knowledge" when a live search tool fails | Verified real risk: recalled restaurant names from the model's training data were checked against live data and found permanently closed or in the wrong city in 3 of 5 cases tested | The user gets no recommendation at all on a tool failure, only a message to retry — a deliberate quality-over-availability tradeoff. |
| `_api_request()` — a shared timeout + short-retry wrapper for every Maps/Places tool call | Removed a real hang risk (`requests` calls previously had no `timeout=`); `search_places_along_route` was also observed, in production, failing transiently on a well-formed request that an identical immediate retry succeeded on. | Retries only 5xx / timeout / connection errors, up to 3 attempts with linear backoff (0.75s, 1.5s); 4xx is never retried (a real request problem, not transient). Adds up to ~2-3s of backoff to a call that's ultimately going to fail anyway — judged worth it against how often the transient case resolves on retry. |
| `search_places_along_route` batches every stop category into one call (`categories: list[str]`), fanned out internally via a thread pool | Each category previously cost its own AFC round trip (30-80+s of model time); one call covering N categories costs one round trip regardless of N — the single highest-leverage latency fix in this app's history. | The system prompt has to explicitly instruct "exactly once, with every category together" — a model that reverts to calling it once per category anyway (nothing enforces the batching at the schema level) loses the benefit silently, with no automated check that would catch that regression. |
| Break-point-based place search (§5 step 4), replacing one broad along-route query | An along-route query was observed clustering nearly every result near the dense origin city, since it has vastly more high-rated places than sparse stretches further out — a real, generic-slop-adjacent quality bug, not a latency one. | Break points are currently plain distance math (~100km intervals, using real traffic-timed waypoints when available) — a reasonable but arbitrary interval; nothing adapts it to genuinely uneven place density along a specific corridor. |
| `calculate_route_and_etas` makes a second Routes API call for real traffic-aware per-leg timing (`intermediates` + `TRAFFIC_AWARE`) | Directly verified that a flat proportional time estimate can meaningfully mislead meal-timing suggestions — traffic can push a stop from "looks like lunchtime" to "actually mid-afternoon." | Doubles this tool's own API call count (still one AFC round trip). The intermediate coordinates fed into the second call are themselves a guess (evenly spaced by distance along the first call's route) — the *timing* returned for those points is real and traffic-aware, but *which* points get timed isn't adaptive to where the plan will actually suggest stops. |
| `restroom_available` — Google's real per-place `restroom` field, threaded through as a required-but-nullable schema field | Root-caused a real user complaint: the app was implying restroom availability across a whole category of options ("we picked restaurants with clean restrooms") when most individual places never confirmed it — an instance of the generic-slop failure mode this app is explicitly designed against. | Google's own field can itself be incomplete (absent ≠ confirmed-absent, just unconfirmed) — the fix makes the app honest about what Google knows, not omniscient about actual restroom availability. |
| `RECOGNIZED_CHAINS` / `_match_recognized_chain` — a curated brand list, matched by plain name substring, used only as verdict-text tone/confidence context | Brand consistency is a real, legitimate signal (a Kailash Parbat branch is more likely to meet a baseline than an unknown independent restaurant) that the Places API has no field for. | Deliberately kept out of the schema as anything but text-flavoring — explicitly forbidden from being used to state or imply a real fact like restroom/hours, which the `restroom_available` incident (row above) shows is an easy failure mode to reintroduce if this boundary isn't respected in future changes. The list itself is a hand-maintained, small, geographically biased sample (South Indian/national chains + two specific fuel brands + four wayside-amenity operators) — not remotely exhaustive, and silently favors whatever chains happened to be discussed when it was built. |
| `place_details_cache` — a Sheets-backed, tiered-freshness cache for `get_place_details_and_reviews` (new, §12) | The single most expensive Places API tier (`reviews` puts every details call at Enterprise+Atmosphere pricing) was being re-paid for on every request, even for the same real place looked up minutes apart by different users. | Correctness rests on a heuristic (a cheap `businessStatus`+`rating` recheck between 30-90 days old) — a place can change in ways that heuristic doesn't catch (new hours, new phone number, restroom status flipping) without a rating or open/closed change. See §12 for the full design and §10 for the open question this raises. |
| `seed_brand_outlets.py` — a standalone, safely-re-runnable script that pre-populates the cache with `RECOGNIZED_CHAINS` outlets along the app's covered corridors (new, §12) | Rather than waiting for organic user traffic to populate the cache for well-known chains, front-load it once. | Discovery is fuzzy free-text search (`"{brand} {city}"`), not verified brand matching — a real run pulled in genuine noise (an airport, unrelated hotels, car-care shops, travel agencies) alongside genuine outlets. Harmless for correctness (every cached row is still real Google data about whatever place it actually is; `recognized_chain` re-checks the real name at use time, independent of how something was discovered) but means the cache now holds a meaningful number of places irrelevant to the brands it was seeded for. |
| API keys always from server env vars, no user-supplied key UI | The app is no longer a shared workshop deployment where attendees needed their own key/quota | Every request against the deployed app spends the *operator's* API budget. There is no per-user rate limiting, no auth, and the "Plan My Trip" button is unauthenticated and unthrottled — see §9. |
| `usage_log.csv` + stdout logging, mirrored to a shared Google Sheet | The CSV/stdout pattern is zero-infrastructure and immediate; Streamlit Community Cloud's ephemeral filesystem meant it couldn't answer "when and whether people actually use this" across restarts, which was the actual question — the Sheet write is additive, not a replacement, so local logging still works standalone. | The Sheet write is service-account-based (`gspread`), best-effort (wrapped in its own `try/except`, never blocks a plan), and adds one more external dependency + one more credential (`GOOGLE_SHEETS_CREDENTIALS_JSON`) to operate. Now shares its spreadsheet with two more growing tabs (§2, §12) — see §10's new question on whether Sheets is an adequate backend at real scale. |
| Photos fetched server-side, never client `<img src>` | Keeps `GOOGLE_MAPS_API_KEY` out of the browser's page source | One more server-side HTTP round trip per photo, and it's synchronous within the render pass (not parallelized across up to 4 photos). |
| Hand-rolled polyline decoding, no Static Maps API | Static Maps API needs separate Cloud Console enablement most keys won't have by default (this project hit that wall repeatedly with other Maps products) | pydeck + a ~20-line decode function instead of an image URL — more code, but zero extra API dependency and no enablement risk. |

## 9. Known limitations and risks (explicit, for critique)

- **No authentication, no rate limiting, no per-user cost cap.** The deployed app is public, uses
  server-side API keys for both Gemini and Google Maps Platform, and nothing stops repeated or
  automated use from consuming the operator's quota/budget. This is the single biggest operational
  risk in the current design and the most obvious thing a reviewer would flag.
- **Zero automated tests.** Every piece of functionality added or changed during this project's
  development was verified by manually driving a live browser session against the running app
  (or, for a couple of narrow cases, by hitting the real Google/Wikipedia APIs directly from a
  Python shell). There is no unit test, integration test, or CI pipeline of any kind in this repo.
- **Ephemeral conversation state; usage/feedback/place-details/toll data are now durable, everything
  else still isn't.** No database, no session persistence across a server restart — a user's
  in-progress conversation is gone if the underlying Streamlit process restarts (Streamlit Community
  Cloud does this on redeploy, and can idle-sleep the app after inactivity). Usage and feedback
  events, toll-plaza discoveries, and now (§12) place details are the exceptions: they land in
  Google Sheets, which survive restarts, alongside the still-ephemeral local CSV. A user's own
  trip/conversation history is not among these exceptions — see §10 #6.
- **The place-details cache's staleness detection is a heuristic, not a guarantee** (new, §12). An
  entry 30-90 days old is trusted again after one cheap `businessStatus`+`rating` recheck; a change
  in hours, phone number, or restroom status that doesn't move the rating or close the business
  entirely would sail through that check undetected, and the stale cached data would be served and
  presented as current. This was a deliberate cost/correctness tradeoff (see §8's row on this), not
  an oversight, but it means the cache can be a source of wrong information in a way the
  previously-always-fresh design couldn't be.
- **The name-matching-vs-place_id issue class.** One concrete instance was found and fixed (stop
  selection silently dropping a choice because the model's descriptive name didn't exactly match
  the raw Places API name — fixed by matching on `place_id` instead). The fallback path for when
  neither `place_id` nor an exact name match resolves (`app.py:793-804`, using the raw name as
  free-text Maps waypoint text) is a heuristic, not a guarantee — it wasn't a bug fix so much as a
  "fail more gracefully" measure, and it's plausible similar name-based assumptions exist elsewhere
  that haven't been exercised yet.
- **Reliability of structured output is inherently probabilistic, not deterministic.** Even with
  the `required`-field workaround in §7, this is fundamentally a language model choosing to comply
  with a schema, not a type system enforcing it at the language level. The `structured_ok` logging
  exists because of this, but nothing currently *alerts* on a degradation trend — someone has to
  read the logs.
- **The system prompt is a single ~2,025-word string with no structure-checking**, up from ~1,100
  words at the last full pass on this document. It references exact schema field names in prose
  (e.g., "set `review_recency`...", "set `restroom`..."). If the schema changes without a
  corresponding prompt update, nothing catches the drift automatically — and the prompt's growth
  rate (§7) suggests this surface keeps getting bigger, not smaller.
- **`RECOGNIZED_CHAINS` is a small, hand-picked, geographically biased list** (South Indian/national
  restaurant and cafe chains, two specific fuel brands, four wayside-amenity operators) built from
  what came up in conversation, not from any systematic survey of chains with genuinely consistent
  standards across India. It will silently under-recognize real, equally-consistent chains that
  simply weren't discussed, and the boundary keeping it from becoming a factual claim (§8) depends on
  every future prompt change respecting that boundary, not on anything schema-enforced.
- `get_place_details_and_reviews`'s per-place_id HTTP requests, and `search_places_along_route`'s
  per-category requests, are fanned out via `ThreadPoolExecutor` instead of a sequential Python
  `for` loop — a real, verified win at zero cost to correctness (worker threads do network I/O only;
  every `st.session_state` write happens back on the main thread, per §3), though secondary to the
  round-trip-count fix (§5, §8) since both were already low single-digit seconds even sequential.
- **A tool call that exhausts `_api_request`'s retries still ends the turn empty-handed for that
  category** — the system prompt tells the model to report the limitation and suggest the user try
  again, which means retyping the whole request. A UI-level "Retry" affordance that resubmits the
  identical request with one click, instead of relying on the user to retype it in chat, was
  identified but not built.
- **`google.genai.Client(api_key=...)` and the chat session are re-created on every "Plan My Trip"
  click** (not cached/reused across trips within a session, only across follow-ups within one
  trip) — cheap in practice, but worth noting as a pattern.

## 10. Open questions for review

Framed as questions a senior reviewer might reasonably raise, not settled conclusions:

1. **Should tool orchestration move off automatic function calling to a manual loop?** A manual
   loop's actual benefit would be running independent tool calls (e.g. two separate
   `generateContent`-triggered searches) concurrently — but the measured bottleneck is
   `generateContent` itself (30-80+s per round trip), not the tool calls it triggers (low
   single-digit seconds, even doing real network I/O). Concurrent tool *execution* doesn't reduce
   round-trip *count*, and round-trip count is what's actually expensive. What moved the needle,
   without touching AFC at all, was batching `search_places_along_route` to cover every category in
   one round trip (§5, §8) — cutting a 4-category plan from 150-200s to ~87s. A manual loop remains
   architecturally cleaner and worth reconsidering if the model ever needs to make tool calls whose
   inputs depend on each other in ways batching can't express, but it's not the obvious next latency
   win. The next place to look, if the ~87s floor isn't good enough (now competing against real
   added latency from cache misses paying full Sheets-write cost, and cache hits paying a
   `get_all_values()` read every call — see #8 below), is `generateContent`'s own per-call latency —
   model/thinking-budget choices, not tool architecture.
2. **Should this be split into multiple modules?** At ~2,920 lines with six identifiable layers
   (data helpers, reference/cached-data tables, Gemini tools, rendering, schema, page script), a
   `tools.py` / `rendering.py` / `schema.py` / `cache.py` / `app.py` split seems like an increasingly
   reasonable next step for maintainability, but hasn't been done — this remains a single file by
   historical accident (workshop-demo origin) more than deliberate choice. The file is now ~49%
   bigger than at the last full pass on this document (toll dual-estimate system, break-point
   search, traffic-aware waypoint timing, restroom fix, recognized-chain signal, place-details cache
   — none of it trivial); the case for splitting only gets stronger as more gets added on top of a
   single file.
3. **What's the actual cost-control story for a public, unauthenticated, server-keyed deployment?**
   Currently none. Options worth evaluating: Streamlit-level auth, a request quota, a CAPTCHA on the
   planning action, or moving to user-supplied keys again (the old behavior, removed specifically
   because it was judged unnecessary friction for what's currently a single-operator use case — that
   judgment call should be revisited if traffic/cost patterns change). The place-details cache (§12)
   is a genuine partial answer for one specific cost driver (repeat lookups of the same real place)
   but does nothing for the Gemini-side cost of a plan, which is still unbounded per request.
4. **Is `st.session_state` side-channeling (tool functions writing directly into global UI state)
   an acceptable long-term pattern, or does it need a cleaner data-flow boundary** (e.g., tool
   functions returning everything, with the render layer reading only from the model's structured
   output plus a single well-defined "tool results" accumulator, rather than tools reaching into
   `st.session_state` directly)?
5. **Does the structured-output reliability pattern (nullable-but-required fields) generalize
   safely as the schema grows**, or does it need a more systematic approach — e.g., a schema
   validation/linting step that checks every prompt reference to a field name against the actual
   schema, to catch the two from drifting apart? The pattern has now been applied at least three
   times (`review_recency`/`critical_review_snippet`, `location_text`, `restroom`) — a real, repeated
   pattern at this point, not a one-off.
6. **Is there a real need for persistence** (saved trips, history across sessions, a lightweight
   database) now that the app has grown well past its original single-shot-demo scope, or does the
   ephemeral, single-session model still match actual usage? Partially answered for one slice of
   "persistence" — usage, feedback, toll, and now place-details data all survive restarts via Sheets
   (§§11-12) — but a user's own trip/conversation history is still gone the moment their session
   ends; whether that gap matters depends on data neither app currently collects (repeat-usage
   patterns).
7. **Is a shared Google Sheet, written to via a service account with no request-level auth, an
   adequate backend at any real scale?** It works today because traffic is low and single-operator.
   `gspread`'s per-request Sheets API call adds real latency to every feedback submission and every
   plan/follow-up (mitigated for usage logging by writing local-CSV first and treating the Sheet
   write as best-effort, per §8) — this doesn't scale to high request volume the way a real
   event-logging backend would, and there's no schema migration story if a column needs to change
   once both apps and their historical rows already depend on the current shape. This question now
   applies more broadly than it used to: `USAGE_SHEET_ID`'s spreadsheet holds three growing tabs
   (usage log, toll plazas, place-details cache), and the last of those (§12) is no longer just
   telemetry — it's read on the hot path of every `get_place_details_and_reviews` call
   (`_load_place_details_cache`'s `sheet.get_all_values()`), so a slow or rate-limited Sheets API
   response now directly adds to plan latency in a way a pure logging write never did.
8. **Is the place-details cache's freshness heuristic (§9, §12) good enough, or does it need
   per-field staleness instead of one blanket age?** Right now, a place's rating and open/closed
   status get rechecked at 30-90 days; hours, phone number, and restroom status do not, and could be
   quietly wrong for up to 90 days before a full refresh happens. Whether that's an acceptable
   tradeoff likely depends on data this app doesn't currently collect: how often those specific
   fields actually change in the wild.
9. **Was seeding the cache with fuzzy-matched brand searches (`seed_brand_outlets.py`, §12) the
   right tradeoff?** It found genuine chain outlets, but also real noise (an airport, unrelated
   hotels, car-care shops) sitting in the same cache table under the same schema, distinguishable
   from genuine finds only by whether a later `recognized_chain` check happens to match their real
   name. Nothing currently measures what fraction of the 339 seeded rows are actually useful.

## 11. Feedback and usage telemetry (Google Sheets)

The reason both apps share `APP_VERSION`. Two independent Google Sheets (spreadsheets), one shared
service account (`gspread.service_account_from_dict`, credentials passed whole as
`GOOGLE_SHEETS_CREDENTIALS_JSON`, not a file path — this app has no local file to point at on
Streamlit Community Cloud). `USAGE_SHEET_ID`'s spreadsheet has since grown two more tabs alongside
the usage log described below — `toll_plazas` (§5 step 4, §8) and `place_details_cache` (§12, new) —
opened by worksheet name within the same spreadsheet rather than getting their own IDs:

- **Usage log** (`USAGE_SHEET_ID`) — every `log_usage_event()` call, mirroring `usage_log.csv`'s
  columns plus `version`. Exists specifically because the CSV can't answer "when and whether people
  actually use this" across a restart (§9).
- **Feedback log** (`FEEDBACK_SHEET_ID`) — every feedback-form submission (`log_feedback()`):
  timestamp, version, thumbs rating, free-text comment, origin/destination (empty if no plan was
  triggered in that session — expected, not a bug). The form itself always renders at the bottom of
  the page (§5, step 11), independent of whether a plan exists.

Both `_get_usage_worksheet()` / `_get_feedback_worksheet()` follow the same shape:
`@st.cache_resource` (a live API client, not serializable data, so `cache_data` would be wrong),
return `None` — falling back to local-only logging — when the env vars aren't set or the Sheet can't
be reached, and self-heal an empty/new Sheet by writing the header row on first successful open.
Every write is `try/except`-wrapped independently of the local CSV write, so a Sheets-side problem
degrades to "no durable copy this time," never a broken plan or a lost local log entry.

**Why both app variants write into the *same* two Sheets rather than each having its own:** the
explicit purpose of running `travelconcierge` and `travelconcierge-strip` side by side is comparing
them — "which layout gets used more, and rated better" is the question `APP_VERSION` exists to
answer, and that comparison only works if both variants' rows sit in one place to filter/pivot on.
The same reasoning extends to `place_details_cache` (§12) — a place a `travelconcierge-strip` user
looked up should be reusable for a `travelconcierge` user asking about the same place, and vice
versa; the cache is deliberately keyed by real-world `place_id`, not scoped per app variant.

## 12. Place-details caching (new)

`get_place_details_and_reviews` — the single most expensive tool call in the app (its field mask
includes `reviews`, which puts every request at Google's Enterprise+Atmosphere Place Details
pricing tier) — is now backed by a cross-session, cross-user cache keyed by `place_id`, stored in
the `place_details_cache` tab of `USAGE_SHEET_ID`'s spreadsheet (`_get_place_cache_worksheet`,
`_load_place_details_cache`, `_persist_place_details_cache`, `app.py:618-710`).

**Freshness policy** (`_PLACE_CACHE_FRESH_DAYS = 30`, `_PLACE_CACHE_VERIFY_DAYS = 90`,
`_PLACE_CACHE_RATING_DRIFT_THRESHOLD = 0.3`):
- **Under 30 days old** — reused outright, zero API calls.
- **30-90 days old** — one cheap recheck (`businessStatus`+`rating` only, a cheaper SKU tier than
  the full lookup) run in parallel across every entry in this bucket; if the business is no longer
  `OPERATIONAL` or the rating has moved by more than 0.3, it's treated as stale and falls through to
  a full refetch, otherwise the cached data is reused and `cached_at` is refreshed so it doesn't get
  re-verified again immediately.
- **Over 90 days, or never cached** — the full fetch this tool always did, then cached for next
  time.

**What's cached is the already-processed, model-facing dict** (reviews sliced to 3, restroom text
resolved, parking list built, etc. — the exact shape `get_place_details_and_reviews` returns), not
the raw Places API response — factored out into a shared `_process()` closure specifically so a
cache write and a cache hit can never drift into returning different shapes for the same place. A
non-model field (`photo_name`, for `render_place_photos`) rides alongside it in the same JSON blob
per row and is stripped back out before anything reaches the model.

**Observability**: `_places_api_stats` (already used for `new`/`legacy`/`failed` tier tracking, §6)
gained `cache_fresh`/`cache_verified`/`cache_verify_failed` counters, logged and written to the
usage Sheet exactly like the existing tier counters (§5 step 7) — deliberate, so the cache's real
payoff is measurable rather than assumed.

**Seeding**: `seed_brand_outlets.py` (repo root, not part of the Streamlit app) pre-populates this
cache with real outlets of `RECOGNIZED_CHAINS` brands along the corridors `TOLL_PLAZAS` already
covers, using fuzzy `"{brand} {city}"` Text Search per brand per city, deduped by `place_id`. A real
run found 340 unique candidates and cached 339 (one API failure); a meaningful fraction of those are
search noise, not genuine brand matches (see §8's row and §10 #9) — harmless for correctness, since
`recognized_chain` matching happens against each place's real name at use time regardless of why it
was originally cached, but real dead weight in the cache table.

See §8 for the tradeoffs this design accepts, and §9/§10 for the specific correctness and scale
risks it introduces.

## Related documents

- [PROCESS.md](PROCESS.md) — plain-language reference for how the app plans a trip end to end; the
  fixed starting point future planning-logic decisions should follow or explicitly amend
- [DESIGN.md](DESIGN.md) — current visual identity (colors, fonts, iconography), grounded in live code
- [DESIGN_CONCEPTS.md](DESIGN_CONCEPTS.md) — unshipped UI redesign explorations
- [HANDOFF.md](HANDOFF.md) — session-to-session narrative handoff notes, non-obvious gotchas

# Journey Concierge — Architecture

Written for a senior engineer/architect doing a critical review, not as onboarding material. It
states design decisions plainly, including ones a reviewer would reasonably push back on. Grounded
in `app.py` as of the working tree following commit `2db7039` (1,957 lines, single file — reliability,
latency, and telemetry changes below are not yet committed as of this writing) — line references will
drift as the file changes; re-verify before relying on a specific number.

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
          two sheets, shared with travelconcierge-strip: one durable usage log
          (mirrors usage_log.csv), one feedback log (rating + comment + per-app version)
```

Two API keys, both server-side environment variables, no per-user credentials:
`GOOGLE_MAPS_API_KEY` (Routes API, Places API New, Places Autocomplete, Geocoding — one key covers
all four Maps Platform products used) and `GEMINI_API_KEY`. Wikipedia's REST API needs no key but
does require a compliant `User-Agent` header (`app.py:186-189`) — Wikimedia's CDN silently 403s
generic/missing user agents, which is exactly how this was discovered (a plain `curl` test failed
first). Google Sheets access is a third, separate credential: a service-account JSON key
(`GOOGLE_SHEETS_CREDENTIALS_JSON`, the whole key file as one env var) plus two sheet IDs
(`USAGE_SHEET_ID`, `FEEDBACK_SHEET_ID`) — see §8's new row and §11.

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

Everything lives in one file. Logically, it separates into five layers, in this order in the file:

| Layer | Functions | Lines (approx) |
|---|---|---|
| **Logging/observability** | `log_usage_event`, `_get_usage_worksheet`, `USAGE_LOG_HEADER`, `log_feedback`, `_get_feedback_worksheet`, `FEEDBACK_LOG_HEADER`, `timed_tool` decorator, `_api_request` (shared timeout/retry wrapper, new) | 22-186, 314-391 |
| **Standalone data helpers** | `decode_polyline`, `get_place_predictions`, `get_timezone_for_location`, `get_wikipedia_thumbnail` | 188-303 |
| **Gemini tool functions** (the model's only way to get real data) | `calculate_route_and_etas`, `search_places_along_route`, `get_place_details_and_reviews` | 393-699 |
| **Rendering** (illustrations, maps, photos, structured-output → UI) | `render_copy_and_share`, `render_navigate_links`, `render_route_map`, `render_place_photos`, `render_home_illustrations`, `render_region_postcards`, `render_structured_response`, `render_plan_cards`, `render_print_button`, plus the schema constants and `_format_option_*` helpers | 701-1479 |
| **Page script** (Streamlit UI, the system prompt, the AFC loop invocation, the feedback form) | everything from `st.set_page_config` to end of file | 1480-1957 |

There is no `src/` layout, no package, no `__init__.py`, no test directory. `requirements.txt` has
8 pinned dependencies (`streamlit`, `google-genai`, `requests`, `python-dateutil`,
`python-dotenv`, `timezonefinder`, `tzdata`, `gspread` — added this pass for the Sheets integration).

## 5. Request lifecycle — "Plan My Trip" end to end

1. User fills the form (origin/destination via Places Autocomplete-backed selectboxes, date/time,
   quick-preference checkboxes + free-text notes) and clicks the button.
2. The click handler (`app.py:1640-1670`) resets *all* session state tied to a previous plan
   (`chat`, `chat_messages`, `discovered_places`, `route_polyline`, `latest_plan`, etc.) and sets
   `need_new_plan = True`, then the script reruns.
3. On the rerun, a new `genai.Client` and a new `chat` session are created (or reused if one exists
   — see §7), with `system_instruction`, all three custom tools + `google_search`,
   `response_schema=CONCIERGE_RESPONSE_SCHEMA`, and `automatic_function_calling.maximum_remote_calls
   = 15`.
4. `chat.send_message(prompt)` is one blocking call. Internally, the SDK's automatic function
   calling loop runs: model requests a tool call → SDK executes the actual Python function
   synchronously → result goes back to the model → repeat, until the model produces a final text
   turn with no more tool calls. This entire loop is **sequential** — the SDK executes one tool
   call, waits for the model's next turn, then executes the next. **Measured directly** (server
   logs, timestamp-to-timestamp) during this project's development: a single `generateContent`
   round trip inside this loop costs **30-80+ seconds of the model's own generation/reasoning time**
   — the tool call it triggers, by contrast, costs low single-digit seconds even doing real network
   I/O (a 4-category `search_places_along_route` call, parallelized across categories, measured
   3.1s; the whole `get_place_details_and_reviews` batch for 9 places measured 0.8s). In other
   words: **total latency is dominated by round-trip *count*, not by anything the tool functions do**
   — parallelizing a tool function's own I/O (§3) helps a little; cutting how many
   `generateContent` round trips a plan requires helps enormously. This is why
   `search_places_along_route` was changed (this pass) from one call per stop category to one call
   carrying every category the plan needs (`categories: list[str]`, fanned out to the Places API
   internally via a thread pool, still a single AFC round trip) — the same batching principle
   `get_place_details_and_reviews` already applied to `place_ids`. Measured before/after on the same
   route: a 4-category plan (food, fuel, restroom, snacks) that would previously have cost 5 AFC
   round trips (route + 4 separate category searches + details) and ran 150-200s now completes in
   **~87s** covering the same 4 categories in one search round trip. Total round trips for a typical
   plan are now 3: `calculate_route_and_etas` → `search_places_along_route` (all categories at once)
   → `get_place_details_and_reviews` (all candidate place_ids at once).
5. Each tool call is wrapped by the `@timed_tool` decorator (`app.py:328-360`), which posts a
   human-readable line to a live `st.status` panel (the only reason the user gets any feedback
   during that latency) and records `{name, detail, duration_s, ok}` into
   `st.session_state._tool_trace` for later logging.
6. Once the model's final turn arrives, `response_to_markdown()` (`app.py:1451` on, calling
   `parse_structured_response()` at `app.py:1244`) parses it as
   JSON against the schema shape. On success, it also stashes `data['plan']` into
   `st.session_state.latest_plan` as a side effect — this is what lets the later render step build
   an interactive card UI instead of just displaying text.
7. `log_usage_event()` writes one row to `usage_log.csv`, one line to stdout, and (new) one row to
   a shared Google Sheet via `_get_usage_worksheet()` — covering duration, tool call trace, tool
   error count, whether structured output actually parsed, and now `APP_VERSION` (`"original"` or
   `"strip"`). The Sheet write is best-effort and additive: it's wrapped in its own `try/except`
   after the local CSV write already succeeded, so a Sheets outage or missing credentials never
   blocks or breaks a plan — see §11.
8. The message is appended to `st.session_state.chat_messages`; if it was a valid plan, the index is
   recorded in `latest_plan_message_index`.
9. The render loop (`app.py:1901` on) walks `chat_messages`. The message at
   `latest_plan_message_index` renders via `render_plan_cards()` — real Streamlit
   widgets/containers with per-option copy/share buttons, a Google Maps place link, and a
   category-by-category stop-picker. Every other message (older plans, follow-up answers) renders
   as plain `st.markdown()` of pre-formatted text.
10. After the message loop: the route map (pydeck), place photos (fetched server-side to keep the
    Maps key off the client), the multi-stop "Get Directions" builder, and the sidebar's Wikipedia
    region imagery all render, each reading from `st.session_state`.

A follow-up message repeats steps 4-9 on the *same* `chat` object, so conversation history is
preserved by the SDK's own chat-session mechanism, not re-sent manually.

11. Unconditionally, at the very end of the script (`app.py:1940` on) — regardless of whether a plan
    was ever generated in this session — a feedback form renders: `st.feedback("thumbs")` plus an
    optional comment box, submitting to `log_feedback()` (same local-CSV-then-Sheet pattern as
    `log_usage_event`). `origin`/`destination` on that row come from `st.session_state`, so they're
    genuinely empty if no plan was triggered first in that session — that's expected behavior, not a
    bug, and was verified as such during testing.

## 6. State management — `st.session_state` inventory

There is no database and no cache layer beyond Streamlit's own `@st.cache_data` (used for three
pure external lookups: place autocomplete predictions are *not* cached, but timezone resolution and
Wikipedia thumbnails are, at `ttl=3600` and `ttl=86400` respectively). Everything else is
`st.session_state`, which is process-memory, per-browser-session, and gone on server restart or
session expiry. Key entries:

| Key | Set by | Read by | Purpose |
|---|---|---|---|
| `chat` | main script | main script | The `google.genai` chat session object — holds full conversation history server-side (in the SDK's own memory, not Streamlit's) |
| `chat_messages` | main script | render loop | Display-ready `{role, content}` list |
| `latest_plan` | `response_to_markdown` | `render_plan_cards`, `render_navigate_links` | The parsed structured JSON of the *current* plan, not its rendered text |
| `latest_plan_message_index` | main script | render loop | Which `chat_messages` index gets the rich-card treatment vs. plain Markdown |
| `discovered_places` | `search_places_along_route`, `get_place_details_and_reviews` (side effect) | `render_route_map`, `render_place_photos`, `render_navigate_links` | place_id → {name, vicinity, lat, lng, photo_name} — the only source of real coordinates/addresses, since the model's JSON text isn't guaranteed to carry them faithfully |
| `route_polyline` | `calculate_route_and_etas` (side effect) | `render_route_map` | Encoded polyline for the map |
| `_tool_trace` / `_progress_status` | main script, read/written by `@timed_tool` | `log_usage_event`, live status panel | Per-request instrumentation, prefixed `_` as an internal/ephemeral convention |
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

**What it costs:** the system prompt (`app.py:1724` on) is ~1,100 words of accumulated,
failure-driven instructions — not written speculatively, each numbered point in the comment above
it maps to a real bug that was observed and fixed. This is a maintenance surface: it's dense,
coupled to the schema's exact field names, and every future schema change likely needs a
corresponding prompt change to stay in sync (there's no automated check that they agree).

## 8. Key architectural decisions (with tradeoffs)

| Decision | Rationale | Tradeoff / risk |
|---|---|---|
| Single-file, ~1,950-line `app.py` | Started as a workshop demo; never refactored as scope grew | No module boundaries between tool functions, rendering, and page script. Everything is globally importable/mutable within the one namespace. A senior reviewer would likely ask for at minimum a `tools.py` / `render.py` / `app.py` split. |
| Session-state side-channeling from tool functions | Streamlit's execution model gives no other way to get non-text data from a blocking `chat.send_message()` call back to the UI | Tool functions have a hidden dependency on Streamlit's runtime (`st.session_state`) — they are not pure functions and can't be unit-tested without mocking Streamlit. |
| Automatic (sequential) function calling, not a manual loop | Simpler code, fewer moving parts | The AFC loop's round trips remain sequential (verified — see §5), but this pass established that round-trip *count*, not per-call concurrency, is what actually drives latency: batching `search_places_along_route`'s categories (below) cut a 4-category plan from 150-200s to ~87s without touching AFC at all. A manual loop with concurrent tool dispatch (§10 #1) would now be expected to buy little beyond what batching already captured, since the tool calls themselves were never the bottleneck. |
| `response_schema` used to force output shape | Fixed a real, demonstrated UI-consistency problem (see §7) | Depends on an explicitly preview-labeled Google feature. Has a designed fallback path, but that fallback (`response_text` shown raw) is materially worse UX and was hit in testing (a genuine Google Routes API 500 once caused two consecutive `"answer"`-typed error explanations instead of a plan — not a structured-output failure that time, but the same code path). |
| No fallback to model's own "knowledge" when a live search tool fails | Verified real risk: recalled restaurant names from the model's training data were checked against live data and found permanently closed or in the wrong city in 3 of 5 cases tested | The user gets no recommendation at all on a tool failure, only a message to retry — a deliberate quality-over-availability tradeoff. Mitigated this pass (below) by making that failure rarer, not by relaxing this rule. |
| `_api_request()` — a shared timeout + short-retry wrapper for all three Maps/Places tool calls (new) | None of the three had a `requests` `timeout=` before this pass — a stuck/slow call could hang a plan indefinitely with no way out. `search_places_along_route` was also observed, in production, failing transiently on a well-formed request that an identical immediate retry succeeded on — previously the only retry was the user manually re-asking. | Retries only 5xx / timeout / connection errors, up to 3 attempts with linear backoff (0.75s, 1.5s); 4xx is never retried (a real request problem, not transient). Adds up to ~2-3s of backoff to a call that's ultimately going to fail anyway — judged worth it against how often the transient case resolves on retry. |
| `search_places_along_route` batches every stop category into one call (`categories: list[str]`), fanned out internally via a thread pool | Each category previously cost its own AFC round trip (30-80+s of model time); one call covering N categories costs one round trip regardless of N. The single highest-leverage latency fix identified this pass (see §5's measured before/after). | The system prompt now has to explicitly instruct "exactly once, with every category together" — a model that reverts to calling it once per category anyway (nothing enforces the batching at the schema level) loses the benefit silently, with no automated check that would catch that regression. |
| API keys always from server env vars, no user-supplied key UI | The app is no longer a shared workshop deployment where attendees needed their own key/quota | Every request against the deployed app spends the *operator's* API budget. There is no per-user rate limiting, no auth, and the "Plan My Trip" button is unauthenticated and unthrottled — see §9. |
| `usage_log.csv` + stdout logging, now mirrored to a shared Google Sheet (new, §11) | The CSV/stdout pattern is zero-infrastructure and immediate; Streamlit Community Cloud's ephemeral filesystem meant it couldn't answer "when and whether people actually use this" across restarts, which was the actual question — the Sheet write is additive, not a replacement, so local logging still works standalone. | The Sheet write is service-account-based (`gspread`), best-effort (wrapped in its own `try/except`, never blocks a plan), and adds one more external dependency + one more credential (`GOOGLE_SHEETS_CREDENTIALS_JSON`) to operate. |
| Photos fetched server-side, never client `<img src>` | Keeps `GOOGLE_MAPS_API_KEY` out of the browser's page source | One more server-side HTTP round trip per photo, and it's synchronous within the render pass (not parallelized across up to 4 photos) — unlike `get_place_details_and_reviews`, this loop was not parallelized this pass. |
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
- **Ephemeral conversation state; usage/feedback are now durable, everything else still isn't.** No
  database, no session persistence across a server restart — a user's in-progress conversation is
  gone if the underlying Streamlit process restarts (Streamlit Community Cloud does this on
  redeploy, and can idle-sleep the app after inactivity). Usage and feedback events are the one
  exception (§11, new): they now also land in a Google Sheet, which survives restarts, alongside the
  still-ephemeral local CSV.
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
- **The system prompt is a single ~1,100-word string with no structure-checking.** It references
  exact schema field names in prose (e.g., "set `review_recency`..."). If the schema changes without
  a corresponding prompt update, nothing catches the drift automatically.
- **Resolved this pass:** `get_place_details_and_reviews`'s per-place_id HTTP requests, and
  `search_places_along_route`'s per-category requests, are now fanned out via
  `ThreadPoolExecutor` instead of a sequential Python `for` loop. Measured impact was secondary to
  the round-trip-count fix (§5, §8) — both were already low single-digit seconds even sequential —
  but it's a real, verified win at zero cost to correctness (worker threads do network I/O only;
  every `st.session_state` write happens back on the main thread, per §3).
- **A tool call that exhausts `_api_request`'s retries still ends the turn empty-handed for that
  category** — the system prompt tells the model to report the limitation and suggest the user try
  again, which means retyping the whole request. A UI-level "Retry" affordance that resubmits the
  identical request with one click, instead of relying on the user to retype it in chat, was
  identified but not built this pass.
- **`google.genai.Client(api_key=...)` and the chat session are re-created on every "Plan My Trip"
  click** (not cached/reused across trips within a session, only across follow-ups within one
  trip) — cheap in practice, but worth noting as a pattern.

## 10. Open questions for review

Framed as questions a senior reviewer might reasonably raise, not settled conclusions:

1. **Should tool orchestration move off automatic function calling to a manual loop?** Previously
   framed as the single highest-leverage latency change; this pass's measurements revise that. A
   manual loop's actual benefit would be running independent tool calls (e.g. two separate
   `generateContent`-triggered searches) concurrently — but the measured bottleneck is
   `generateContent` itself (30-80+s per round trip), not the tool calls it triggers (low
   single-digit seconds, even doing real network I/O). Concurrent tool *execution* doesn't reduce
   round-trip *count*, and round-trip count is what's actually expensive. What did move the needle,
   without touching AFC at all, was batching `search_places_along_route` to cover every category in
   one round trip (§5, §8) — cutting a 4-category plan from 150-200s to ~87s. A manual loop remains
   architecturally cleaner and worth reconsidering if the model ever needs to make tool calls whose
   inputs depend on each other in ways batching can't express, but it's no longer the obvious next
   latency win it looked like before this data existed. The next place to look, if the ~87s current
   floor isn't good enough, is `generateContent`'s own per-call latency — model/thinking-budget
   choices, not tool architecture.
2. **Should this be split into multiple modules?** At ~1,950 lines with five identifiable layers
   (data helpers, Gemini tools, rendering, schema, page script), a `tools.py` / `rendering.py` /
   `schema.py` / `app.py` split seems like a reasonable next step for maintainability, but hasn't
   been done — this remains a single file by historical accident (workshop-demo origin) more than
   deliberate choice. The file has grown by ~200 lines this pass alone (reliability wrapper,
   category-batched search, feedback form, two Sheets integrations); the case for splitting only
   gets stronger as more gets added on top of a single file.
3. **What's the actual cost-control story for a public, unauthenticated, server-keyed deployment?**
   Currently none. Options worth evaluating: Streamlit-level auth, a request quota, a CAPTCHA on the
   planning action, or moving to user-supplied keys again (which was the old behavior, removed this
   session specifically because it was judged unnecessary friction for what's currently a
   single-operator use case — that judgment call should be revisited if traffic/cost patterns
   change).
4. **Is `st.session_state` side-channeling (tool functions writing directly into global UI state)
   an acceptable long-term pattern, or does it need a cleaner data-flow boundary** (e.g., tool
   functions returning everything, with the render layer reading only from the model's structured
   output plus a single well-defined "tool results" accumulator, rather than tools reaching into
   `st.session_state` directly)?
5. **Does the structured-output reliability pattern (nullable-but-required fields) generalize
   safely as the schema grows**, or does it need a more systematic approach — e.g., a schema
   validation/linting step that checks every prompt reference to a field name against the actual
   schema, to catch the two from drifting apart?
6. **Is there a real need for persistence** (saved trips, history across sessions, a lightweight
   database) now that the app has grown well past its original single-shot-demo scope, or does the
   ephemeral, single-session model still match actual usage? Partially answered this pass for one
   slice of "persistence" — usage and feedback events now survive restarts via Sheets (§11) — but a
   user's own trip/conversation history is still gone the moment their session ends; whether that
   gap matters depends on data neither app currently collects (repeat-usage patterns).
7. **Is a shared Google Sheet, written to via a service account with no request-level auth, an
   adequate telemetry backend at any real scale** (new)? It works today because traffic is low and
   single-operator. `gspread`'s per-request Sheets API call adds real latency to every feedback
   submission and every plan/follow-up (mitigated for usage logging by writing local-CSV first and
   treating the Sheet write as best-effort, per §8) — this doesn't scale to high request volume the
   way a real event-logging backend would, and there's no schema migration story if a column needs
   to change once both apps and their historical rows already depend on the current shape.

## 11. Feedback and usage telemetry (Google Sheets)

Added this pass, and the reason both apps now share `APP_VERSION`. Two independent Google Sheets,
one shared service account (`gspread.service_account_from_dict`, credentials passed whole as
`GOOGLE_SHEETS_CREDENTIALS_JSON`, not a file path — this app has no local file to point at on
Streamlit Community Cloud):

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

## Related documents

- [DESIGN.md](DESIGN.md) — current visual identity (colors, fonts, iconography), grounded in live code
- [DESIGN_CONCEPTS.md](DESIGN_CONCEPTS.md) — unshipped UI redesign explorations
- [HANDOFF.md](HANDOFF.md) — session-to-session narrative handoff notes, non-obvious gotchas

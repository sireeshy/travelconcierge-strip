# Journey Concierge — Process

This is the reference for how the app actually works, in plain terms — not an engineering critique
(see `ARCHITECTURE.md` for that) and not a UI spec (see `DESIGN.md`). It exists so future design
conversations have a fixed starting point instead of re-deriving the pipeline from scratch each
time.

**How to use this document:** any future decision about how the app plans a trip should either (a)
follow the process described here, or (b) explicitly change it — and if it changes it, update this
file in the same pass. Don't let the real pipeline and this document drift apart. Ideas that were
discussed and *not* built are kept in their own section below, clearly separated from what's
actually live, so they aren't mistaken for current behavior in a later conversation.

Grounded in `travelconcierge/app.py` as of 2026-08-27 (updated same day to reflect the 2-hour-segment
redesign). `travelconcierge-strip/app.py` mirrors this pipeline exactly (see `ARCHITECTURE.md`
§system context) — the two differ only in UI rendering.

## The current pipeline

1. **Place confirmation.** The user types an origin/destination; Google's own Places Autocomplete
   resolves it to a real, specific place before anything else happens. This removes ambiguity about
   which "Bengaluru" or which branch of a chain is meant.

2. **Route calculation** (`calculate_route_and_etas`) — two Routes API calls, not one:
   - **Call 1** gets the actual route: distance, total duration, the polyline (the road's exact
     shape), and toll info.
   - **Call 2** re-requests the same origin/destination but with a handful of intermediate
     coordinates (guessed from the route's own geometry) passed as `intermediates`, with
     `routingPreference: TRAFFIC_AWARE`. This returns real per-leg traffic-aware duration, which
     gets cumulative-summed into real ISO arrival timestamps at each waypoint
     (`route_waypoints` / `waypoints` in the tool's return). This exists because a simple
     proportional estimate ("this stop is 40% of the way through, so 40% of the total time") can be
     meaningfully wrong under real traffic — a stop that looks like lunchtime by flat math can
     actually be reached mid-afternoon.
   - Toll cost is a dual estimate: a curated `TOLL_PLAZAS` table (hand-researched, NHAI-sourced,
     revised every ~6 months) is checked first, then a live-scraped fallback
     (`_toll_from_live_lookup`, persisted to a Sheet once discovered so it's not re-scraped every
     time), then Google's own estimate as a last resort with an explicit "unverified" caveat —
     because Google's own toll estimate was directly verified to overestimate by ~2.7x on a real
     route.

3. **Break-point selection** (inside `search_places_along_route`) — plain code, not AI. Roughly one
   point every **~2 hours of actual driving** (time-based, not distance-based — a slow ghat-heavy
   stretch gets more break points than a flat highway stretch covering the same km in less time),
   plus the start and end, using the real traffic-timed waypoints from step 2 when available (falls
   back to a distance approximation, assuming ~55 km/h average, if not). This runs before any place
   search happens.

4. **Place discovery** (`search_places_along_route`) — a real, separately-scoped Text Search
   (`locationBias`, not a single broad along-route query) at each break point, batched into one tool
   call. This replaced an earlier design that searched the whole route in one query and let Google's
   own ranking decide what to show — that clustered nearly every result near the dense origin city.
   Every plan **always** searches four base categories at every break point — restaurants, fuel
   stations, hospitals/emergency care, and tea/snacks — regardless of what the user explicitly asked
   for, on top of anything else the trip specifically needs (a pharmacy, an ATM, a grocery stop).
   Hospital options additionally carry a phone number (Google's own `nationalPhoneNumber`, already
   fetched by step 5 but not previously surfaced) — a hospital listing with no way to call it isn't
   actually useful in an emergency. Each returned candidate also gets a `recognized_chain` check
   (see "Data integrity principles" below).

5. **Place details** (`get_place_details_and_reviews`) — full details (hours, reviews, phone,
   parking, restroom) for the specific candidates chosen in step 4, batched into one call. Backed by
   a cache (see "Supporting infrastructure" below) so repeat lookups of the same real place don't
   re-pay for the same data.

6. **Itinerary generation** — Gemini writes the actual plan from everything steps 2-5 returned. Its
   final turn is constrained to a JSON schema (`CONCIERGE_RESPONSE_SCHEMA`), not free text, so the
   app can render a stable card UI instead of parsing inconsistent Markdown.

7. **Rendering** — the structured plan becomes real Streamlit cards: trip overview, itinerary
   timeline, per-category stop options with copy/share/navigate buttons, the route map, photos.
   India's emergency numbers (112 unified, 100 police, 101 fire, 108 ambulance) render as a fixed
   block on every plan — written directly by code (`EMERGENCY_NUMBERS`), not asked of the model or
   routed through the schema at all, since static data has no business depending on a language model
   to reproduce it correctly every time.

## Data integrity principles

These aren't incidental details — they're the throughline behind most of this session's fixes, and
should apply to anything added to the plan going forward:

- **A per-place claim needs per-place evidence.** The `restroom_available` field exists because the
  app used to imply restroom availability across a whole category of options (e.g. "we picked
  restaurants with clean restrooms") without any individual place actually confirming it. Google's
  real `restroom` field is now read and stated per place; the model is explicitly told never to
  generalize from category, cuisine, or a restroom search run elsewhere on the route.
- **Brand recognition is tone, never a fact.** `RECOGNIZED_CHAINS` (Kailash Parbat, Saravana Bhavan,
  Jio-bp, Cube Stop, etc. — see the list in `app.py`) lets the model note that a place is a
  recognized chain with generally consistent standards, but this must never stand in for a real
  field like restroom availability or hours when that field says "not confirmed."
- **Real traffic-aware timing over flat proportional math**, for the same reason as step 2 above —
  every itinerary time and meal-window judgment uses the real waypoint ISO timestamps, not a
  fraction of total trip duration.
- **A tool failure means no recommendation, not a guess from the model's training data.** Verified
  directly: restaurant names recalled from the model's own memory, checked against live data, were
  wrong (closed, wrong city) in 3 of 5 tested cases. The app would rather say "the search failed,
  try again" than confidently suggest something unverified.
- **Static, unconditional facts bypass the AI entirely.** Emergency numbers don't vary by trip, need
  no lookup, and must never be missing — so they're rendered directly by code, not asked of the
  model. Reserve this pattern for genuinely constant data; anything that varies by place or trip
  still has to come from a real tool call.
- **A baseline of coverage doesn't depend on the user remembering to ask.** The four base categories
  (restaurant, fuel, hospital, tea/snacks) are searched on every plan regardless of what was
  explicitly requested, for the same reason as the point above about emergency numbers — nobody
  remembers to ask "are there hospitals nearby" until they need one.

## Supporting infrastructure

- **Place-details cache** (`place_details_cache` Google Sheet tab, keyed by `place_id`) — under 30
  days old, reused with zero API calls; 30-90 days old, one cheap recheck
  (`businessStatus`+`rating` only) before trusting it again; over 90 days or never seen, a full
  fetch. Verified live: a repeat lookup of the same place served entirely from cache, correctly,
  with restroom/brand-recognition data intact.
- **Toll plaza data** — curated `TOLL_PLAZAS` table plus a Sheet-persisted "learned" table that
  grows from live lookups over time.
- **Brand outlet seeding** (`seed_brand_outlets.py`, one-off maintenance script, not part of the
  live app) — pre-populates the place-details cache with real outlets of the `RECOGNIZED_CHAINS`
  brands, scoped to the corridors the app already has curated toll data for (Bengaluru↔Mysuru↔Ooty,
  Hyderabad↔Bengaluru, Bengaluru↔Chennai, Hyderabad↔Vijayawada, Hyderabad↔Nagpur) rather than a
  nationwide crawl. Safe to re-run — upserts by `place_id`.

## Proposed, discussed, but NOT implemented

Kept here so a future conversation doesn't have to re-derive these from scratch, and doesn't
mistake them for current behavior. Pick any of these up by explicitly moving it into "The current
pipeline" above once it's actually built.

- **Journey time accounting for break duration**, not just drive time — every recommended break
  (tea/restroom ~15-20 min, a real meal ~45-60 min, fuel ~10 min) should add to the cumulative clock
  before calculating the next stop's arrival time, so later stops don't show up too early.
- **Keyword-tilted discovery queries** (e.g. `"clean restaurant"` instead of `"restaurant"`) — tested
  live, has a real but modest effect (filters out bar/nightlife venues), not a strong signal on its
  own.
- **`contextualContents` (Places API Text Search's review-justification feature) — confirmed dead
  end.** Tested extensively live, including Google's own documented example query and a US-region
  location: `contextualContents.reviews` never populated, only photos did. Not worth designing
  around; an AI's own explanation for why (a geographic restriction) was tested and found wrong.
- **Cross-referencing Zomato/Swiggy/TripAdvisor hygiene data — parked, likely blocked.** TripAdvisor
  has a real API but its current version sunsets 2026-08-31; Zomato's old public API is dead, what
  remains is merchant/POS-oriented; Swiggy's official API appears to be order/operations-oriented,
  not a discovery/reviews endpoint, and its own terms restrict uses adjacent to what this app would
  be doing. Would need an actual approval conversation with either platform to resolve, not just
  reading their docs.
- **A redesigned SVG background motif** (a traditional Indian gate / kamaan) to replace the current
  panel-style route SVGs, which read as awkward, boxed-in illustrations rather than blending into
  the page. Prototyped once (arch shape, flanking towers, crenellations) but never integrated into
  either app.
- **Strip's app-wide dark theme** (extending the Strip visualization's ink/paper/accent palette to
  the rest of the UI) — was in progress at one point; status should be re-verified against the
  current file before assuming it's complete.

## Related documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — engineering-level critique: system context, tradeoffs, known risks
- [DESIGN.md](DESIGN.md) — current visual identity
- [HANDOFF.md](HANDOFF.md) — session-to-session narrative notes

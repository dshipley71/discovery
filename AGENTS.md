# AGENTS.md — Public Webcam Discovery System

## Overview

This document defines the agent architecture for discovering, validating, and cataloging publicly accessible webcams from around the world. All agents operate under a strict **public-only mandate**: no credentials, no registration, no API keys, no private or surveillance feeds.

---

## Code Generation Policy

This system generates executable code as part of its output. All agents that perform searching, verification, and validation produce **Python scripts**. Map output is generated in the stack best matched to the requested complexity and features.

### Python — Search, Verification & Validation

All of the following agent functions are implemented as generated Python code:

| Agent | Python Module Generated |
|-------|------------------------|
| `DirectoryAgent` | `directory_crawler.py` — directory traversal, pagination, metadata extraction |
| `SearchAgent` | `search_agent.py` — multi-language query generation and result parsing |
| `ValidationAgent` | `validator.py` — HTTP validation, HLS playlist classification, legitimacy scoring |
| `CatalogAgent` | `catalog.py` — deduplication, geo-enrichment, JSON/Markdown export |
| `MaintenanceAgent` | `maintenance.py` — scheduled status checks, status updates, and validation-review reporting |
| `MapAgent` | `map.html` — self-contained Leaflet.js map; loads `camera.geojson` by default or any custom `.geojson` via in-map file picker |

**Required Python libraries:**
```
requests          # HTTP requests and HEAD checks
httpx             # Async HTTP for parallel validation
beautifulsoup4    # HTML parsing for directory traversal
playwright        # JS-rendered page handling (lazy-loaded directories)
rapidfuzz         # Fuzzy label matching for deduplication
geopy             # Geocoding via Nominatim/OSM
tldextract        # Domain normalization
langdetect        # Language detection for locale routing
deep_translator   # Label translation for non-English sources
validators        # URL normalization and validation
python-slugify    # ID slug generation
schedule          # MaintenanceAgent cron-style scheduling
loguru            # Structured logging across all modules
pydantic          # Schema validation for camera records
```

### Map Output — HTML Only

`MapAgent` always generates a single self-contained `map.html` file (Leaflet.js). No build step, no server, and no additional dependencies are required — open in any browser.

The generated map supports loading geospatial HLS camera data from a `.geojson` file in one of two ways:

| Mode | Behaviour |
|------|-----------|
| **Default** | Map auto-loads `camera.geojson` from the same directory on page open |
| **Custom file** | A file picker in the map UI lets the user load any `.geojson` file saved from a previous session or external source |

See `MAPS.md` for full map specification and geojson file format requirements.

---

## Agent Roster

### 1. `DirectoryAgent`
**Role:** Primary source crawler  
**Goal:** Systematically search prioritized directories and known public webcam aggregators for camera listings.

**Responsibilities:**
- Iterate through all sources listed in `SOURCES.md` (Priority Tier 1 first, then Tier 2, then general search)
- Extract camera URLs, labels, and metadata from each directory page
- Handle pagination, lazy-loaded content, and multi-page city indexes
- Pass raw results to `ValidationAgent`

**Search Strategy:**
- Begin with structured directories (Windy, webcamtaxi, skylinewebcams, etc.)
- For each directory, prioritize city-indexed pages over general browsing
- Use known URL patterns to enumerate city/region subpages (e.g., `/webcams/country/city/`)
- For non-English sources, apply locale-aware URL traversal (see `SKILLS.md → LocaleNavigationSkill`)
- Do not follow links beyond two hops from a known priority source without validation

**Generated Code:** `directory_crawler.py`
- Uses `httpx` (async) for concurrent page fetching across multiple directories
- Uses `BeautifulSoup` for HTML parsing and link extraction
- Uses `playwright` for JS-rendered directories that require scroll or interaction to load listings
- Outputs a list of `CameraCandidate` Pydantic objects passed to the validator pipeline

**Output:** Raw camera candidate list → passed to `ValidationAgent`

---

### 2. `SearchAgent`
**Role:** Open web discovery  
**Goal:** Find public webcam feeds not indexed in known directories via targeted search queries.

**Responsibilities:**
- Execute structured search queries targeting public cam embed pages, municipal open data portals, tourism boards, transport authority feeds, and university/research network cams
- Search in multiple languages using translated query templates
- Identify new directories or aggregators not in `SOURCES.md` and flag them for human review
- Avoid any source on the blocked list in `SOURCES.md`

**Search Query Templates:**
```
"live webcam" [city] site:[known_directory]
"public webcam" [city] -login -register -subscribe
inurl:webcam OR inurl:livecam [city] filetype:html
"m3u8" OR "hls stream" [city] public
[city] "traffic camera" live public feed
[city] municipality OR "city council" webcam public
[city] 観光 ライブカメラ (Japanese tourism live camera)
[city] cámara en vivo pública
[city] caméra en direct publique
[city] öffentliche Webcam
```

**Generated Code:** `search_agent.py`
- Uses `httpx` to execute search queries against public search APIs and scrape result pages
- Applies `QueryGenerationSkill` templates rendered per city and language code
- Uses `langdetect` + `deep_translator` to handle and normalize non-English results
- Outputs additional `CameraCandidate` objects merged into the DirectoryAgent pipeline

**Output:** Additional camera candidates → passed to `ValidationAgent`

---

### 3. `ValidationAgent`
**Role:** Feed verification and legitimacy scoring  
**Goal:** Confirm each candidate camera is genuinely public, accessible, and live.

**Responsibilities:**
- Perform HTTP HEAD (fallback: GET) on each candidate URL
- **Verify that the candidate URL resolves to a live HLS playlist, not an HTML page** — the URL must be a direct `.m3u8` endpoint and return an HLS playlist payload (`#EXTM3U`) with an HLS content type or equivalent byte signature; a `text/html` response is a validation failure
- Classify playlist type: `HLS_master` or `HLS_stream`
- Apply the **Public Legitimacy Score** (see below)
- Flag feeds that redirect to login pages, require cookies/JS, or return non-200 status
- Check `robots.txt` of host domain — skip if webcam scraping is explicitly prohibited
- Reject any feed where public accessibility is ambiguous

**Public Legitimacy Score:**

| Score | Criteria |
|-------|----------|
| ✅ **High** | Direct stream URL, HTTP 200, no auth headers, resolves cleanly |
| ⚠️ **Medium** | Embed page with player, no login wall, JS required but no auth |
| ❌ **Low / Exclude** | Login redirect, cookie gate, HTTP 403/401, geo-block detected, ambiguous consent |

**Generated Code:** `validator.py`
- Uses `httpx` with configurable timeout and `asyncio.gather` for parallel HEAD/GET checks across all candidates
- Implements `RobotsPolicySkill` via `urllib.robotparser` before any domain is fetched
- Implements `FeedTypeClassificationSkill` by inspecting HLS content-type headers and playlist body signatures
- Assigns a `legitimacy_score` enum (`high` / `medium` / `low`) to each record using rule-based logic
- Rejects records scoring `low`; flags `medium` records with a `requires_review` note
- Emits validated `CameraRecord` Pydantic objects

**Output:** Validated camera records → passed to `CatalogAgent`

---

### 4. `CatalogAgent`
**Role:** Data structuring, deduplication, and output management  
**Goal:** Maintain the canonical camera list as a clean, deduplicated, structured dataset.

**Responsibilities:**
- Accept validated records and insert into the master catalog
- Deduplicate using normalized direct stream URL or stable source-provided camera identity when available. Do **not** collapse cameras solely because they share approximate coordinates, city names, labels, source pages, CDN hostnames, or cloud regions.
- Consolidate duplicate entries: keep one canonical record, log all source URLs in `source_refs[]`
- Organize output by: `continent → country → city → camera`
- Maintain `last_verified` timestamp and `status` field per record
- Export to `camera.geojson` (primary map-ready output) and `cameras.md` (human-readable link list); signal `MapAgent` that a new catalog is ready for map generation

**Generated Code:** `catalog.py`
- Uses `rapidfuzz` for fuzzy label deduplication
- Uses `geopy` (Nominatim) for coordinate enrichment on records missing lat/lon
- Uses `python-slugify` to generate stable `id` slugs
- Serializes the final catalog directly to `camera.geojson` (GeoJSON FeatureCollection, validated against the Pydantic schema) and `cameras.md`
- Coordinate order in GeoJSON output is `[longitude, latitude]` per RFC 7946 — enforced in `export_geojson()`
- Triggers map generation by writing `camera.geojson` to the output directory alongside `map.html`

**Output:** `camera.geojson`, `cameras.md`

---

### 5. `MapAgent`
**Role:** Geospatial visualization and interactive map output  
**Goal:** Produce and maintain a live interactive world map of all cataloged cameras, enabling hover inspection and click-to-play directly on the map.

**Responsibilities:**
- Generate `map.html` — a self-contained Leaflet.js interactive map (see `MAPS.md` for full specification)
- Ensure `map.html` auto-loads `camera.geojson` from its directory on page open if the file is present
- If `camera.geojson` is not found, display an empty-state overlay prompting the user to load a `.geojson` file via the in-map file picker — the map is never silently blank
- Display the active filename in the map header so the user always knows which dataset is loaded
- Ensure every camera record with valid coordinates appears as a map marker
- Apply marker clustering for dense city areas; auto-uncluster on zoom
- Populate click popup with full camera schema fields
- Wire the "Watch" button in each popup to open the camera's `url` in a new tab
- Read `camera.geojson` produced by `CatalogAgent` — no conversion step required; every record represents a direct `.m3u8` stream

**Map Behavior Requirements:**
- Hover over marker → display full camera info card (all schema fields)
- Click marker → open the HLS player modal using the record `url`
- Cluster badge shows camera count; clicking cluster zooms to reveal individual markers
- Map filter panel: filter by `feed_type`, `legitimacy_score`, `status`, `continent` (feed types will be HLS only)
- Search bar: fly-to by city or camera label
- Layer overlays: heatmap density; Tier 1 city circles (blue); Tier 2 city circles (amber); Tier 3 destination circles (green); Blocked sources markers (red ✕) — all independent toggles
- Status color coding on markers: ✅ green = live, ⚠️ amber = unknown, ❌ red = dead

**Generated Code:** `map.html` — always HTML (Leaflet.js), single self-contained file

- Uses Leaflet.js 1.9.x + Leaflet.markercluster + Leaflet.heat via CDN — no install required
- On page load, attempts to fetch `camera.geojson` from the same directory; displays an empty-state overlay with a file picker prompt if the file is not found
- Includes a **file picker** in the map UI: user can click "Load GeoJSON" to open any `.geojson` file saved from a prior session or external tool
- Loaded file name is displayed in the UI header so the user knows which dataset is active
- All map features (clustering, filtering, status colours, popups, search) work identically regardless of which geojson source is loaded

**Output:** `map.html`

---

### 6. `MaintenanceAgent`
**Role:** Ongoing liveness monitoring  
**Goal:** Keep the catalog accurate over time by detecting dead links and re-verifying feeds.

**Responsibilities:**
- Run HEAD checks on all catalog entries on a **weekly cadence**
- Mark feeds dead after 2 consecutive failed checks; after 4 consecutive failures, queue them for validation review before any manual removal
- Re-run `DirectoryAgent` on priority sources monthly to catch new additions
- Flag feeds that have changed URL structure for human review
- Log all status changes with timestamps

**Generated Code:** `maintenance.py`
- Uses `httpx` async for batched weekly status checks with configurable concurrency
- Uses `schedule` library for cron-style execution (weekly full check, monthly re-crawl)
- Updates `status` and `last_verified` fields in `camera.geojson` in-place, preserving all other feature properties
- Writes a timestamped `maintenance_log.jsonl` audit trail of all status changes
- Writes `pending_validation_review.jsonl` entries for records that have failed repeatedly so removal only happens after explicit validation review

---

## Output Schema

Each camera record must conform to the following structure:

```json
{
  "id": "unique-slug-city-label",
  "label": "Times Square North View",
  "city": "New York City",
  "region": "New York",
  "country": "United States",
  "continent": "North America",
  "latitude": 40.7580,
  "longitude": -73.9855,
  "url": "https://example.com/stream/ts-north.m3u8",
  "feed_type": "HLS_stream",
  "source_directory": "earthcam.com",
  "source_refs": [
    "https://earthcam.com/usa/newyork/timessquare/",
    "https://webcamtaxi.com/en/usa/new-york/times-square.html"
  ],
  "legitimacy_score": "high",
  "last_verified": "2025-03-10",
  "status": "live",
  "notes": ""
}
```

---

## Geographic Priority Tiers

### Tier 1 — Major Global Cities (search exhaustively)
New York City, London, Tokyo, Paris, Sydney, Dubai, Singapore, Hong Kong, Los Angeles, Chicago, Toronto, Berlin, Amsterdam, Barcelona, Rome, Madrid, São Paulo, Mexico City, Seoul, Mumbai, Shanghai, Beijing, Istanbul, Cairo, Johannesburg, Moscow, Vienna, Prague, Budapest, Warsaw, Zurich, Stockholm, Oslo, Copenhagen, Helsinki, Athens, Lisbon, Brussels, Dublin

### Tier 2 — Regional Cities & Landmarks (search broadly)
All remaining capital cities, major port cities, UNESCO World Heritage sites with public cam infrastructure, major airport hubs, major mountain/alpine resorts, notable beach destinations, major river confluences and harbors

### Tier 3 — General Discovery (opportunistic)
Any publicly accessible feed found via search that passes validation, regardless of location

---

## Execution Order

```
1. DirectoryAgent       → Priority Tier 1 sources
2. SearchAgent          → Fill gaps in Tier 1 cities
3. DirectoryAgent       → Priority Tier 2 sources
4. SearchAgent          → Fill gaps in Tier 2 cities + general discovery
5. ValidationAgent      → Process all candidates (runs in parallel with above)
6. CatalogAgent         → Deduplicate and structure → emit camera.geojson + cameras.md
7. GeoEnrichmentSkill   → Ensure all records have lat/lon (required by MapAgent)
8. HealthCheckSkill     → Verify and stamp status fields before map export
9. MapAgent             → Generate map.html (HTML/Leaflet); reads camera.geojson produced in step 6; also supports user-supplied .geojson via file picker
10. MaintenanceAgent    → Schedule weekly HEAD checks, monthly re-crawl, and map refresh on catalog change
```

---

## Hard Rules (All Agents)

- **Never** follow a source on the SOURCES.md block list, even if linked from a trusted source
- **Never** attempt to authenticate, register, or bypass any access control
- **Never** include a camera where public accessibility is uncertain — default to exclusion
- **Never** cap the number of cameras cataloged per city or location
- **Always** record the source directory for every entry
- **Always** prefer a direct stream URL over an embed page URL when both are available
- **Always** verify that `url` resolves directly to a live HLS `.m3u8` playlist — HTML pages, embeds, JPEG refresh URLs, and any non-HLS protocol are invalid
- **Always** include `latitude` and `longitude` on every record — run `GeoEnrichmentSkill` if missing; a record without coordinates cannot appear on the map
- **Always** invoke `MapAgent` after any catalog update to regenerate `map.html`
- **Do not use or add a `tests/` directory in this repository for validation workflows**; use the Colab notebook validation path instead.

---

## Agentic Planner / Memory / Analysis Extensions

- `PlannerAgent` now supports real LLM-backed planning (`ollama` default; `openai-compatible` optional). No mock planner path is provided.
- Optional `MemWeave` sidecar memory can be enabled via config/env/CLI and stores markdown run summaries under `memory/runs/`.
- Optional `VisualStreamAnalysis` provides bounded stream behavior classification and maps detailed substatus to public `live|dead|unknown` output status.
- Optional `VideoSummarizationAgent` generates bounded visual and optional audio summaries from real stream samples.
- New command: `webcam-discovery run-agentic "<natural language query>"`.
- New logs: `planner_runs.jsonl`, `visual_stream_analysis.jsonl`, `video_summaries.jsonl`, `memory_updates.jsonl`.


## Agentic Validation Handoff Requirements

The agentic workflow uses `candidates/agentic_candidates.jsonl` as the primary durable handoff between discovery and validation once direct HLS candidates are found. Page-level scope gates are discovery boundaries: they decide whether a search result should be expanded, but `logs/search_result_scope_decisions.jsonl` is not final truth for whether an already-discovered direct stream should be validated.

Stream-candidate scope gates review direct HLS candidates using the full evidence package: direct URL, source page, source query, title/snippet where available, lineage, source metadata, labels, coordinates, and the LLM-inferred user scope. HLS URL formats are source-specific; agents must not require any fixed camera-ID format, CDN path, agency prefix, or location token. When a stream-scope LLM call times out or fails, plausible direct HLS candidates default to `review`/validation-allowed behavior, with the fallback clearly written to `logs/stream_candidate_scope_decisions.jsonl`.

Validation must classify each unique direct HLS candidate that is not skipped by policy, safety, or configured caps. Status vocabulary includes `live`, `dead`, `unknown`, `restricted`, `timeout`, `offline_http`, and `decode_failed`, with richer substatus values preserved where available. Geocoding must preserve source coordinates first, then candidate/page metadata, nearby labels/text, LLM candidate context, and only then clearly-labeled scope-level fallbacks. Do not invent coordinates.

## One-Time Clarification Agent Rules

Before any search, feed discovery, deep discovery, validation, cataloging, or map generation, the workflow must run an LLM-based clarification preflight unless explicitly disabled for debugging. This is not a heuristic location parser. It asks for clarification only when the query is ambiguous, conflicting, or lacks a specific searchable scope.

Examples:

- `Get me all traffic cameras from Paris` is ambiguous and should ask one concise question, such as whether the user means Paris, France, Paris, Texas, or another Paris.
- `Get me all traffic camera` is underspecified and should ask for a place, landmark, coordinates, IP address, hostname, agency, or public website.
- `Get me public live HLS traffic cameras from Paris, France` is sufficiently scoped and should proceed to normal LLM scope enforcement.

The clarification agent may ask only one turn and must provide no more than three questions. The CLI must stop before discovery and instruct the user to rerun with a clearer natural-language query; do not use a separate clarification-answer flag or hidden interactive query rewrite path. Write `logs/query_clarification.json` and `logs/run_summary.json` with `status=needs_clarification`. If the rerun query is still insufficient or ambiguous, do not ask repeatedly; let the normal scope enforcement rules stop discovery.

## Validation Reporting Consistency Rules

Agents must keep validation reporting internally consistent. `http_hls_probe_results.jsonl` is the early HTTP/HLS probe artifact; it is not final camera truth. `validation_results.jsonl`, `camera_status_summary.json`, and `run_summary.json.validation` must represent final status after playlist, ffprobe, visual analysis, capping, and catalog selection. If a candidate or record is removed by a validation/catalog cap, write a durable row with the URL, stage, cap, before/after counts, and reason. Scope-decision rows must include provider, model, raw LLM response, fallback metadata, and whether validation was allowed.

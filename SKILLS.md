# SKILLS.md — Public Webcam Discovery Skills Library

## Overview

This file documents all reusable skills available to agents in the webcam discovery system. Each skill is a discrete, composable capability that agents may invoke independently or in combination. Skills are stateless unless noted.

All skills are implemented as Python functions or classes within their respective generated modules. Each skill entry below documents its logic, inputs/outputs, and the Python implementation pattern used.

---

## Skill Index

| Skill | Primary Agent | Description |
|-------|--------------|-------------|
| `DirectoryTraversalSkill` | DirectoryAgent | Enumerate cameras from structured web directories |
| `FeedExtractionSkill` | DirectoryAgent, SearchAgent | Extract raw stream URLs from embed/player pages |
| `FeedValidationSkill` | ValidationAgent | HTTP-level liveness and auth detection |
| `LocaleNavigationSkill` | SearchAgent, DirectoryAgent | Navigate non-English camera sites |
| `QueryGenerationSkill` | SearchAgent | Produce targeted search query variants |
| `DeduplicationSkill` | CatalogAgent | Normalize and merge duplicate camera records |
| `GeoEnrichmentSkill` | CatalogAgent | Attach coordinates and city metadata to records |
| `RobotsPolicySkill` | ValidationAgent | Check robots.txt compliance before scraping |
| `FeedTypeClassificationSkill` | ValidationAgent | Identify stream protocol and player type |
| `HealthCheckSkill` | MaintenanceAgent | Batch liveness verification for existing records |
| `SourceDiscoverySkill` | SearchAgent | Identify new unlisted cam directories for review |
| `GeoJSONExportSkill` | CatalogAgent | Serialize validated camera records directly to `camera.geojson` — the primary pipeline output |

---

## Skill Specifications

---

### `DirectoryTraversalSkill`

**Purpose:** Systematically enumerate all camera listings from a structured public webcam directory.

**Inputs:**
- `base_url`: Root URL of the directory
- `city_filter`: Optional — restrict to specific cities
- `max_depth`: Maximum link-follow depth (default: 2)

**Process:**
1. Fetch directory index page
2. Identify city/region navigation structure (breadcrumb, sidebar, URL pattern)
3. Enumerate all city subpages matching the geographic priority tiers in `AGENTS.md`
4. For each city page, extract all camera entries: label, embed URL, thumbnail if present
5. Handle pagination — follow "next page" links until exhausted
6. Detect and handle lazy-loaded content (scroll-triggered or JS-rendered listings)
7. Return structured list of raw camera candidates

**URL Pattern Examples:**
```
/webcams/europe/france/paris/          ← country/city hierarchy
/live-cameras/?city=tokyo              ← query param city filter
/en/cameras/list/[city]/page/[n]/      ← paginated city list
```

**Error Handling:**
- HTTP 429 (rate limit): back off 30s, retry up to 3 times
- HTTP 403/404: log and skip, do not retry
- Timeout (>10s): log and skip

---

### `FeedExtractionSkill`

**Purpose:** Extract the raw stream URL from an embed page or JS player.

**Inputs:**
- `page_url`: URL of the embed/player page

**Process:**
1. Fetch page HTML
2. Scan for direct stream indicators in priority order:
   - `<source src="...">` tags with `.m3u8`, `.mjpg`, `.mp4` extensions
   - `<iframe src="...">` pointing to known player domains
   - JavaScript variables: `streamUrl`, `src`, `hlsUrl`, `videoSrc`, etc.
   - Network request inspection patterns (document `data-src`, `data-stream`)
3. Resolve relative URLs to absolute
4. Return: `direct_stream_url` (if found), `embed_url`, `feed_type_hint`

**Known Player Signatures:**
```
JW Player:     jwplayer().setup({ file: "..." })
Video.js:      <video data-setup='{"sources":[{"src":"..."}]}'>
HLS.js:        Hls.loadSource("...")
YouTube:       <iframe src="https://www.youtube.com/embed/...">
EarthCam:      data-cam-url="..."
Windy embed:   <iframe src="https://embed.windy.com/embed2.html?...">
```

---

### `FeedValidationSkill`

**Purpose:** Confirm a camera URL is genuinely public, accessible, and not auth-gated.

**Inputs:**
- `url`: Stream or embed URL to validate

**Process:**
1. Perform HTTP HEAD request (2s timeout)
2. If HEAD not supported, fall back to GET with 512-byte limit
3. Evaluate response:
   - **Pass:** HTTP 200–206, content-type matches expected media type
   - **Warn:** HTTP 200 but content-type is `text/html` (may be player page, not stream)
   - **Fail:** HTTP 301/302 to login URL, HTTP 401/403/407, content contains "login", "sign in", "register", "subscribe"
4. **Confirm stream_url is a live media stream, not an HTML page:** A `text/html` `Content-Type` on a `stream_url` is a hard failure — the URL must return an actual media type (`image/jpeg`, `multipart/x-mixed-replace`, `application/vnd.apple.mpegurl`, `video/mp4`, or equivalent). YouTube embed URLs (`youtube-nocookie.com/embed/`) are accepted as `youtube_live` without a media Content-Type check.
5. Check response headers for auth indicators: `WWW-Authenticate`, `X-Auth-Required`
5. Check for soft redirects: follow up to 2 redirects, fail if landing page contains auth forms
6. Return: `status_code`, `content_type`, `legitimacy_score`, `fail_reason` (if applicable)

**Auth Detection Patterns:**
```
URL contains:   /login, /signin, /auth, /register, /subscribe, /account
Body contains:  "Please log in", "Create an account", "Sign up to view"
Header:         WWW-Authenticate: Basic/Bearer/Digest
```

---

### `LocaleNavigationSkill`

**Purpose:** Navigate and extract camera listings from non-English language directories.

**Inputs:**
- `source_url`: URL of the non-English directory
- `target_language`: ISO language code (e.g., `ja`, `de`, `fr`, `ko`, `pt`, `es`, `nl`, `sv`, `no`)

**Process:**
1. Identify navigation language from page `lang` attribute or content detection
2. Apply known URL/navigation patterns for that locale:
   - Japanese: Look for `ライブカメラ` (live camera), `観光` (tourism), `国道` (national road)
   - German: `Webcam`, `Livekamera`, `Straßenkamera`, `Verkehr`
   - French: `caméra en direct`, `webcam en ligne`, `caméra de surveillance publique`
   - Korean: `실시간 카메라`, `CCTV 공개`
   - Portuguese: `câmera ao vivo`, `webcam pública`
   - Spanish: `cámara en vivo`, `webcam pública`, `cámara de tráfico`
   - Norwegian/Swedish: `webkamera`, `trafikkamera`, `veikamera`
3. Extract camera entries using structure-based parsing (not language-dependent)
4. Translate labels to English using available translation tool before catalog insertion

---

### `QueryGenerationSkill`

**Purpose:** Produce high-yield search query variants for a given city or region.

**Inputs:**
- `city`: Target city name
- `language_codes`: List of applicable ISO language codes

**Output — Query Set per City:**
```
Core English queries:
  "[city] live webcam public"
  "[city] traffic camera public feed"
  "[city] city webcam no login"
  "[city] tourism live camera"
  "[city] harbor port live cam"
  "[city] airport webcam live"
  "[city] transport authority live camera"
  "[city] open data webcam"
  "site:windy.com/webcams [city]"
  "site:webcamtaxi.com [city]"
  "site:skylinewebcams.com [city]"
  "site:earthcam.com [city]"

Locale queries (auto-translated per language_codes):
  "[city] ライブカメラ 公開" (Japanese)
  "[city] webcam öffentlich kostenlos" (German)
  "[city] caméra en direct gratuit" (French)
  "[city] cámara en vivo público" (Spanish)
  "[city] 실시간 카메라 공개" (Korean)
  "[city] live webkamera gratis" (Swedish/Norwegian)

Government/infrastructure:
  "[city] DOT traffic camera"
  "[city] municipality live camera"
  "[city] 511 traffic feed"
  "[city] national weather service camera"
```

---

### `DeduplicationSkill`

**Purpose:** Identify and merge duplicate camera records in the catalog.

**Inputs:**
- `candidate_record`: New record to insert
- `existing_catalog`: Current catalog dataset

**Deduplication Logic (in priority order):**

1. **URL normalization match:**
   - Strip: query params (`utm_*`, `ref=`, `source=`), trailing slashes, `www.` prefix, protocol (`http` vs `https`)
   - If normalized URLs match → duplicate

2. **Coordinate proximity match (if lat/lon available):**
   - If two records are within 50 meters of each other AND same city → likely same camera
   - Flag for human review if labels differ significantly

3. **Fuzzy label match:**
   - If city matches exactly AND label similarity > 85% (Levenshtein) → likely duplicate
   - Flag for human review rather than auto-merge

**On Duplicate Detected:**
- Keep existing canonical record
- Append new `source_url` to `source_refs[]`
- Update `last_verified` if newer
- Log merge action

---

### `GeoEnrichmentSkill`

**Purpose:** Attach geographic metadata (coordinates, region, country, continent) to camera records lacking it.

**Inputs:**
- `city`: City name string
- `label`: Camera label (may contain street name or landmark)

**Process:**
1. Attempt coordinate lookup from camera embed page metadata (`og:latitude`, `og:longitude`, schema.org Place)
2. If not found, geocode city centroid using public geocoding (Nominatim/OSM)
3. If label contains a recognizable landmark or street, attempt landmark geocoding
4. Populate: `latitude`, `longitude`, `country`, `region`, `continent`
5. Flag records where geocoding confidence is low

---

### `RobotsPolicySkill`

**Purpose:** Check whether a domain's `robots.txt` prohibits scraping webcam pages.

**Inputs:**
- `domain`: Base domain of the source

**Process:**
1. Fetch `https://[domain]/robots.txt`
2. Parse `Disallow` rules for relevant paths (e.g., `/webcams/`, `/cameras/`, `/live/`)
3. Check `User-agent: *` and `User-agent: Claude` rules
4. Return: `allowed` (bool), `disallowed_paths` (list)

**Policy:** If any relevant webcam path is disallowed, skip that source entirely. Do not attempt to work around robot exclusions.

---

### `FeedTypeClassificationSkill`

**Purpose:** Identify the stream protocol and player type for a given feed.

**Inputs:**
- `url`: Stream or embed URL
- `content_type`: HTTP content-type header value (optional)
- `page_html`: Source HTML (optional)

**Classification Logic:**

| Feed Type | Indicators |
|-----------|-----------|
| `MJPEG` | URL ends in `.mjpg`, `.mjpeg`, content-type: `multipart/x-mixed-replace` |
| `HLS` | URL ends in `.m3u8`, content-type: `application/vnd.apple.mpegurl` |
| `MP4_stream` | URL ends in `.mp4` with `Content-Type: video/mp4` |
| `iframe_embed` | URL is an embed page containing `<iframe>` |
| `js_player` | Page uses JW Player, Video.js, HLS.js, or similar |
| `static_refresh` | URL returns JPEG/PNG, page uses meta-refresh or JS polling |
| `youtube_live` | URL matches `youtube.com/embed/` or `youtu.be/` live stream |
| `unknown` | Cannot be classified |

---

### `HealthCheckSkill`

**Purpose:** Batch liveness check for existing catalog records.

**Inputs:**
- `records`: List of catalog records to check
- `concurrency`: Number of parallel checks (default: 10)

**Process:**
1. For each record, perform HTTP HEAD on `stream_url` (primary) and `url` (fallback); also check `direct_stream_url` if present
2. For `youtube_live` feed type, verify the embed URL is still active by checking the video ID status via the YouTube oEmbed endpoint (`https://www.youtube.com/oembed?url=...`) — a 404 response means the stream has been removed
2. Record result: `live`, `dead`, `redirected`, `auth_gated`
3. Update `status` and `last_verified` for each record
4. After 2 consecutive `dead` results: set `status = "dead"`, flag for review
5. After 4 consecutive `dead` results: mark for removal from catalog
6. Return: summary report (total checked, live count, dead count, newly dead)

---

### `SourceDiscoverySkill`

**Purpose:** Identify new webcam directories or aggregators not currently in `SOURCES.md`.

**Inputs:**
- `search_results`: Raw search results from `SearchAgent`

**Process:**
1. Extract root domains from all result URLs
2. Filter out known domains (already in SOURCES.md allow or block list)
3. For each unknown domain: check if it appears to be a cam directory (>3 cam listings, structured navigation)
4. Score by: number of cam listings found, geographic coverage, accessibility (no auth)
5. Return: candidate new sources list → flagged for human review and manual addition to `SOURCES.md`

**Do not automatically add to allow list** — all new sources require human approval.

---

### `GeoJSONExportSkill`

**Purpose:** Convert the `cameras.json` catalog into a valid GeoJSON FeatureCollection suitable for map rendering.

**Inputs:**
- `cameras`: List of validated `CameraRecord` Pydantic objects from `CatalogAgent`

**Process:**
1. Accept the validated `CameraRecord` list from `CatalogAgent` — no intermediate JSON file read required
2. Skip any record where `latitude` or `longitude` is null — log these as `unmapped`
3. For each valid record, construct a GeoJSON Feature:

```json
{
  "type": "Feature",
  "geometry": {
    "type": "Point",
    "coordinates": [longitude, latitude]
  },
  "properties": {
    "id": "...",
    "label": "...",
    "city": "...",
    "region": "...",
    "country": "...",
    "continent": "...",
    "url": "...",
    "stream_url": "...",
    "direct_stream_url": "...",
    "video_id": "...",
    "feed_type": "...",
    "source_directory": "...",
    "legitimacy_score": "...",
    "requires_js": false,
    "geo_restricted": false,
    "last_verified": "...",
    "status": "...",
    "notes": "..."
  }
}
```

4. Wrap all features in a FeatureCollection:
```json
{
  "type": "FeatureCollection",
  "features": [...],
  "metadata": {
    "total": 0,
    "live": 0,
    "dead": 0,
    "unknown": 0,
    "unmapped": 0,
    "generated": "ISO timestamp"
  }
}
```

5. Validate GeoJSON schema before writing
6. Write to `camera.geojson`

**Output:** `camera.geojson`

---

### `MapRenderingSkill`

**Purpose:** Generate a fully self-contained interactive HTML map from the `camera.geojson` file. The map must require no build step, no server, and no external dependencies beyond CDN-hosted libraries.

**Inputs:**
- `geojson_path`: Path to `camera.geojson`
- `map_config`: Optional configuration overrides (see MAP.md)

**Technology Stack:**
- **Map engine:** Leaflet.js (v1.9+) via CDN
- **Tile layer:** CartoDB Dark Matter (no API key required)
- **Clustering:** Leaflet.markercluster via CDN
- **Video playback:** HTML5 `<video>` for HLS (via hls.js), native for MJPEG, `<iframe>` for embed types
- **UI framework:** Vanilla JS + CSS (no React/Vue dependency)
- **Icons:** Custom SVG camera pin icons, color-coded by status

**Marker Color Coding:**
| Status | Color | Hex |
|--------|-------|-----|
| `live` | Cyan-green | `#00e5a0` |
| `unknown` | Amber | `#f5a623` |
| `dead` | Red-grey | `#e05252` |

**Legitimacy Score Indicator:**
- High → solid marker fill
- Medium → semi-transparent fill
- Low/Excluded → should not appear on map (excluded at export step)

**Hover Tooltip — Required Fields (all from output schema):**
```
┌─────────────────────────────────────────┐
│ 📷 [label]                    [status]  │
│ ─────────────────────────────────────── │
│ 📍 [city], [region], [country]          │
│ 🌐 [continent]                          │
│ ─────────────────────────────────────── │
│ Feed Type:     [feed_type]              │
│ Source:        [source_directory]       │
│ Legitimacy:    [legitimacy_score]       │
│ Requires JS:   [yes/no]                 │
│ Geo Restricted:[yes/no]                 │
│ Last Verified: [last_verified]          │
│ ─────────────────────────────────────── │
│ Notes: [notes]                          │
│ ─────────────────────────────────────── │
│ [▶ Click to Watch]                      │
└─────────────────────────────────────────┘
```

**Click-to-Play Modal — Behavior:**
1. On marker click: open full-screen overlay modal
2. Modal header: camera label + city/country
3. Player area: attempt in order:
   - `direct_stream_url` with `feed_type = HLS` → use hls.js
   - `direct_stream_url` with `feed_type = MJPEG` → `<img src="...">` tag (MJPEG streams as img)
   - `url` → `<iframe src="...">` embed
   - Fallback: display URL with "Open in new tab" button
4. Modal footer: full schema metadata, `source_refs[]` links, "Open source page" button
5. Close: ESC key, click outside, or ✕ button
6. Modal must not navigate away from the map

**Filter Panel — Required Controls:**
- Feed type: checkbox multi-select (MJPEG, HLS, iframe, js_player, static_refresh, youtube_live)
- Status: toggle buttons (Live, Unknown, Dead)
- Legitimacy: toggle buttons (High, Medium)
- Continent: dropdown
- Real-time: filter applies instantly, map re-renders without page reload

**Search Bar:**
- Accepts city name or camera label
- Flies map to matching city centroid on Enter
- Highlights matching markers

**Stats Bar (top of map):**
- Total cameras | Live | Unknown | Dead | Last updated

**Process:**
1. Initialize Leaflet map centered on world view (zoom 2–3)
2. On page load, attempt to `fetch('camera.geojson')` from the same directory; if not found, display an empty-state overlay prompting the user to load a `.geojson` file via the in-map file picker — never render without data silently
3. Load all features into marker cluster group
4. Attach hover and click handlers per spec above
5. Render filter panel and wire to cluster group
6. Output single `map.html` file

**Output:** `map.html` (self-contained, no external file dependencies except CDN libraries)


---

## Python Implementation Standards

All skills are implemented as Python modules conforming to the following conventions:

### Module Structure
```python
# Each skill is a class with a single public `run()` method
from pydantic import BaseModel
from loguru import logger

class SkillInput(BaseModel):
    ...

class SkillOutput(BaseModel):
    ...

class ExampleSkill:
    def run(self, input: SkillInput) -> SkillOutput:
        logger.info(f"Running ExampleSkill with input: {input}")
        ...
```

### Async Skills
Skills performing network I/O use `async def run()` and are called via `asyncio.gather` for parallelism:
```python
import httpx
import asyncio

class FeedValidationSkill:
    async def run(self, urls: list[str]) -> list[ValidationResult]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            tasks = [self._check(client, url) for url in urls]
            return await asyncio.gather(*tasks, return_exceptions=True)
```

### Error Handling
All skills catch and log exceptions without crashing the pipeline:
```python
try:
    result = await self._check(client, url)
except httpx.TimeoutException:
    logger.warning(f"Timeout on {url}")
    result = ValidationResult(url=url, status="unknown", fail_reason="timeout")
```

### Schema Validation
All camera records flowing between agents are validated against the `CameraRecord` Pydantic model defined in `schemas.py`. Any record failing validation is logged and discarded rather than passed forward.

---

## GeoJSON Export Skill

### `GeoJSONExportSkill`

**Purpose:** Serialize validated `CameraRecord` objects directly to `camera.geojson` — the primary output of the pipeline. `camera.geojson` is the canonical map-ready file; it is not derived from a separate `cameras.json` but is produced in a single export step.

**Inputs:**
- `cameras`: List of validated `CameraRecord` Pydantic objects
- `output_path`: Destination file path (default: `camera.geojson`)

**Process:**
1. Filter out any records missing `latitude` or `longitude` — log each skipped record
2. For each remaining record, build a GeoJSON `Feature`:
   - `geometry.type` = `"Point"`
   - `geometry.coordinates` = `[longitude, latitude]` (GeoJSON spec: lon first)
   - `properties` = all remaining `CameraRecord` fields via `model_dump()`
3. Wrap all features in a `FeatureCollection`
4. Write to `output_path` as formatted JSON
5. Return a summary: total records, exported count, skipped count

**Implementation (in `catalog.py`):**
```python
def export_geojson(cameras: list[CameraRecord], path: str = "camera.geojson") -> dict:
    skipped = []
    features = []
    for c in cameras:
        if not (c.latitude and c.longitude):
            skipped.append(c.id)
            continue
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c.longitude, c.latitude]},
            "properties": c.model_dump(exclude={"latitude", "longitude"})
        })
    geojson = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2, default=str)
    logger.info(f"GeoJSON export: {len(features)} features written, {len(skipped)} skipped")
    if skipped:
        logger.warning(f"Skipped (missing coordinates): {skipped}")
    return {"exported": len(features), "skipped": len(skipped), "path": path}
```

**Notes:**
- This is the primary export call at the end of every `CatalogAgent` run — `camera.geojson` is the canonical output, not a secondary conversion
- The output file can be named anything with a `.geojson` extension — users may save session-specific files (e.g., `europe_2025-03.geojson`) and load them via the map's file picker
- GeoJSON coordinate order is `[longitude, latitude]` per RFC 7946 — not `[latitude, longitude]`

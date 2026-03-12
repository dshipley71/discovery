# MAPS.md — Interactive Camera Map: Architecture & Integration

## Overview

`map.html` is the visual interface for the public webcam discovery system. It renders all cataloged cameras onto an interactive world map, organized by geographic tier, feed type, and live status.

The map is always a **single self-contained HTML file** (Leaflet.js). No server, no build step, no dependencies to install — open it in any browser. Camera data is loaded from a `.geojson` file, either the default `camera.geojson` or any custom `.geojson` file the user selects via the in-map file picker.

---

## Integration with Other Files

| File | Role in Map System |
|------|--------------------|
| `AGENTS.md` | `CatalogAgent` produces `camera.geojson` directly as its primary output; `MapAgent` reads it to generate `map.html` |
| `SKILLS.md` → `GeoEnrichmentSkill` | Ensures every camera record has valid lat/lon for map placement |
| `SKILLS.md` → `HealthCheckSkill` | Updates `status` field used for map marker color coding |
| `SOURCES.md` | Priority tier assignments determine map cluster colors |
| `CLAUDE.md` | Output schema defines the JSON fields consumed by the map |
| `camera.geojson` | Primary map data file — auto-loaded on page open; also selectable via in-map file picker |
| `map.html` | The rendered map itself |

---

## Data Requirements

For a camera to appear on the map, its feature in `camera.geojson` **must** contain:

```json
{
  "id": "string — unique slug",
  "label": "string — human-readable name",
  "city": "string",
  "country": "string",
  "latitude": "number — decimal degrees",
  "longitude": "number — decimal degrees",
  "url": "string — source/watch page URL",
  "stream_url": "string — direct playable stream URL (MJPEG, HLS, JPEG refresh, or YouTube nocookie embed)",
  "video_id": "string — YouTube video ID (youtube_live feeds only)",
  "feed_type": "MJPEG | HLS | iframe | js_player | static_refresh | youtube_live | unknown",
  "legitimacy_score": "high | medium | low",
  "status": "live | dead | unknown",
  "last_verified": "ISO date string"
}
```

> `stream_url` must resolve to a live media stream, not an HTML page. Records with a `text/html` stream_url will fail popup preview rendering. `video_id` is required for YouTube feeds to enable thumbnail display in the popup before the user expands to the inline player.

Records missing `geometry.coordinates` will be skipped by `GeoJSONExportSkill` during export and will not appear on the map. Run `GeoEnrichmentSkill` to fill missing coordinates **before** the export step. `camera.geojson` is the canonical pipeline output — there is no separate `cameras.json` conversion step.

---

## Map Features

### Marker System
- Each camera is represented by a map marker
- Marker color encodes **live status**:
  - 🟢 Green = `status: live`
  - 🟡 Yellow = `status: unknown`
  - 🔴 Red = `status: dead`
- Marker icon encodes **feed type**:
  - 📹 = MJPEG / HLS / direct stream
  - 🖥️ = iframe embed / JS player
  - 📸 = static refresh
  - ▶️ = YouTube Live

### Clustering
- Cameras within close proximity are automatically clustered
- Cluster badge shows count; click to expand
- No cap on cameras per cluster — all cameras render

### Popup on Click
Each marker click opens a popup showing:
- Camera label and city
- Feed type and legitimacy score
- Last verified date
- **Direct "Watch" link** — opens the camera in a new tab
- Source directory reference

### Filter Panel
The map includes a side panel with filters:
- **By Status:** Live / Unknown / Dead
- **By Feed Type:** MJPEG, HLS, iframe, YouTube, etc.
- **By Legitimacy Score:** High / Medium
- **By Continent/Region**
- **Search by city or label**

### Layer Controls
- Toggle between: All Cameras / Live Only / High Confidence Only
- Heatmap overlay: density of cameras per region
- **Tier 1 city highlights** (blue circles, ~40 km radius) — 39 major global cities
- **Tier 2 city highlights** (amber circles, ~28 km radius) — 99 capitals and port cities
- **Tier 3 destination highlights** (green circles, ~18 km radius) — 50 tourist and remote locations
- **Blocked sources** (red ✕ markers) — 22 blocked services at their HQ coordinates; hover for name and reason
- All tier and blocked overlays are independent toggles drawn from fixed coordinate lists, unaffected by active camera filters

---

## Map Agent Responsibilities

`MapAgent` is responsible for generating and maintaining `map.html`. The following conditions must be met before `MapAgent` runs:

1. **All records must be geo-enriched** — `GeoEnrichmentSkill` must have run on any record missing lat/lon; records without coordinates will not appear on the map
2. **All records must have a verified status** — `HealthCheckSkill` must stamp `status` and `last_verified` before export
3. **`camera.geojson` must be valid GeoJSON** — `GeoJSONExportSkill` in `catalog.py` validates schema on every write; never manually edit `camera.geojson`
4. **Dead cameras** (`status: dead`) must be included — they render as red markers and should not be pruned from the export

---

## Updating the Map

The map reads `camera.geojson` dynamically on page load. To update the map:

1. Run `MaintenanceAgent` to refresh all `status` and `last_verified` fields
2. Run `CatalogAgent` export — this regenerates `camera.geojson` (primary output) and `cameras.md`
3. Replace `camera.geojson` in the same directory as `map.html`
4. Reload `map.html` in browser — no rebuild required

To load a different dataset without replacing the default file, use the **"Load GeoJSON"** file picker in the map header and select any `.geojson` file directly.

For offline / standalone use, `map.html` includes a bundled sample dataset of Tier 1 city cameras that renders when no `camera.geojson` is present.

---

## File Structure

```
/webcam-discovery/
├── map.html           ← Open this in browser
├── camera.geojson    ← Generated by CatalogAgent (place here; or load any .geojson via file picker)
├── AGENTS.md
├── SKILLS.md
├── CLAUDE.md
├── SOURCES.md
└── MAPS.md            ← This file
```

---

## Standalone Mode vs. Live Data Mode

| Mode | How it Works |
|------|-------------|
| **No data** | `map.html` opens and displays an empty-state overlay prompting the user to load a `.geojson` file — the map is never silently blank |
| **Default file** | Place `camera.geojson` in same directory; map auto-detects and loads it on open |
| **Custom file** | Click "Load GeoJSON" in the map header or empty-state overlay to open any `.geojson` file at runtime |
| **Hosted** | Serve the directory via any static web server; replace `camera.geojson` in place to update |

---

## Technical Stack

| Component | Library | Notes |
|-----------|---------|-------|
| Map rendering | Leaflet.js 1.9.x | Via CDN — no install |
| Marker clustering | Leaflet.markercluster | Via CDN |
| Heatmap overlay | Leaflet.heat | Via CDN |
| UI / filters | Vanilla JS + CSS | Embedded in single file |
| Primary data input | `camera.geojson` | Auto-loaded from same directory on page open |
| Custom data input | File picker (in-map UI) | User can load any `.geojson` file at runtime |

---

## GeoJSON Data Loading

The map supports two data loading modes, selectable at runtime with no page reload required:

### Mode 1 — Default Auto-Load
On page open, `map.html` attempts to `fetch('camera.geojson')` from its own directory. If found, it loads and renders it automatically. If not found, the map falls back to a bundled sample dataset of Tier 1 city cameras so the map is never blank.

### Mode 2 — Custom File Picker
A **"Load GeoJSON"** button is always visible in the map header. Clicking it opens the browser's native file picker filtered to `.geojson` files. The user can select:
- `camera.geojson` exported from a previous crawl session
- Any custom-named `.geojson` file (e.g., `europe_only.geojson`, `session_2025-03-10.geojson`)
- A `.geojson` generated by an external tool, provided it conforms to the required schema below

Once loaded, the active filename and camera count are displayed in the map header (e.g., `📂 europe_only.geojson — 847 cameras`). Switching files replaces all markers and resets filters without reloading the page.

---

## GeoJSON Format Requirements

`map.html` expects a standard GeoJSON `FeatureCollection` where each `Feature` has:

```json
{
  "type": "Feature",
  "geometry": {
    "type": "Point",
    "coordinates": [longitude, latitude]
  },
  "properties": {
    "id": "string",
    "label": "string",
    "city": "string",
    "country": "string",
    "continent": "string",
    "url": "string",
    "stream_url": "string — playable media stream URL",
    "video_id": "string — YouTube video ID (youtube_live only)",
    "feed_type": "MJPEG | HLS | iframe | js_player | static_refresh | youtube_live | unknown",
    "legitimacy_score": "high | medium | low",
    "status": "live | dead | unknown",
    "source_directory": "string",
    "last_verified": "ISO date string",
    "notes": "string"
  }
}
```

Records missing `geometry.coordinates` are silently skipped. All other missing properties render as "Unknown" in the popup.

> ⚠️ GeoJSON coordinate order is `[longitude, latitude]` per RFC 7946 — **not** `[latitude, longitude]`. `catalog.py`'s `export_geojson()` handles this automatically.

---

## Generating camera.geojson

`CatalogAgent` calls `GeoJSONExportSkill` (`export_geojson()` in `catalog.py`) at the end of every run. `camera.geojson` is the **primary and only required output** — there is no intermediate `cameras.json` step:

```python
def export_geojson(cameras: list[CameraRecord], path: str = "camera.geojson") -> dict:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c.longitude, c.latitude]},
            "properties": c.model_dump(exclude={"latitude", "longitude"})
        }
        for c in cameras if c.latitude and c.longitude
    ]
    geojson = {"type": "FeatureCollection", "features": features}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2, default=str)
    return {"exported": len(features), "path": path}
```

Place `camera.geojson` alongside `map.html` for auto-load on page open, or load it manually via the **📂 Load GeoJSON** file picker using any filename.

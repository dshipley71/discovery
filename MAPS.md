# MAPS.md — Interactive Webcam Discovery Map

## Overview

`map.html` is the visual interface for the public webcam discovery system.  It renders all
cataloged cameras onto an interactive world map with full filtering, table view, heatmap overlays,
geographic tier circles, and inline popup previews.

The map is always a **single self-contained HTML file** (Leaflet.js + MarkerCluster + Leaflet.heat
via CDN).  No server, no build step, and no runtime dependencies beyond CDN-hosted libraries are
required.  Open in any browser.

---

## Template Location

```
src/webcam_discovery/templates/map_template.html
```

`MapAgent` copies this file verbatim to the project root as `map.html`.  All dynamic behaviour
(data loading, filtering, rendering) lives entirely inside the HTML template.  No server-side
rendering or variable substitution is performed.

---

## Data Loading

The map supports two loading modes:

| Mode | Behaviour |
|------|-----------|
| **Auto-load (HTTP)** | On page open, the map fetches `camera.geojson` from the same directory. Requires a local HTTP server (`python3 -m http.server 8000`). |
| **Manual load (file://)** | An empty-state overlay guides the user to drag-and-drop or pick a `.geojson` file via the 📂 button. Works without any server. |

The active filename is always shown in the header so the user knows which dataset is loaded.

---

## GeoJSON Feature Schema

Each feature in the loaded GeoJSON must conform to this structure.
All fields are read by the map template.

```json
{
  "type": "Feature",
  "geometry": {
    "type": "Point",
    "coordinates": [longitude, latitude]
  },
  "properties": {
    "id":                 "unique-slug",
    "label":              "Times Square North View",
    "city":               "New York City",
    "region":             "New York",
    "country":            "United States",
    "continent":          "North America",
    "url":                "https://example.com/stream.m3u8",
    "stream_url":         "https://example.com/stream.m3u8",
    "direct_stream_url":  "https://example.com/direct.m3u8",
    "feed_type":          "HLS_stream",
    "playlist_type":      "master",
    "legitimacy_score":   "high",
    "status":             "live",
    "last_verified":      "2025-03-10",
    "source_directory":   "earthcam.com",
    "source_refs":        ["https://earthcam.com/..."],
    "requires_js":        false,
    "geo_restricted":     false,
    "notes":              ""
  }
}
```

Coordinate order is **`[longitude, latitude]`** per RFC 7946.

---

## Map Features

### Header bar
- Displays active filename, total camera count, and live / unknown / dead stat pills
- **📋 Table** button toggles the sortable data table
- **📂 Load GeoJSON** button opens the file picker

### Sidebar (left panel)
- **Search** — fly-to by city, label, country, or continent
- **Status chips** — Live / Unknown / Dead (toggle individually)
- **Confidence chips** — High / Medium / Low
- **Feed Type chips** — built dynamically from loaded data
- **Continent chips** — built dynamically from loaded data
- **Quick View** presets — All · Live Only · High Confidence
- **Layer toggles** — Heatmap · Tier 1 cities · Tier 2 cities · Tier 3 destinations · Blocked sources
- **Status Legend** — colour reference for markers
- **Feed Type Legend** — emoji reference (📹 MJPEG/HLS · 🖥️ embed · 📸 static · ▶️ YouTube)
- **Coverage Tier Legend** — blue Tier 1 · amber Tier 2 · green Tier 3 · red ✕ Blocked
- **Camera list** — scrollable list of up to 100 matching cameras; click to fly to marker

### Map area
- **Base tile** — CartoDB Dark Matter
- **Marker clustering** — auto-uncluster on zoom; custom blue cluster badges
- **Marker icons** — colour-coded by status (green = live, amber = unknown, red = dead) with feed-type emoji badge
- **Popup on click** — full schema info card with Watch link and all source refs

### Layer overlays (independent toggles)
| Layer | Description |
|-------|-------------|
| Heatmap | Camera density heatmap (Leaflet.heat) |
| Tier 1 circles | Blue circles on 39 priority global cities |
| Tier 2 circles | Amber circles on ~96 regional capitals & ports |
| Tier 3 circles | Green circles on ~50 tourist/remote destinations |
| Blocked sources | Red ✕ markers on blocked-source locations |

### Table view (📋 button)
- Sortable columns: Status, Label, City, Region, Country, Continent, Feed Type, Confidence, Verified, Source
- Toolbar filters: full-text search, Status select, Feed Type select, Continent select
- **⬇ Export CSV** — downloads filtered rows as a `.csv` file
- **📍 Map** button — switches back to map view and flies to that camera

### Empty-state overlay
Displayed when no GeoJSON is loaded:
- Drag-and-drop zone for `camera.geojson`
- 📂 file picker button
- `python3 -m http.server 8000` command with one-click copy
- Guidance to open `http://localhost:8000/map.html` for auto-load

---

## MapAgent Responsibilities

`MapAgent` must:

1. Copy `src/webcam_discovery/templates/map_template.html` to `<output_dir>/map.html`.
2. Confirm `camera.geojson` exists alongside `map.html` (warn if absent).
3. Log how to open the map (HTTP server command + direct-open fallback).
4. Be invoked by the pipeline after every `CatalogAgent` run that updates `camera.geojson`.

`MapAgent` must **not**:
- Perform any template variable substitution — the template is copied verbatim.
- Generate or modify `camera.geojson` — that is `CatalogAgent`'s responsibility.
- Require any Python web framework or build tool.

---

## Updating the Template

To update map visuals, features, or behaviour:

1. Edit `src/webcam_discovery/templates/map_template.html` directly.
2. Re-run `MapAgent` to deploy the updated file as `map.html`.
3. Commit both `map_template.html` and the regenerated `map.html` to version control.

The canonical reference for what `map.html` should look like is always
`src/webcam_discovery/templates/map_template.html`.

---

## Quick Start

```bash
# Serve and open (auto-load enabled):
cd /path/to/webcam-discovery
python3 -m http.server 8000
# open http://localhost:8000/map.html

# Regenerate map.html from the template:
python -m webcam_discovery.agents.map_agent --output-dir .

# Or from the pipeline:
python scripts/run_pipeline.py   # MapAgent runs as step 9
```

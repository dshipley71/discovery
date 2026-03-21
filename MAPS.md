# MAPS.md — Interactive HLS Camera Map

## Overview

`map.html` is the visual interface for the public webcam discovery system. It renders all cataloged
**HLS (.m3u8) cameras** onto an interactive world map, organized by geographic tier, feed subtype,
and live status.

The map is always a single self-contained HTML file (Leaflet.js). No server, no build step, and no
runtime dependencies beyond CDN-hosted libraries are required.

## Data Requirements

Each feature loaded into the map must contain direct HLS playback data:

```json
{
  "id": "string — unique slug",
  "label": "string — human-readable name",
  "city": "string",
  "country": "string",
  "latitude": "number — decimal degrees",
  "longitude": "number — decimal degrees",
  "url": "string — direct .m3u8 stream URL",
  "feed_type": "HLS_master | HLS_stream | unknown",
  "playlist_type": "master | media | null",
  "legitimacy_score": "high | medium | low",
  "status": "live | dead | unknown",
  "last_verified": "ISO date string"
}
```

`url` must resolve directly to a playable `.m3u8` playlist. HTML watch pages, iframe embeds,
YouTube links, JPEG refresh endpoints, and any non-HLS transport are invalid for map playback.

## Map Features

- Marker color reflects `status`.
- Feed-type filters are limited to HLS playlist subtypes.
- Clicking a marker opens an HLS player modal powered by `hls.js` (or native Safari HLS support).
- The file picker can load any compatible GeoJSON export from the HLS-only catalog.

## Map Agent Responsibilities

`MapAgent` must:

1. Auto-load `camera.geojson` when present.
2. Show an empty-state overlay when no HLS dataset is available.
3. Render every feature with valid coordinates.
4. Open the record `url` as the HLS playback source in the modal.
5. Preserve status, legitimacy, and continent filters.

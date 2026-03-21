# SKILLS.md — Discovery and Validation Skills

## FeedExtractionSkill

**Purpose:** Extract direct HLS `.m3u8` URLs from public camera pages.

**Process:**
1. Fetch page HTML.
2. Look for direct `.m3u8` references in `<source>`, `<video>`, `data-*`, and player scripts.
3. Follow one iframe level only when needed to discover a direct `.m3u8` URL.
4. Return `direct_stream_url`, `embed_url`, and `feed_type_hint` where `feed_type_hint` is HLS-only.

## FeedValidationSkill

**Purpose:** Confirm that a candidate URL is a publicly reachable HLS playlist.

**Process:**
1. Reject any non-HTTP URL.
2. Reject any URL that does not point to `.m3u8`.
3. GET the first bytes of the playlist and confirm HLS markers such as `#EXTM3U`.
4. Classify the playlist as `HLS_master` or `HLS_stream`.
5. Reject any HTML response, login redirect, auth wall, or ambiguous public access pattern.

## FeedTypeClassificationSkill

Only the following feed types are valid:

| Feed Type | Indicators |
|-----------|-----------|
| `HLS_master` | Playlist contains `#EXT-X-STREAM-INF` |
| `HLS_stream` | Playlist contains `#EXTINF` or `#EXT-X-TARGETDURATION` |
| `unknown` | HLS could not be classified further |

## HealthCheckSkill

**Purpose:** Re-verify cataloged HLS `.m3u8` URLs.

- Use `ffprobe` as the primary signal-level liveness check.
- Fall back to HTTP HEAD only when `ffprobe` is unavailable.
- Mark non-HLS records as invalid if they somehow enter the catalog.

## GeoJSONExportSkill

Export only direct HLS camera records. `properties.url` is the playback URL and must point to
`.m3u8`.

## MapRenderingSkill

Generate `map.html` for HLS playback only. Use HTML5 `<video>` + `hls.js` (or native Safari HLS)
for modal playback and do not render alternate player types.

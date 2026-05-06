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
5. Classify failures as `restricted`, `timeout`, `offline_http`, `decode_failed`, `dead`, or `unknown` rather than silently discarding them.

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


## AgenticCandidateHandoffSkill

**Purpose:** Preserve discovered direct HLS candidates as the authoritative discovery-to-validation handoff.

**Process:**
1. Write all discovered candidate evidence to `candidates/agentic_candidates.jsonl`.
2. Load direct `.m3u8` candidates from that artifact dynamically; never assume a fixed count.
3. Normalize stream URLs while preserving playback-critical query parameters.
4. Deduplicate by normalized stream URL or stable source-provided camera identity when available.
5. Write `candidates/agentic_candidates_unique.jsonl` and `candidates/agentic_candidates_validation_handoff.jsonl` so the operator can audit exactly what proceeded to validation and why.

Page-level scope decisions are not final camera inclusion/exclusion decisions. Candidate-level scope decisions must use the full evidence package and tolerate review/fallback decisions for plausible direct HLS streams.

## Geocoding and Coordinate Precision

Camera coordinates must be derived from evidence, not invented. Prefer source/API coordinates, then page/listing metadata, nearby labels/text, existing source page coordinates, LLM candidate context, and finally explicitly labeled scope-level fallback coordinates. Records include `geocode_source`, `geocode_confidence`, `geocode_precision`, and `geocode_reason` when available.

## QueryClarificationSkill / QueryClarificationAgent

**Purpose:** Detect ambiguous or underspecified user queries before discovery and ask at most one clarification turn.

**Inputs:** user query and planner plan.

**Outputs:** `logs/query_clarification.json` with `needs_clarification`, `clarification_type`, `reason`, `questions`, `candidate_interpretations`, `adjusted_query`, `provider`, `model`, and raw LLM response.

**Rules:**

1. Ask only when the query is ambiguous/conflicting or lacks a searchable place/source indicator.
2. Ask no more than three questions and only one clarification turn.
3. If a clarification answer is supplied, build a clarified query and continue to scope enforcement.
4. If no answer is supplied in non-interactive execution, stop before discovery with `status=needs_clarification`.
5. If the answer is still insufficient, do not ask again; the normal scope enforcement rules apply.

## FinalValidationArtifactSkill

**Purpose:** Keep validation artifacts reconciled after all classification stages.

`http_hls_probe_results.jsonl` captures early HTTP/HLS probing. `validation_results.jsonl` is overwritten with final records after playlist/ffprobe/visual analysis and caps. `camera_status_summary.json` must be derived from final validation rows and match `run_summary.json.validation`.

# Public Webcam Discovery System

Discovers, validates, and maps publicly accessible HLS (.m3u8) webcams worldwide — no credentials, no logins, no private feeds.

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Run full pipeline
python scripts/run_pipeline.py

# Open map (after pipeline runs)
open map.html          # macOS
xdg-open map.html      # Linux
# or: python -m http.server 8000, then open http://localhost:8000/map.html

# Validate this phase in Colab with real public data; do not add synthetic pytest runs.
```

## Architecture

| Layer | Files |
|-------|-------|
| Agent docs | `AGENTS.md`, `SKILLS.md`, `SOURCES.md`, `MAPS.md` |
| Claude Code guide | `CLAUDE.md` |
| Package | `src/webcam_discovery/` |
| Agents | `src/webcam_discovery/agents/` |
| Skills | `src/webcam_discovery/skills/` |
| CLI scripts | `scripts/` |
| Notebooks | `notebooks/` (Colab / Jupyter / SageMaker) |
| Map output | `map.html` + `camera.geojson` (project root) |
| Logs | `logs/` |

See `docs/DIRECTORY_STRUCTURE.md` for the full layout and rationale.

## Validation and maintenance notes

- HLS discovery targets the standard `.m3u8` playlist extension. Inputs or docs that mention `.h3u8` should be treated as a typo, not a supported stream type.
- `ValidationAgent` uses HTTP/HLS probing plus optional `ffprobe`/`ffmpeg` frame analysis to classify streams as `live`, `dead`, `unknown`, `restricted`, `timeout`, `offline_http`, or `decode_failed`. It writes structured probe logs to `logs/validation_results.jsonl`, `logs/camera_status_summary.json`, and, when enabled, `logs/ffprobe_validation.jsonl`.
- `MaintenanceAgent` keeps repeatedly failing links already present in the catalog and writes them to a validation-review queue instead of auto-pruning them automatically.

- The pipeline is HLS-only: discovery, validation, maintenance, and map playback all require direct `.m3u8` URLs.


## Agentic candidate validation handoff

`webcam-discovery run-agentic` now treats `candidates/agentic_candidates.jsonl` as the durable handoff from discovery into validation. Search-result scope decisions are page-triage signals only: they control which pages are expanded, but they are not final camera truth once direct `.m3u8` candidates have already been discovered.

The handoff flow is:

```text
user query → PlannerAgent → LLM scope inference → scoped search/discovery
→ candidates/agentic_candidates.jsonl
→ candidates/agentic_candidates_unique.jsonl
→ candidates/agentic_candidates_validation_handoff.jsonl
→ ValidationAgent → geocoding/enrichment → camera.geojson + cameras.md + map.html
```

Important behavior:

- Candidate counts are dynamic. Do not expect a fixed number of streams from any query.
- The user query defines the location/place/scope; no city, state, country, agency, or landmark is hardcoded.
- HLS URL formats vary by source and location. The system normalizes stream URLs for dedupe but does not require any source-specific prefix, camera-ID pattern, CDN hostname, or path convention.
- Stream-candidate scope failures fall back to `review` by default and can still proceed to validation when the candidate is a plausible direct HLS stream. Every fallback is written to `logs/stream_candidate_scope_decisions.jsonl`.
- Deduplication is by normalized direct stream URL or stable source-provided camera identity when available, not by approximate coordinates, labels, or city names. Multiple distinct cameras may share the same approximate coordinates.

Key artifacts to inspect after a run:

| Artifact | Purpose |
|---|---|
| `candidates/agentic_candidates.jsonl` | Raw discovered candidates before validation. |
| `candidates/agentic_candidates_unique.jsonl` | Unique direct HLS candidates after normalization/deduplication. |
| `candidates/agentic_candidates_validation_handoff.jsonl` | Audit of what was allowed into validation and why. |
| `logs/search_result_scope_decisions.jsonl` | Page-level scope decisions used only for expansion triage. |
| `logs/stream_candidate_scope_decisions.jsonl` | Candidate-level decisions, including review/fallback allowances. |
| `logs/validation_results.jsonl` | HTTP/HLS status for each validated stream. |
| `logs/geocoding_results.jsonl` | Coordinate source, confidence, precision, and reason. |
| `logs/run_summary.json` | Dynamic counts for handoff, scope gates, validation, geocoding, and catalog output. |

Recommended Colab command shape:

```bash
USER_QUERY="<user-provided scoped camera discovery query>"
RUN_DIR="/content/wcd-run"
webcam-discovery run-agentic "$USER_QUERY" \
  --output-dir "$RUN_DIR" \
  --enable-feed-discovery \
  --max-feed-endpoints 500 \
  --max-feed-records 15000 \
  --max-stream-candidates 10000 \
  --per-source-stream-cap 2000 \
  --preserve-direct-streams \
  --max-streams 25 \
  --max-search-queries 4 \
  --max-search-results-per-query 5 \
  --llm-provider ollama \
  --llm-model gemma4:31b-cloud \
  --disable-ffprobe-validation
```

`--disable-ffprobe-validation` skips frame-level ffprobe checks but keeps HTTP/HLS validation active. `--disable-validation` is debug-only; it writes unvalidated review artifacts and should not be used for production catalog/map output.

## Agentic discovery (real LLM planner)

Use the new command to run a natural-language, planner-driven workflow:

```bash
webcam-discovery run-agentic "Get me public HLS cameras near <specific place or landmark>" \
  --max-search-queries 25 \
  --max-search-results-per-query 10 \
  --max-candidates 20 --max-streams 5 --enable-visual-analysis
```

### LLM planner setup (no mock planner)

Default planner backend is Ollama Cloud.

```bash
export WCD_PLANNER_PROVIDER=ollama
export WCD_OLLAMA_API_KEY="<your-real-ollama-cloud-key>"
export WCD_PLANNER_MODEL="gemma3:27b"
# optional:
export WCD_PLANNER_BASE_URL="https://ollama.com"
```

For OpenAI-compatible backends:

```bash
export WCD_PLANNER_PROVIDER=openai-compatible
export WCD_PLANNER_BASE_URL="https://<provider-host>"
export WCD_PLANNER_API_KEY="<real-api-key>"
export WCD_PLANNER_MODEL="<chat-model-name>"
```

If required planner credentials are missing, `run-agentic` fails clearly and exits.

### Optional MemWeave memory

```bash
pip install -e ".[memory]"
webcam-discovery run-agentic "..." --enable-memory
```

Memory writes to `memory/runs/*.md` and `logs/memory_updates.jsonl`.

### Optional visual stream analysis

```bash
webcam-discovery run-agentic "..." --enable-visual-analysis
```

Writes `logs/visual_stream_analysis.jsonl` with live/dead/unknown plus substatus and metrics.

### Developer/debug validation controls

```text
--disable-ffprobe-validation
    Developer/debug flag. Runs normal validation but skips ffprobe/ffmpeg frame-level checks.
    HTTP/HLS probing, robots.txt checks, playlist checks, geocoding, and catalog handling still run
    for records that pass the non-ffprobe validation path. Useful for faster scope-enforcement
    and candidate-funnel debugging.

--disable-validation
    Developer/debug flag. Skips ValidationAgent entirely. The run may write unvalidated candidates
    for review, but they are not cataloged as cameras and are not written as mapped GeoJSON features.
    Do not use for production catalog generation.
```

When `--disable-validation` is used, the CLI writes `logs/validation_skipped.json` and may write
`candidates/unvalidated_stream_candidates.jsonl`; `logs/run_summary.json` reports validation as
skipped with zero validated and mapped camera records.

### Optional video summarization

```bash
pip install -e ".[video-summary]"
webcam-discovery run-agentic "..." --enable-video-summary
```

Writes `logs/video_summaries.jsonl` from bounded frame/audio samples.

### New logs

- `logs/search_plan.json`
- `logs/search_queries.jsonl`
- `logs/search_results.jsonl`
- `candidates/agentic_candidates.jsonl`
- `logs/planner_runs.jsonl`
- `logs/visual_stream_analysis.jsonl`
- `logs/video_summaries.jsonl`
- `logs/memory_updates.jsonl`

## One-Time Query Clarification Preflight

`webcam-discovery run-agentic` now performs an LLM-based clarification preflight before discovery. The preflight is designed for ambiguous or underspecified natural-language requests, not for broad heuristic parsing. For example, `Get me all traffic cameras from Paris` should stop and ask one concise clarification question such as whether the user means Paris, France, Paris, Texas, or another Paris. A query such as `Get me all traffic camera` should stop and ask for a specific place, landmark, agency, coordinates, hostname, IP address, or public website.

Clarification is asked only once. In an interactive terminal, the CLI prompts for one answer. In Colab or other non-interactive runs, the app writes `logs/query_clarification.json` and `logs/run_summary.json` with `status=needs_clarification`, then exits before search/feed discovery. To run non-interactively with the answer already supplied, pass:

```bash
webcam-discovery run-agentic "Get me all traffic cameras from Paris" \
  --clarification-answer "Paris, France"
```

If the single clarification answer is still insufficient, the normal LLM scope enforcement rules apply and discovery stops before broad search. Use `--disable-clarification` only for developer/debug validation of the underlying scope enforcement behavior.

## Validation Status Accounting

Final validation artifacts are now separated from early HTTP/HLS probe artifacts:

- `logs/http_hls_probe_results.jsonl` and `logs/http_hls_probe_summary.json` capture the initial HTTP/HLS probe only.
- `logs/validation_results.jsonl` and `logs/camera_status_summary.json` capture final stream status after playlist/ffprobe/visual classification and any configured caps.
- `logs/run_summary.json`, `logs/camera_status_summary.json`, and `camera.geojson` should reconcile: final validation counts should match run-summary validation counts, and catalog feature counts should match GeoJSON feature counts.

Validation caps are applied before validation. If `--max-validation-candidates` is not set, `--max-streams` is used as the validation handoff cap for this debug path. Dropped candidates are written to `candidates/agentic_candidates_validation_dropped.jsonl` or `candidates/catalog_cap_dropped.jsonl` with explicit cap/drop reasons.

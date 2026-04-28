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

# Run tests
pytest tests/ -q
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
| Tests | `tests/` (headless, mocked HTTP) |
| Map output | `map.html` + `camera.geojson` (project root) |
| Logs | `logs/` |

See `docs/DIRECTORY_STRUCTURE.md` for the full layout and rationale.

## Validation and maintenance notes

- HLS discovery targets the standard `.m3u8` playlist extension. Inputs or docs that mention `.h3u8` should be treated as a typo, not a supported stream type.
- `ValidationAgent` uses HTTP probing plus `ffprobe`/`ffmpeg` frame analysis to classify streams as `live`, `unknown`, or `dead`, writes structured probe logs to `logs/validation_results.jsonl` and `logs/ffprobe_validation.jsonl`, and excludes confirmed-dead URLs from new catalog exports.
- `MaintenanceAgent` keeps repeatedly failing links already present in the catalog and writes them to a validation-review queue instead of auto-pruning them automatically.

- The pipeline is HLS-only: discovery, validation, maintenance, and map playback all require direct `.m3u8` URLs.

## Agentic discovery (real LLM planner)

Use the new command to run a natural-language, planner-driven workflow:

```bash
webcam-discovery run-agentic "Get me all the live traffic cameras from Pennsylvania" \
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

### Optional video summarization

```bash
pip install -e ".[video-summary]"
webcam-discovery run-agentic "..." --enable-video-summary
```

Writes `logs/video_summaries.jsonl` from bounded frame/audio samples.

### New logs

- `logs/planner_runs.jsonl`
- `logs/visual_stream_analysis.jsonl`
- `logs/video_summaries.jsonl`
- `logs/memory_updates.jsonl`

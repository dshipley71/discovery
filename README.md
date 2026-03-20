# Public Webcam Discovery System

Discovers, validates, and maps publicly accessible webcams worldwide — no credentials, no logins, no private feeds.

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
- `ValidationAgent` uses HTTP probing plus `ffprobe`/`ffmpeg` frame analysis to classify streams as `live`, `unknown`, or `dead`.
- `MaintenanceAgent` keeps repeatedly failing links in the catalog and writes them to a validation-review queue instead of auto-pruning them.

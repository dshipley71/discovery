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

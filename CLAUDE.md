# Public Webcam Discovery System — Claude Code Guide

## Project
Discovers, validates, and maps publicly accessible webcams worldwide.
No credentials, no logins, no private or surveillance feeds.
Every camera in the catalog must be viewable by anyone, right now, with no strings attached.

## Install
```bash
pip install -e ".[dev]"          # local development dependencies
pip install -e ".[notebooks]"    # Colab / SageMaker
```

## Run full pipeline
```bash
python scripts/run_pipeline.py
```

## Run individual agents
```bash
python scripts/run_discovery.py  --tier 1 --output candidates/
python scripts/run_validation.py --input candidates/candidates.jsonl
python scripts/run_catalog.py    --input candidates/validated.jsonl --output .
python scripts/run_maintenance.py --catalog camera.geojson
```

## CLI entry points (after pip install)
```bash
wcd-pipeline
wcd-discover --tier 1
wcd-validate --input candidates/candidates.jsonl
wcd-catalog  --input candidates/validated.jsonl
wcd-maintain --catalog camera.geojson
```

## Verify generated code for this phase
```bash
python -m compileall -q src
python -m json.tool notebooks/camera_discovery.ipynb >/dev/null
# Functional validation is performed in Colab against real public data.
```

## Key output files (project root — map.html and camera.geojson must stay co-located)
- `camera.geojson`  — canonical pipeline output; auto-loaded by map.html
- `cameras.md`      — human-readable link list
- `map.html`        — interactive Leaflet.js map; open in browser

## Serve map locally (required for auto-load of camera.geojson)
```bash
python -m http.server 8000
# open http://localhost:8000/map.html
```

## Runtime directories (all git-ignored)
- `logs/`        — pipeline.log + maintenance_log.jsonl
- `snapshots/`   — dated camera_YYYY-MM-DD.geojson backups
- `candidates/`  — inter-agent JSONL handoff files

## Architecture
- `schemas.py`   — CameraRecord, CameraCandidate (shared Pydantic models; single source of truth)
- `agents/`      — 5 agents: directory_crawler, search_agent, validator, catalog, maintenance
- `skills/`      — 13 skills grouped by function
- `pipeline.py`  — orchestrator; imports and sequences all agents in execution order

## Agent execution order
1. DirectoryAgent   (directory_crawler.py) → CameraCandidate list
2. SearchAgent      (search_agent.py)      → additional CameraCandidate objects
3. ValidationAgent  (validator.py)         → validated CameraRecord list
4. CatalogAgent     (catalog.py)           → camera.geojson + cameras.md
5. MaintenanceAgent (maintenance.py)       → scheduled status checks + validation-review reporting

## Code generation rules (from CLAUDE.md — always follow these)
- Always generate complete, runnable scripts — no pseudocode, no placeholder functions
- Use pydantic models for all data structures that cross agent boundaries
- Use httpx with asyncio for all network I/O — never requests for bulk operations
- Use loguru for all logging with structured context (URL, agent, timestamp)
- Never hardcode credentials, API keys, or tokens
- Include a __main__ block in every script for standalone testing
- Write docstrings on every class and public method

## Standard module header
```python
#!/usr/bin/env python3
"""
module_name.py — Brief description.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations
import asyncio
from loguru import logger
from pydantic import BaseModel
```

## Never
- Hardcode credentials or API keys
- Edit camera.geojson directly — always regenerate via CatalogAgent
- Place map.html and camera.geojson in different directories
- Use requests for bulk HTTP — use httpx async
- Pass raw dicts between agents — always use CameraCandidate / CameraRecord

## Reference docs (read these before generating code for the relevant layer)
- `AGENTS.md`  — agent roles, execution order, full output schema
- `SKILLS.md`  — all 13 skills, interfaces, async patterns
- `SOURCES.md` — source allow/block lists (Tier 1–5 + blocked)
- `MAPS.md`    — map spec, GeoJSON format, field requirements


## Current agentic handoff rules

- `candidates/agentic_candidates.jsonl` is the primary validation handoff after discovery.
- Search-result scope decisions only gate page expansion; they must not be treated as final camera truth for direct HLS candidates already discovered.
- Stream-candidate fallback should default to review/validation-allowed for plausible direct `.m3u8` streams and must be auditable in `logs/stream_candidate_scope_decisions.jsonl`.
- Deduplicate cameras by normalized stream URL or stable source-provided camera identity, never by approximate coordinates alone.
- Keep the app location-agnostic: infer scope from the user query and never special-case a test location or HLS URL pattern.

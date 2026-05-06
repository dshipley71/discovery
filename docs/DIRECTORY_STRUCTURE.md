# Directory Structure — Public Webcam Discovery System
## Optimized for Claude Code · Google Colab · CLI · Jupyter / SageMaker

---

## Guiding Principles

| Principle | Implementation |
|-----------|---------------|
| **Claude Code reads `CLAUDE.md` automatically** | Root `CLAUDE.md` + sub-directory `CLAUDE.md` files in `agents/`, `skills/`, and `tests/` |
| **Installable package** | `pyproject.toml` + `src/` layout → `pip install -e .` works in Colab and SageMaker |
| **Single source of truth for schemas** | `webcam_discovery/schemas.py` — all agents and skills import from here |
| **Agent boundaries are explicit** | Each agent is one file; skills are grouped by function |
| **Environment-agnostic config** | `config.py` reads from `.env` / environment variables; no hardcoded paths |
| **Pipeline is a first-class object** | `pipeline.py` is the orchestrator; also runnable as a CLI entry point |
| **Map outputs are at project root** | `map.html` and `camera.geojson` always co-located at `webcam-discovery/` root so the map's `fetch('camera.geojson')` resolves correctly when served from the project directory |
| **Runtime directories are at project root** | `logs/`, `snapshots/`, `candidates/` are top-level and git-ignored — easy to find, easy to clean |
| **Notebooks are environment-portable** | `notebooks/` uses a self-contained bootstrap cell for Colab/SageMaker self-setup |

---

## Full Directory Layout

```
webcam-discovery/
│
├── CLAUDE.md                          ← AUTO-LOADED by Claude Code — project overview,
│                                         key commands, architecture summary, agent map
│
├── AGENTS.md                          ← Agent roles, execution order, output schema
├── SKILLS.md                          ← Skills library reference
├── SOURCES.md                         ← Source allow/block lists
├── MAPS.md                            ← Map spec and GeoJSON requirements
│
├── pyproject.toml                     ← Package definition, dependencies, CLI entry points
├── .env.example                       ← Template: env vars, never commit actual .env
├── .gitignore
├── README.md
│
├── map.html                           ← MapAgent output — open in browser; auto-fetches
│                                         camera.geojson from the same directory on load
├── camera.geojson                     ← CatalogAgent primary output — loaded by map.html
├── cameras.md                         ← CatalogAgent human-readable link list
│
├── src/
│   └── webcam_discovery/              ← Installable Python package
│       ├── __init__.py
│       ├── config.py                  ← Settings via pydantic-settings; reads .env
│       ├── schemas.py                 ← CameraRecord + CameraCandidate Pydantic models
│       │                                (single source of truth — all agents import from here)
│       ├── pipeline.py                ← Orchestrator: runs all agents in execution order
│       │
│       ├── agents/                    ← One file per agent
│       │   ├── __init__.py
│       │   ├── CLAUDE.md              ← Agent-specific Claude Code context:
│       │   │                             execution order, input/output contracts,
│       │   │                             how to add a new agent
│       │   ├── directory_crawler.py   ← DirectoryAgent
│       │   ├── search_agent.py        ← SearchAgent
│       │   ├── validator.py           ← ValidationAgent
│       │   ├── catalog.py             ← CatalogAgent (includes export_geojson())
│       │   └── maintenance.py         ← MaintenanceAgent
│       │
│       └── skills/                    ← Skills grouped by function
│           ├── __init__.py
│           ├── CLAUDE.md              ← Skills-specific Claude Code context:
│           │                             skill interface contract (run() method),
│           │                             how to add a new skill
│           ├── traversal.py           ← DirectoryTraversalSkill, FeedExtractionSkill
│           ├── validation.py          ← FeedValidationSkill, RobotsPolicySkill,
│           │                             FeedTypeClassificationSkill
│           ├── search.py              ← QueryGenerationSkill, LocaleNavigationSkill,
│           │                             SourceDiscoverySkill
│           ├── catalog.py             ← DeduplicationSkill, GeoEnrichmentSkill,
│           │                             GeoJSONExportSkill
│           ├── maintenance.py         ← HealthCheckSkill
│           └── map_rendering.py       ← MapRenderingSkill
│
├── scripts/                           ← CLI entry points (also registered in pyproject.toml)
│   ├── run_pipeline.py                ← Full end-to-end pipeline
│   ├── run_discovery.py               ← DirectoryAgent + SearchAgent only
│   ├── run_validation.py              ← ValidationAgent only (accepts candidates.jsonl)
│   ├── run_catalog.py                 ← CatalogAgent only (accepts validated.jsonl)
│   └── run_maintenance.py             ← MaintenanceAgent only (reads camera.geojson)
│
├── notebooks/                         ← Colab / Jupyter / SageMaker — interactive runs
│   ├── 00_setup.ipynb                 ← pip install -e .., env setup, smoke tests
│   ├── 01_discovery.ipynb             ← DirectoryAgent + SearchAgent interactive run
│   ├── 02_validation.ipynb            ← ValidationAgent with inspection of candidates
│   ├── 03_catalog_and_map.ipynb       ← CatalogAgent → camera.geojson → map preview
│   └── 04_maintenance.ipynb           ← HealthCheckSkill batch checks + status analysis
│
├── tests/                             ← Headless unit tests — run by Claude Code via pytest
│   ├── CLAUDE.md                      ← Scope boundary: what belongs here vs notebooks
│   ├── __init__.py
│   ├── conftest.py                    ← Shared fixtures (sample CameraRecord, mock httpx)
│   ├── test_schemas.py                ← CameraRecord / CameraCandidate validation rules
│   ├── agents/
│   │   ├── test_validator.py          ← Contract: HEAD check logic, score assignment
│   │   └── test_catalog.py            ← Contract: dedup, slug generation, GeoJSON export
│   └── skills/
│       ├── test_feed_validation.py    ← Content-type checks, media vs HTML rejection
│       ├── test_geo_enrichment.py     ← Coord enrichment, missing-coord handling
│       └── test_deduplication.py      ← Fuzzy match thresholds, id collision handling
│
├── docs/                              ← Human reference only — not read by agents or CI
│   └── DIRECTORY_STRUCTURE.md        ← This file
│
├── logs/                              ← Runtime logs — git-ignored
│   ├── maintenance_log.jsonl          ← Append-only audit trail (MaintenanceAgent)
│   └── pipeline.log                   ← Loguru output across all modules
│
├── snapshots/                         ← Dated GeoJSON backups — git-ignored
│   └── camera_2025-03-10.geojson     ← Point-in-time copy for rollback / diff
│
└── candidates/                        ← Inter-agent pipeline state — git-ignored
    ├── candidates.jsonl               ← Raw candidates (DirectoryAgent + SearchAgent out)
    └── validated.jsonl                ← Validated records (ValidationAgent out)
```

---

## Why `tests/` and `notebooks/` Both Exist

They test different things and run in different contexts:

| | `notebooks/` | `tests/` |
|---|---|---|
| **Who runs it** | You, interactively | Claude Code, headlessly via `pytest` |
| **What it tests** | Full agent runs against live sources — happy-path, real data | Schema contracts, edge cases, error handling, skill logic in isolation |
| **Network required** | Yes | No — all HTTP is mocked with `respx` |
| **When it runs** | During development and exploration | Every time Claude Code generates or modifies a module |
| **What it catches** | Wrong data from real sources, bad URL patterns | Schema regressions, validation logic bugs, broken inter-agent contracts |

The notebooks cover end-to-end behavior against live sources. The tests cover **correctness of the contracts** that agents rely on each other to uphold. Claude Code uses `pytest` to verify generated code immediately after writing it — it cannot execute notebooks headlessly, so `tests/` is the feedback loop for agentic code generation.

The `tests/` scope is deliberately narrow: **no agent that makes real HTTP calls is tested here.** `DirectoryAgent` and `SearchAgent` are covered only in notebooks because their behavior depends entirely on live third-party sources. What `tests/` covers is everything with a deterministic, mockable contract: schema validation, feed classification logic, deduplication thresholds, GeoJSON export correctness, and robots.txt parsing.

---

## Key Files in Detail

### `CLAUDE.md` (root) — Claude Code entry point

Claude Code auto-loads this on every invocation. It must contain:

```markdown
## Project
Public Webcam Discovery System — discovers, validates, and maps
publicly accessible webcams worldwide. No credentials, no logins.

## Install
pip install -e ".[dev]"          # local dev
pip install -e ".[notebooks]"    # Colab / SageMaker

## Run full pipeline (outputs written to project root)
python scripts/run_pipeline.py

## Run individual agents
python scripts/run_discovery.py  --tier 1 --output candidates/
python scripts/run_validation.py --input candidates/candidates.jsonl
python scripts/run_catalog.py    --input candidates/validated.jsonl --output .
python scripts/run_maintenance.py --catalog camera.geojson

## Verify generated code
pytest tests/ -q

## Key output files (project root — camera.geojson and map.html must stay co-located)
camera.geojson    — canonical pipeline output; loaded by map.html
cameras.md        — human-readable link list
map.html          — interactive map; open in browser or serve via http.server

## Runtime directories (all git-ignored)
logs/             — pipeline.log + maintenance_log.jsonl
snapshots/        — dated camera_YYYY-MM-DD.geojson backups
candidates/       — inter-agent JSONL handoff files

## Architecture
- schemas.py      — CameraRecord, CameraCandidate (shared Pydantic models)
- agents/         — 5 agents: directory_crawler, search_agent, validator,
                    catalog, maintenance
- skills/         — 13 skills grouped by function (traversal, validation,
                    search, catalog, maintenance, map_rendering)
- pipeline.py     — orchestrator; imports and sequences all agents

## Never
- Hardcode credentials or API keys
- Edit camera.geojson directly — always regenerate via CatalogAgent
- Place map.html and camera.geojson in different directories
```

---

### `tests/CLAUDE.md` — Scope boundary for Claude Code

```markdown
## Test Scope

Unit tests only — no live network calls, no real sources.
All HTTP is mocked with respx fixtures defined in conftest.py.

## What belongs here
- Schema validation edge cases (CameraRecord, CameraCandidate)
- Skill logic that is deterministic and mockable:
    FeedValidationSkill, GeoEnrichmentSkill, DeduplicationSkill,
    FeedTypeClassificationSkill, RobotsPolicySkill
- CatalogAgent: dedup, slug generation, GeoJSON coordinate order
- ValidationAgent: score assignment rules, content-type rejection logic

## What does NOT belong here
- DirectoryAgent end-to-end (live sources → test in notebooks/01)
- SearchAgent end-to-end (live sources → test in notebooks/01)
- MaintenanceAgent scheduling (live HEAD checks → test in notebooks/04)
- Any test that requires a real URL to pass

## Running tests (Claude Code uses this command)
pytest tests/ -q
pytest tests/skills/test_feed_validation.py -v   # single file
pytest tests/ -k "dedup" -v                      # by keyword
```

---

### `agents/CLAUDE.md` — Agent-level Claude Code context

```markdown
## Agent Directory

Each agent is a standalone Python module with a run() async entry
point and a main() function for CLI invocation.

## Execution Order (from AGENTS.md)
1. directory_crawler.py   → produces CameraCandidate list
2. search_agent.py        → appends additional CameraCandidate objects
3. validator.py           → produces validated CameraRecord list
4. catalog.py             → deduplicates + exports camera.geojson + cameras.md
5. maintenance.py         → scheduled liveness checks + pruning

## Inter-agent Contract
- Agents exchange data as lists of Pydantic objects (CameraCandidate,
  CameraRecord) defined in webcam_discovery/schemas.py
- For multi-step CLI runs, exchange via JSONL in candidates/ (project root)
- Never pass raw dicts between agents — always use the schema types

## Output paths
catalog.py writes camera.geojson and cameras.md to project root (.)
maintenance.py reads camera.geojson from project root
All log output goes to logs/ (created automatically if missing)

## Adding a New Agent
1. Create the .py file here following the standard module header (CLAUDE.md root)
2. Add the agent class with async run() method
3. Add a main() block for standalone execution
4. Register in pipeline.py at the correct execution step
5. Add an entry point in pyproject.toml [project.scripts]
6. Update AGENTS.md agent roster and execution order table
```

---

### `skills/CLAUDE.md` — Skills-level Claude Code context

```markdown
## Skills Directory

Each skill is a class with a single public run() method. Async skills
use async def run(). All inputs/outputs are Pydantic models.

## Skill-to-File Mapping
traversal.py     — DirectoryTraversalSkill, FeedExtractionSkill
validation.py    — FeedValidationSkill, RobotsPolicySkill, FeedTypeClassificationSkill
search.py        — QueryGenerationSkill, LocaleNavigationSkill, SourceDiscoverySkill
catalog.py       — DeduplicationSkill, GeoEnrichmentSkill, GeoJSONExportSkill
maintenance.py   — HealthCheckSkill
map_rendering.py — MapRenderingSkill

## Interface Contract
class SomeSkill:
    def run(self, input: SomeInput) -> SomeOutput:
        ...
    # OR for network I/O:
    async def run(self, input: SomeInput) -> SomeOutput:
        ...

## Adding a New Skill
1. Add class to the appropriate grouped file (or new file if new category)
2. Define SkillInput and SkillOutput as Pydantic models
3. Implement run() or async run()
4. Export from skills/__init__.py
5. Update SKILLS.md skill index table
```

---

### `config.py` — Environment-aware settings

```python
from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    # Root-level output files (map.html and camera.geojson must be co-located)
    catalog_output_dir: Path = Path(".")        # camera.geojson, cameras.md, map.html

    # Runtime directories at project root (all git-ignored)
    log_dir:            Path = Path("logs")
    snapshot_dir:       Path = Path("snapshots")
    candidates_dir:     Path = Path("candidates")

    # Pipeline behaviour
    max_concurrency:    int   = 10
    request_timeout:    float = 5.0
    min_legitimacy:     str   = "medium"   # "high" | "medium" | "low"

    # Maintenance schedule
    maintenance_cadence_days: int = 7
    dead_after_n_failures:    int = 2
    prune_after_n_failures:   int = 4

    class Config:
        env_file = ".env"
        env_prefix = "WCD_"          # WCD_LOG_DIR=... etc.
```

---

## Notebook Bootstrap Cell (Colab / SageMaker)

Every notebook begins with this cell so it is self-contained:

```python
# ── Environment setup (run once) ──────────────────────────────
import subprocess, sys, os
from pathlib import Path

IN_COLAB     = 'google.colab' in sys.modules
IN_SAGEMAKER = os.environ.get('SM_TRAINING_ENV') is not None

if IN_COLAB:
    if not Path('webcam-discovery').exists():
        subprocess.run(['git', 'clone', 'https://github.com/YOUR_ORG/webcam-discovery'], check=True)
    os.chdir('webcam-discovery')

subprocess.run([sys.executable, '-m', 'pip', 'install', '-e', '.[notebooks]', '-q'], check=True)

from webcam_discovery.config import Settings
cfg = Settings()
for d in [cfg.log_dir, cfg.snapshot_dir, cfg.candidates_dir]:
    d.mkdir(parents=True, exist_ok=True)

print(f"✓ Ready")
print(f"  logs/       → {cfg.log_dir.resolve()}")
print(f"  snapshots/  → {cfg.snapshot_dir.resolve()}")
print(f"  candidates/ → {cfg.candidates_dir.resolve()}")
```

---

## `.gitignore`

```gitignore
# Generated pipeline outputs at project root
camera.geojson
cameras.md
map.html

# Runtime directories
logs/
snapshots/
candidates/

# Secrets
.env

# Python
__pycache__/
*.pyc
*.egg-info/
dist/
.venv/

# Jupyter
.ipynb_checkpoints/

# Playwright
playwright/.local-browsers/
```

---

## Environment Variables (`.env.example`)

```bash
# Runtime directory paths (defaults are relative to project root)
WCD_LOG_DIR=logs
WCD_SNAPSHOT_DIR=snapshots
WCD_CANDIDATES_DIR=candidates

# Catalog output — camera.geojson, cameras.md, map.html must stay co-located
WCD_CATALOG_OUTPUT_DIR=.

# Pipeline tuning
WCD_MAX_CONCURRENCY=10
WCD_REQUEST_TIMEOUT=5.0
WCD_MIN_LEGITIMACY=medium

# Maintenance
WCD_MAINTENANCE_CADENCE_DAYS=7
WCD_DEAD_AFTER_N_FAILURES=2
WCD_PRUNE_AFTER_N_FAILURES=4
```

---

## Updated File Path Reference

The existing `AGENTS.md`, `CLAUDE.md`, and `MAPS.md` reference bare module
names and relative paths. With this layout, all paths resolve as follows:

| Doc reference | Actual path |
|---|---|
| `directory_crawler.py` | `src/webcam_discovery/agents/directory_crawler.py` |
| `search_agent.py` | `src/webcam_discovery/agents/search_agent.py` |
| `validator.py` | `src/webcam_discovery/agents/validator.py` |
| `catalog.py` | `src/webcam_discovery/agents/catalog.py` |
| `maintenance.py` | `src/webcam_discovery/agents/maintenance.py` |
| `schemas.py` | `src/webcam_discovery/schemas.py` |
| `camera.geojson` | `webcam-discovery/camera.geojson` (project root) |
| `cameras.md` | `webcam-discovery/cameras.md` (project root) |
| `map.html` | `webcam-discovery/map.html` (project root) |
| `maintenance_log.jsonl` | `webcam-discovery/logs/maintenance_log.jsonl` |
| `candidates.jsonl` | `webcam-discovery/candidates/candidates.jsonl` |
| `validated.jsonl` | `webcam-discovery/candidates/validated.jsonl` |

## Added clarification and validation artifacts

```text
logs/query_clarification.json                 # one-time LLM clarification preflight result
logs/query_clarification_response.json        # supplied clarification answer and clarified query
logs/http_hls_probe_results.jsonl             # early HTTP/HLS probe rows, not final camera truth
logs/http_hls_probe_summary.json              # early HTTP/HLS probe counts
logs/validation_results.jsonl                 # final validation status rows after all classification/caps
logs/camera_status_summary.json               # final status counts matching run_summary validation counts
candidates/agentic_candidates_validation_dropped.jsonl  # validation handoff rows dropped by cap
candidates/catalog_cap_dropped.jsonl          # records dropped before catalog by max-streams cap
```

`agentic_candidates_unique.jsonl` contains all unique direct HLS candidates found in the handoff. `agentic_candidates_validation_handoff.jsonl` includes rows allowed to scope/validation review plus any cap-skipped rows with explicit reasons.

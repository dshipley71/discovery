# Agents — Claude Code Context

## What lives here
One file per agent. Each agent is a class with an `async run()` method
and a standalone `main()` function for CLI invocation.

## Execution order (from AGENTS.md)
1. `directory_crawler.py` → produces `list[CameraCandidate]`
2. `search_agent.py`      → produces additional `list[CameraCandidate]`
3. `validator.py`         → produces `list[CameraRecord]`
4. `catalog.py`           → writes `camera.geojson` + `cameras.md` to project root
5. `maintenance.py`       → scheduled HEAD checks + pruning on `camera.geojson`

## Inter-agent contract
- Exchange data as typed Pydantic objects (`CameraCandidate`, `CameraRecord`)
  defined in `webcam_discovery/schemas.py`
- For multi-step CLI runs, serialize via JSONL in `candidates/` (project root)
- Never pass raw dicts between agents

## Output paths
- `catalog.py` writes `camera.geojson` and `cameras.md` to project root (`.`)
- `maintenance.py` reads `camera.geojson` from project root
- All log output goes to `logs/` (created automatically)

## Code requirements (from root CLAUDE.md)
- Complete, runnable scripts — no pseudocode
- `pydantic` models for all cross-boundary data
- `httpx` + `asyncio` for all network I/O
- `loguru` for all logging
- `__main__` block in every script

## Adding a new agent
1. Create `agent_name.py` here — use the standard module header
2. Implement `class AgentName` with `async def run(...)` and `def main()`
3. Register in `pipeline.py` at the correct execution step
4. Add CLI entry point in `pyproject.toml` `[project.scripts]`
5. Update `AGENTS.md` agent roster table

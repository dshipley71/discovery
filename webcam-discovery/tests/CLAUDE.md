# Tests — Claude Code Context

## Scope: unit tests only — no live network calls

All HTTP is mocked with `respx` fixtures defined in `conftest.py`.
Run with: `pytest tests/ -q`

## What belongs here
- `test_schemas.py`              — CameraRecord / CameraCandidate validation edge cases
- `agents/test_validator.py`     — HEAD check logic, legitimacy score assignment
- `agents/test_catalog.py`       — dedup, slug generation, GeoJSON coordinate order
- `skills/test_feed_validation.py`  — content-type checks, media vs HTML rejection
- `skills/test_geo_enrichment.py`   — coord enrichment, missing-coord handling
- `skills/test_deduplication.py`    — fuzzy match thresholds, id collision handling

## What does NOT belong here
- DirectoryAgent end-to-end (live sources → test in notebooks/01_discovery.ipynb)
- SearchAgent end-to-end (live sources → test in notebooks/01_discovery.ipynb)
- MaintenanceAgent scheduling (live HEAD checks → test in notebooks/04_maintenance.ipynb)
- Any test that requires a real URL to pass

## When Claude Code should run these
After generating or modifying any module in `agents/` or `skills/`.
Always run `pytest tests/ -q` to verify the inter-agent contracts are intact.

## Common commands
```bash
pytest tests/ -q                                        # all tests
pytest tests/skills/test_feed_validation.py -v         # single file
pytest tests/ -k "dedup" -v                            # by keyword
pytest tests/ --tb=short                               # short tracebacks
```

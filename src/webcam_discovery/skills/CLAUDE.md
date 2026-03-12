# Skills — Claude Code Context

## What lives here
Skills are reusable, single-responsibility classes with one public `run()` method.
Each file groups skills by functional category.

## File-to-skill mapping
| File | Skills |
|------|--------|
| `traversal.py`    | `DirectoryTraversalSkill`, `FeedExtractionSkill` |
| `validation.py`   | `FeedValidationSkill`, `RobotsPolicySkill`, `FeedTypeClassificationSkill` |
| `search.py`       | `QueryGenerationSkill`, `LocaleNavigationSkill`, `SourceDiscoverySkill` |
| `catalog.py`      | `DeduplicationSkill`, `GeoEnrichmentSkill`, `GeoJSONExportSkill` |
| `maintenance.py`  | `HealthCheckSkill` |
| `map_rendering.py`| `MapRenderingSkill` |

## Interface contract
```python
class SomeSkill:
    def run(self, input: SomeInput) -> SomeOutput:
        ...

    # OR for network I/O:
    async def run(self, input: SomeInput) -> SomeOutput:
        ...
```
All inputs and outputs are Pydantic models.
All skills catch and log exceptions without crashing the pipeline.
Network skills use `httpx.AsyncClient` and `asyncio.gather` for parallelism.

## Code requirements
- Pydantic BaseModel for SkillInput + SkillOutput
- loguru for all logging with URL context
- httpx async for all network operations
- Never raise unhandled exceptions — catch, log, return a degraded result

## Adding a new skill
1. Add the class to the appropriate file (or create a new file for a new category)
2. Define `SkillInput` and `SkillOutput` as Pydantic models
3. Implement `run()` or `async def run()`
4. Export from `skills/__init__.py`
5. Update `SKILLS.md` skill index table

## Full skill specs
See `SKILLS.md` in the project root for complete interface specs,
async patterns, and implementation guidance for every skill.

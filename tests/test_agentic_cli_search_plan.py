import argparse
import asyncio
import json
from pathlib import Path

from webcam_discovery import cli
from webcam_discovery.models.planner import PlannerIntent, PlannerPlan
from webcam_discovery.schemas import CameraCandidate


class FakePlanner:
    async def plan(self, query, context=None):
        return PlannerPlan(
            original_query=query,
            parsed_intent=PlannerIntent(geography=["Pennsylvania"], camera_types=["traffic cameras"]),
            target_locations=["Pennsylvania"],
            camera_types=["traffic cameras"],
            discovery_methods=["web_search"],
            source_preferences=[],
            reasoning_summary="test",
        )


class FakeSearchAgent:
    def __init__(self, *args, **kwargs):
        self.custom_queries_seen = []

    async def stream_queries(self, *, custom_queries=None, raw_query=None, max_results_per_query=None, query_source=None, on_query=None, on_result=None):
        self.custom_queries_seen = list(custom_queries or [])
        for q in custom_queries or []:
            if on_query:
                on_query(q)
            if on_result:
                on_result({"query": q, "url": "https://example.com/page", "title": "t", "snippet": "s"})
        yield CameraCandidate(url="https://example.com/live.m3u8", city="Harrisburg", source_refs=["query:PennDOT traffic cameras live"])


class FakeDirectoryAgent:
    async def stream(self, *args, **kwargs):
        if False:
            yield


class FakeValidator:
    async def run(self, candidates):
        return []


class FakeCatalog:
    async def run(self, **kwargs):
        return None


def test_run_agentic_writes_search_audit_files_and_ignores_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    (tmp_path / "candidates").mkdir()

    fake_search = FakeSearchAgent()

    monkeypatch.setattr(cli, "PlannerAgent", lambda: FakePlanner())
    monkeypatch.setattr(cli, "SearchAgent", lambda: fake_search)
    monkeypatch.setattr(cli, "DirectoryAgent", lambda: FakeDirectoryAgent())
    monkeypatch.setattr(cli, "ValidationAgent", lambda: FakeValidator())
    monkeypatch.setattr(cli, "CatalogAgent", lambda: FakeCatalog())
    monkeypatch.setattr(cli, "create_memory_backend", lambda: None)

    args = argparse.Namespace(
        query="Get me public live traffic cameras from Pennsylvania and ignore SOURCES.md",
        output_dir=Path("."),
        max_candidates=20,
        max_streams=5,
        max_search_queries=25,
        max_search_results_per_query=10,
        ignore_sources_md=True,
        enable_memory=False,
        enable_visual_analysis=False,
        enable_video_summary=False,
        llm_provider=None,
        llm_model=None,
        enable_deep_discovery=False,
        max_links_per_page=25,
        max_js_assets_per_page=20,
        max_deep_depth=3,
        max_deep_pages=100,
        max_network_capture_pages=10,
        network_capture_timeout=8,
    )

    asyncio.run(cli.run_agentic(args))

    assert Path("logs/search_plan.json").exists()
    assert Path("logs/search_queries.jsonl").exists()
    assert Path("logs/search_results.jsonl").exists()
    assert Path("candidates/agentic_candidates.jsonl").exists()

    plan = json.loads(Path("logs/search_plan.json").read_text(encoding="utf-8"))
    assert "Pennsylvania" in plan["original_locations"]
    assert any("PennDOT" in q for q in fake_search.custom_queries_seen)

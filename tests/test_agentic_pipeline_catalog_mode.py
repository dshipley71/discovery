import argparse
import asyncio
from pathlib import Path

from webcam_discovery import cli
from webcam_discovery.models.planner import PlannerIntent, PlannerPlan


class P:
    async def plan(self, query, context=None):
        return PlannerPlan(original_query=query, parsed_intent=PlannerIntent(geography=[], camera_types=[]), target_locations=[], camera_types=[], discovery_methods=["web_search"], source_preferences=[], reasoning_summary="x")


def test_catalog_mode_raises_limits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "PlannerAgent", lambda: P())
    monkeypatch.setattr(cli, "create_memory_backend", lambda: None)
    args = argparse.Namespace(query="Find public HLS cameras", output_dir=Path('.'), max_candidates=20, max_streams=5, max_search_queries=2, max_search_results_per_query=3, catalog_mode=True, ignore_sources_md=True, enable_memory=False, enable_visual_analysis=False, enable_video_summary=False, llm_provider=None, llm_model=None, enable_deep_discovery=False, max_links_per_page=25, max_js_assets_per_page=20, max_deep_depth=3, max_deep_pages=100, max_network_capture_pages=10, network_capture_timeout=8)
    asyncio.run(cli.run_agentic(args))
    assert args.max_candidates >= 500
    assert args.max_streams >= 250
    assert Path("logs/target_resolution.json").exists()

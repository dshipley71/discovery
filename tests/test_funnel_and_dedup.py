import argparse
import asyncio
import json
from pathlib import Path

from webcam_discovery import cli
from webcam_discovery.models.planner import PlannerIntent, PlannerPlan
from webcam_discovery.schemas import CameraRecord
from webcam_discovery.skills.catalog import DeduplicationInput, DeduplicationSkill

class _Planner:
    async def plan(self, query, context=None):
        return PlannerPlan(original_query=query, parsed_intent=PlannerIntent(geography=[], camera_types=[]), target_locations=[], camera_types=[], discovery_methods=["web_search"], source_preferences=[], reasoning_summary="x")

class _Search:
    async def stream_queries(self, **kwargs):
        if False:
            yield None

class _Validator:
    async def run(self, candidates):
        return []

def test_unknown_location_dedup_does_not_collapse_distinct_streams():
    skill = DeduplicationSkill()
    existing = []
    for i in range(11):
        r = CameraRecord(id=f"cam-{i}", label="Unknown Cam", city="Unknown", country="Unknown", continent="Unknown", url=f"https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8?camera={i}")
        out = skill.run(DeduplicationInput(candidate_record=r, existing_catalog=existing))
        if not out.is_duplicate:
            existing.append(r)
    assert len(existing) == 11

def test_discovery_funnel_written_for_feed_discovery(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "PlannerAgent", lambda: _Planner())
    monkeypatch.setattr(cli, "SearchAgent", lambda: _Search())
    monkeypatch.setattr(cli, "create_memory_backend", lambda: None)
    monkeypatch.setattr(cli, "ValidationAgent", lambda: _Validator())
    args = argparse.Namespace(query="Find public HLS cameras near Paris", output_dir=Path('.'), max_candidates=5, max_streams=5, max_search_queries=2, max_search_results_per_query=2, catalog_mode=False, ignore_sources_md=True, enable_memory=False, enable_visual_analysis=False, enable_video_summary=False, llm_provider=None, llm_model=None, enable_deep_discovery=False, max_links_per_page=25, max_js_assets_per_page=20, max_deep_depth=3, max_deep_pages=100, max_network_capture_pages=10, network_capture_timeout=8, enable_feed_discovery=True, max_feed_endpoints=2, max_feed_records=2, max_stream_candidates=10)
    asyncio.run(cli.run_agentic(args))
    funnel = json.loads(Path("logs/discovery_funnel.json").read_text())
    assert "feed_discovery" in funnel and "candidate_generation" in funnel and "validation" in funnel and "catalog" in funnel

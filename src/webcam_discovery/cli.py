from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from loguru import logger

from webcam_discovery.agents.catalog import CatalogAgent
from webcam_discovery.agents.directory_crawler import DirectoryAgent
from webcam_discovery.agents.planner_agent import PlannerAgent, PlannerContext
from webcam_discovery.agents.search_agent import SearchAgent
from webcam_discovery.agents.search_result_triage_agent import SearchResultTriageAgent
from webcam_discovery.agents.target_resolution_agent import TargetResolutionAgent
from webcam_discovery.agents.deep_discovery_agent import DeepDiscoveryAgent
from webcam_discovery.agents.validator import ValidationAgent
from webcam_discovery.agents.video_summarization_agent import VideoSummarizationAgent
from webcam_discovery.config import settings
from webcam_discovery.memory.factory import create_memory_backend
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.models.deep_discovery import PageCandidate, StreamCandidate
from webcam_discovery.skills.candidate_relevance import CandidateRelevanceFilter
from webcam_discovery.skills.location_expansion import LocationExpansionSkill
from webcam_discovery.skills.visual_stream_analysis import VisualStreamAnalysis


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def run_agentic(args: argparse.Namespace) -> None:
    settings.ensure_dirs()
    from webcam_discovery.pipeline import configure_logging

    configure_logging()

    if args.llm_provider:
        settings.planner_provider = args.llm_provider
    if args.llm_model:
        settings.planner_model = args.llm_model
    if args.enable_memory:
        settings.memory_enabled = True
    if args.enable_visual_analysis:
        settings.visual_stream_analysis_enabled = True
    if args.enable_video_summary:
        settings.video_summary_enabled = True

    if getattr(args, "catalog_mode", False):
        args.max_search_queries = max(args.max_search_queries, 50)
        args.max_search_results_per_query = max(args.max_search_results_per_query, 20)
        args.max_deep_pages = max(getattr(args, "max_deep_pages", 100), 300)
        args.max_candidates = max(args.max_candidates, 500)
        args.max_streams = max(args.max_streams, 250)

    memory = create_memory_backend()
    memory_hints = memory.search(args.query, limit=5) if (memory and settings.memory_search_before_planning) else []

    planner = PlannerAgent()
    plan = await planner.plan(args.query, context=PlannerContext(memory_hints=memory_hints))
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    _append_jsonl(settings.log_dir / "planner_runs.jsonl", {
        "timestamp": datetime.utcnow().isoformat(),
        "run_id": run_id,
        "query": args.query,
        "plan": plan.model_dump(),
        "memory_hints": memory_hints,
    })

    target_resolution = TargetResolutionAgent().resolve(args.query, plan)
    (settings.log_dir / "target_resolution.json").write_text(json.dumps(target_resolution.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    if target_resolution.insufficient_target:
        logger.warning(target_resolution.message)
        return

    target_locations = [t.normalized_name or t.raw_text for t in target_resolution.targets]
    camera_types = plan.camera_types or plan.parsed_intent.camera_types
    location_search_plan = LocationExpansionSkill().expand(
        target_locations=target_locations,
        camera_types=camera_types,
        raw_query=args.query,
        max_queries=args.max_search_queries,
    )
    (settings.log_dir / "search_plan.json").write_text(
        json.dumps(location_search_plan.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    dir_agent = DirectoryAgent()
    search_agent = SearchAgent()
    candidates: list[CameraCandidate] = []
    page_candidates: list[PageCandidate] = []
    seen: set[str] = set()
    rejection_reasons: dict[str, int] = {}

    async def _collect(gen):
        async for c in gen:
            if c.url not in seen:
                seen.add(c.url)
                candidates.append(c)
                if args.max_candidates and len(candidates) >= args.max_candidates:
                    return

    tasks = []
    ignore_sources = bool(args.ignore_sources_md)

    if not ignore_sources and ("directory_search" in plan.discovery_methods or "known_sources" in plan.discovery_methods):
        tasks.append(_collect(dir_agent.stream(tier=1, hls_only=True)))
    if "web_search" in plan.discovery_methods or not tasks:
        tasks.append(
            _collect(
                search_agent.stream_queries(
                    custom_queries=location_search_plan.search_queries,
                    raw_query=args.query,
                    max_results_per_query=args.max_search_results_per_query,
                    query_source="planner_location_search",
                    on_query=lambda q: _append_jsonl(
                        settings.log_dir / "search_queries.jsonl",
                        {"run_id": run_id, "query": q, "source": "planner_location_search"},
                    ),
                    on_result=lambda r: (
                        _append_jsonl(settings.log_dir / "search_results.jsonl", {"run_id": run_id, **r}),
                        page_candidates.append(PageCandidate(run_id=run_id, user_query=args.query, source_query=r.get("query"), url=r.get("url", ""), title=r.get("title"), snippet=r.get("snippet"), target_locations=target_locations, camera_types=camera_types)),
                        _append_jsonl(settings.candidates_dir / "search_page_candidates.jsonl", PageCandidate(run_id=run_id, user_query=args.query, source_query=r.get("query"), url=r.get("url", ""), title=r.get("title"), snippet=r.get("snippet"), target_locations=target_locations, camera_types=camera_types).model_dump()),
                    ),
                )
            )
        )
    await asyncio.gather(*tasks)

    for c in candidates:
        _append_jsonl(
            settings.candidates_dir / "agentic_candidates.jsonl",
            {
                "run_id": run_id,
                "source_query": next((ref[6:] for ref in c.source_refs if ref.startswith("query:")), ""),
                "candidate_url": c.url,
                "candidate_type": "hls_or_page",
                "discovered_by": "SearchAgent",
            },
        )

    stream_candidates: list[StreamCandidate] = [
        StreamCandidate(run_id=run_id, user_query=args.query, source_query=next((ref[6:] for ref in c.source_refs if ref.startswith("query:")), None), candidate_url=c.url, source_page=c.source_refs[0] if c.source_refs else None, discovery_strategy="search_direct", target_locations=target_locations, camera_types=camera_types, page_relevance_score=0.6, camera_likelihood_score=0.6)
        for c in candidates
    ]
    if getattr(args, "enable_deep_discovery", True) and page_candidates:
        triaged = SearchResultTriageAgent().triage(page_candidates, target_locations, location_search_plan.agencies, camera_types)
        deep_agent = DeepDiscoveryAgent(settings.log_dir, settings.candidates_dir, getattr(args, "max_links_per_page", 25), getattr(args, "max_js_assets_per_page", 20))
        deep_streams = await deep_agent.discover(triaged, args.query, target_locations, location_search_plan.agencies, camera_types, getattr(args, "max_deep_depth", 3), getattr(args, "max_deep_pages", 100), getattr(args, "max_network_capture_pages", 10), getattr(args, "network_capture_timeout", 8))
        stream_candidates.extend(deep_streams)

    dedup = {s.candidate_url: s for s in stream_candidates}
    decisions = CandidateRelevanceFilter().filter(list(dedup.values()), target_locations, location_search_plan.agencies, camera_types)
    validation_candidates = []
    for sc, decision in decisions:
        _append_jsonl(settings.log_dir / "candidate_relevance.jsonl", decision.model_dump())
        if decision.accepted:
            validation_candidates.append(CameraCandidate(url=sc.candidate_url, source_refs=[f"query:{sc.source_query}" if sc.source_query else ""]))
        else:
            rejection_reasons[decision.reason] = rejection_reasons.get(decision.reason, 0) + 1
            _append_jsonl(settings.candidates_dir / "rejected_candidates.jsonl", {"candidate_url": sc.candidate_url, "reason": decision.reason, "source_page": sc.source_page})

    validator = ValidationAgent()
    records = await validator.run(candidates=validation_candidates)
    if args.max_streams:
        records = records[: args.max_streams]

    if settings.visual_stream_analysis_enabled:
        analyzer = VisualStreamAnalysis()
        for record in records:
            result = await analyzer.analyze(record.url)
            record.status = result.stream_status
            record.stream_substatus = result.stream_substatus
            record.stream_confidence = result.stream_confidence
            record.stream_reasons = result.stream_reasons
            record.visual_metrics = result.visual_metrics
            _append_jsonl(settings.log_dir / "visual_stream_analysis.jsonl", result.model_dump())
            _append_jsonl(settings.log_dir / "temporal_status.jsonl", result.model_dump())

    mapped_records = [r for r in records if r.latitude is not None and r.longitude is not None]
    unknown_location = [r for r in records if r.latitude is None or r.longitude is None]
    for r in unknown_location:
        _append_jsonl(settings.candidates_dir / "needs_review_location_unknown.jsonl", r.model_dump())

    run_summary = {
        "query": args.query,
        "targets": target_locations,
        "candidate_counts": {
            "raw": len(stream_candidates),
            "relevance_passed": len(validation_candidates),
            "robots_passed": len(validation_candidates),
            "http_live": len(records),
            "ffprobe_live": len(records),
            "playlist_live": len([r for r in records if (getattr(r, "notes", "") or "").find("playlist:live_playlist") >= 0]),
            "mapped": len(mapped_records),
            "needs_review_location_unknown": len(unknown_location),
            "rejected": sum(rejection_reasons.values()),
        },
        "rejection_reasons": rejection_reasons,
        "output_files": {"geojson": str(Path(args.output_dir) / "camera.geojson")},
    }
    (settings.log_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    if len(mapped_records) == 0:
        logger.warning("No catalogable cameras were mapped for the requested target(s).")

    if settings.video_summary_enabled:
        summarizer = VideoSummarizationAgent()
        for record in records:
            summary = await summarizer.summarize(record)
            _append_jsonl(settings.log_dir / "video_summaries.jsonl", summary.__dict__)

    await CatalogAgent().run(records=records, output_dir=args.output_dir, snapshot_dir=settings.snapshot_dir)

    if memory and settings.memory_write_run_summaries:
        status_counts = {"live": 0, "dead": 0, "unknown": 0}
        for r in records:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1
        md = "\n".join([
            f"# Run Summary — {args.query}",
            "",
            f"- Query: {args.query}",
            f"- Candidates discovered: {len(candidates)}",
            f"- Valid HLS streams: {len(records)}",
            f"- Status counts: {status_counts}",
            f"- Planner methods: {', '.join(plan.discovery_methods)}",
            f"- Visual analysis: {settings.visual_stream_analysis_enabled}",
            f"- Video summary: {settings.video_summary_enabled}",
        ])
        run_note = memory.write_run_summary(args.query[:80], md)
        _append_jsonl(settings.log_dir / "memory_updates.jsonl", {
            "timestamp": datetime.utcnow().isoformat(),
            "memory_file": str(run_note),
            "summary_title": args.query,
        })

    logger.info("run-agentic complete — {} records", len(records))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webcam-discovery", description="Webcam discovery CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run-agentic", help="Run real LLM-planned agentic discovery")
    run.add_argument("query", type=str)
    run.add_argument("--output-dir", type=Path, default=Path("."))
    run.add_argument("--max-candidates", type=int, default=30)
    run.add_argument("--max-streams", type=int, default=10)
    run.add_argument("--max-search-queries", type=int, default=25)
    run.add_argument("--max-search-results-per-query", type=int, default=10)
    run.add_argument("--catalog-mode", action="store_true")
    run.add_argument("--ignore-sources-md", action="store_true")
    run.add_argument("--enable-deep-discovery", action="store_true", default=True)
    run.add_argument("--disable-deep-discovery", action="store_false", dest="enable_deep_discovery")
    run.add_argument("--max-deep-depth", type=int, default=3)
    run.add_argument("--max-deep-pages", type=int, default=100)
    run.add_argument("--max-links-per-page", type=int, default=25)
    run.add_argument("--max-js-assets-per-page", type=int, default=20)
    run.add_argument("--max-network-capture-pages", type=int, default=10)
    run.add_argument("--network-capture-timeout", type=int, default=8)
    run.add_argument("--max-total-deep-dive-seconds", type=int, default=180)
    run.add_argument("--enable-memory", action="store_true")
    run.add_argument("--enable-visual-analysis", action="store_true")
    run.add_argument("--enable-video-summary", action="store_true")
    run.add_argument("--llm-provider", type=str, choices=["ollama", "openai-compatible"])
    run.add_argument("--llm-model", type=str)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run-agentic":
        asyncio.run(run_agentic(args))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from webcam_discovery.agents.catalog import CatalogAgent
from webcam_discovery.agents.directory_crawler import DirectoryAgent
from webcam_discovery.agents.planner_agent import PlannerAgent, PlannerContext
from webcam_discovery.agents.search_agent import SearchAgent
from webcam_discovery.agents.validator import ValidationAgent
from webcam_discovery.agents.video_summarization_agent import VideoSummarizationAgent
from webcam_discovery.config import settings
from webcam_discovery.memory.factory import create_memory_backend
from webcam_discovery.schemas import CameraCandidate
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

    memory = create_memory_backend()
    memory_hints = memory.search(args.query, limit=5) if (memory and settings.memory_search_before_planning) else []

    planner = PlannerAgent()
    plan = await planner.plan(args.query, context=PlannerContext(memory_hints=memory_hints))
    _append_jsonl(settings.log_dir / "planner_runs.jsonl", {
        "timestamp": datetime.utcnow().isoformat(),
        "query": args.query,
        "plan": plan.model_dump(),
        "memory_hints": memory_hints,
    })

    dir_agent = DirectoryAgent()
    search_agent = SearchAgent()
    candidates: list[CameraCandidate] = []
    seen: set[str] = set()

    async def _collect(gen):
        async for c in gen:
            if c.url not in seen:
                seen.add(c.url)
                candidates.append(c)
                if args.max_candidates and len(candidates) >= args.max_candidates:
                    return

    tasks = []
    if "directory_search" in plan.discovery_methods or "known_sources" in plan.discovery_methods:
        tasks.append(_collect(dir_agent.stream(tier=1, hls_only=True)))
    if "web_search" in plan.discovery_methods or not tasks:
        tasks.append(_collect(search_agent.stream(tier=1)))
    await asyncio.gather(*tasks)

    validator = ValidationAgent()
    records = await validator.run(candidates=candidates)
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

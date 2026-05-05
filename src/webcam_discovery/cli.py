from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

from loguru import logger

from webcam_discovery.agents.catalog import CatalogAgent
from webcam_discovery.agents.directory_crawler import DirectoryAgent
from webcam_discovery.agents.planner_agent import PlannerAgent, PlannerContext
from webcam_discovery.agents.search_agent import SearchAgent
from webcam_discovery.agents.scope_enforcement_agent import ScopeEnforcementAgent
from webcam_discovery.agents.scope_enforcement_agent import ScopeInferenceParseError
from webcam_discovery.agents.search_result_triage_agent import SearchResultTriageAgent
from webcam_discovery.agents.feed_discovery_agent import FeedDiscoveryAgent
from webcam_discovery.agents.target_resolution_agent import TargetResolutionAgent
from webcam_discovery.agents.deep_discovery_agent import DeepDiscoveryAgent
from webcam_discovery.agents.validator import ValidationAgent
from webcam_discovery.agents.video_summarization_agent import VideoSummarizationAgent
from webcam_discovery.config import settings
from webcam_discovery.memory.factory import create_memory_backend
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.models.deep_discovery import PageCandidate, StreamCandidate
from webcam_discovery.skills.candidate_priority import CandidatePriorityScorer
from webcam_discovery.skills.url_metadata_extraction import URLMetadataExtractor
from webcam_discovery.skills.location_expansion import LocationExpansionSkill
from webcam_discovery.skills.visual_stream_analysis import VisualStreamAnalysis
from webcam_discovery.models.stream_analysis import StreamAnalysisResult
from webcam_discovery.llm.base import LLMRequestError

def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def run_agentic(args: argparse.Namespace) -> None:
    from webcam_discovery.pipeline import configure_logging

    output_dir = Path(args.output_dir)
    settings.catalog_output_dir = output_dir
    settings.log_dir = output_dir / "logs"
    settings.candidates_dir = output_dir / "candidates"
    settings.snapshot_dir = output_dir / "snapshots"

    validation_enabled = not getattr(args, "disable_validation", False)
    if getattr(args, "disable_ffprobe_validation", False):
        settings.use_ffprobe_validation = False
    if not validation_enabled:
        settings.use_ffprobe_validation = False
        settings.visual_stream_analysis_enabled = False
        settings.video_summary_enabled = False

    settings.ensure_dirs()
    configure_logging()

    if getattr(args, "disable_ffprobe_validation", False) and validation_enabled:
        logger.warning("ffprobe validation disabled by CLI flag; HTTP/HLS validation will still run.")
    if not validation_enabled:
        logger.warning("Validation disabled by CLI flag. Unvalidated candidates will be written for review only and will not be cataloged as cameras.")

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
    if not validation_enabled:
        settings.visual_stream_analysis_enabled = False
        settings.video_summary_enabled = False

    if getattr(args, "catalog_mode", False):
        args.max_search_queries = max(args.max_search_queries, 50)
        args.max_search_results_per_query = max(args.max_search_results_per_query, 20)
        args.max_deep_pages = max(getattr(args, "max_deep_pages", 100), 300)
        args.max_candidates = max(args.max_candidates, 500)
        args.max_streams = max(args.max_streams, 250)

    memory = create_memory_backend()
    memory_hints = memory.search(args.query, limit=5) if (memory and settings.memory_search_before_planning) else []

    planner = PlannerAgent()
    try:
        plan = await planner.plan(args.query, context=PlannerContext(memory_hints=memory_hints))
    except LLMRequestError as exc:
        logger.error("Planner LLM request timed out before discovery started.")
        logger.error("No discovery, validation, catalog, or map generation was performed.")
        payload = {"status": "failed_before_discovery", "failed_stage": exc.stage, "provider": exc.provider, "model": exc.model, "attempts": exc.attempts, "error_type": exc.error_type, "message": exc.message, "query": args.query}
        (settings.log_dir / "planner_error.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "failed_before_discovery", "failed_stage": exc.stage, "query": args.query, "discovery_started": False, "search_started": False, "feed_discovery_started": False, "deep_discovery_started": False, "validation_started": False, "catalog_started": False, "mapped": 0, "error": {"type": exc.error_type, "message": exc.message}}, indent=2), encoding="utf-8")
        raise SystemExit(2) from exc
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    _append_jsonl(settings.log_dir / "planner_runs.jsonl", {
        "timestamp": datetime.utcnow().isoformat(),
        "run_id": run_id,
        "query": args.query,
        "plan": plan.model_dump(),
        "memory_hints": memory_hints,
    })

    scope_agent = ScopeEnforcementAgent()
    try:
        scope = await scope_agent.infer_scope(args.query, plan)
    except LLMRequestError as exc:
        payload = {"status": "failed_before_discovery", "failed_stage": exc.stage, "provider": exc.provider, "model": exc.model, "attempts": exc.attempts, "error_type": exc.error_type, "message": exc.message, "query": args.query}
        (settings.log_dir / "scope_error.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "failed_before_discovery", "failed_stage": exc.stage, "query": args.query, "discovery_started": False, "search_started": False, "feed_discovery_started": False, "deep_discovery_started": False, "validation_started": False, "catalog_started": False, "mapped": 0, "error": {"type": exc.error_type, "message": exc.message}}, indent=2), encoding="utf-8")
        raise SystemExit(2) from exc
    except ScopeInferenceParseError as exc:
        logger.error("Scope inference failed before discovery: the LLM returned a response that could not be parsed into the required scope schema.")
        logger.error("No search, feed discovery, deep discovery, validation, cataloging, or map generation was performed.")
        error_payload = {
            "stage": "scope_inference",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "user_query": args.query,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            "raw_llm_response": exc.raw_llm_response,
            "normalized_payload": exc.normalized_payload,
            "discovery_started": False,
        }
        (settings.log_dir / "scope_inference_error.json").write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "failed_before_discovery", "failed_stage": "scope_inference", "query": args.query, "discovery_started": False, "search_started": False, "feed_discovery_started": False, "deep_discovery_started": False, "validation_started": False, "catalog_started": False, "mapped": 0, "error": {"type": type(exc).__name__, "message": str(exc)}}, indent=2), encoding="utf-8")
        raise SystemExit(2) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error("Scope inference failed before discovery: the LLM returned a response that could not be parsed into the required scope schema.")
        logger.error("No search, feed discovery, deep discovery, validation, cataloging, or map generation was performed.")
        error_payload = {
            "stage": "scope_inference",
            "error_type": type(exc).__name__,
            "error_message": str(exc),
            "user_query": args.query,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            "raw_llm_response": None,
            "normalized_payload": None,
            "discovery_started": False,
        }
        (settings.log_dir / "scope_inference_error.json").write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "failed_before_discovery", "failed_stage": "scope_inference", "query": args.query, "discovery_started": False, "search_started": False, "feed_discovery_started": False, "deep_discovery_started": False, "validation_started": False, "catalog_started": False, "mapped": 0, "error": {"type": type(exc).__name__, "message": str(exc)}}, indent=2), encoding="utf-8")
        raise SystemExit(2) from exc
    llm_metadata = {"provider": settings.planner_provider, "model": settings.planner_model}
    scope_payload = {**scope.model_dump(), **llm_metadata}
    (settings.log_dir / "scope_inference.json").write_text(json.dumps(scope_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (settings.log_dir / "scope_summary.json").write_text(json.dumps({
        "query": args.query,
        "plan_summary": getattr(plan, "summary", None),
        "provider": settings.planner_provider,
        "model": settings.planner_model,
        "has_sufficient_scope": scope.has_sufficient_scope,
        "scope_label": scope.scope_label,
        "scope_type": scope.scope_type,
        "confidence": scope.confidence,
        "normalized_scope_payload": scope.model_dump(),
        "raw_llm_response": scope.raw_llm_response,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not scope.has_sufficient_scope:
        message = scope.user_message or scope.insufficient_scope_reason or "I need a specific place, landmark, coordinates, IP address, hostname, agency, or public website before discovery."
        logger.warning("Insufficient scope: {}", message)
        for rel in ["search_result_scope_decisions.jsonl", "stream_candidate_scope_decisions.jsonl"]:
            (settings.log_dir / rel).touch()
        scope_summary = {"scope": {**scope.model_dump(), **llm_metadata, "search_results_accepted": 0, "search_results_rejected": 0, "search_results_review": 0, "stream_candidates_accepted": 0, "stream_candidates_rejected": 0, "stream_candidates_review": 0}}
        (settings.log_dir / "scope_summary.json").write_text(json.dumps(scope_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        run_summary = {
            "query": args.query,
            "targets": [],
            "scope": scope_summary["scope"],
            "validation": {
                "validation_enabled": validation_enabled,
                "validation_skipped": not validation_enabled,
                "ffprobe_validation_enabled": bool(settings.use_ffprobe_validation and validation_enabled),
            },
            "candidate_counts": {
                "raw": 0,
                "relevance_passed": 0,
                "validated_records": 0,
                "mapped": 0,
                "unvalidated_candidates": 0,
            },
            "stopped_before_discovery": True,
            "reason": "insufficient_scope",
        }
        (settings.log_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
        return
    logger.info("Scope inferred: {} [{}], confidence={}", scope.scope_label or "(unspecified)", scope.scope_type or "unknown", scope.confidence)
    for rel in ["search_result_scope_decisions.jsonl", "stream_candidate_scope_decisions.jsonl"]:
        (settings.log_dir / rel).touch()

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
    started_at = datetime.utcnow().isoformat()
    funnel = {
        "run": {"run_id": run_id, "query": args.query, "started_at": started_at, "completed_at": None, "exit_code": 0},
        "target_resolution": {"targets": target_locations, "insufficient_target": target_resolution.insufficient_target},
        "search": {"queries": len(location_search_plan.search_queries), "results": 0, "page_candidates": 0},
        "feed_discovery": {"enabled": bool(getattr(args, "enable_feed_discovery", True)), "endpoints_discovered": 0, "endpoints_parsed": 0, "records_extracted": 0, "candidates_created": 0, "candidates_with_coordinates": 0, "candidates_without_coordinates": 0},
        "candidate_generation": {"raw_camera_candidates": 0, "direct_hls_candidates": 0, "deep_discovery_candidates": 0, "stream_candidates_before_priority": 0, "stream_candidates_sent_to_validation": 0},
        "candidate_priority": {"high": 0, "medium": 0, "low": 0},
        "url_metadata_extraction": {"processed": 0, "with_location_hint": 0},
        "validation": {"received": 0, "robots_passed": 0, "http_live": 0, "http_dead": 0, "http_unknown": 0, "ffprobe_live": 0, "ffprobe_dead": 0, "ffprobe_unknown": 0, "validated_records": 0, "records_with_coordinates": 0, "records_without_coordinates": 0},
        "catalog": {"records_received": 0, "unique_records": 0, "dedup_dropped": 0, "geojson_features_written": 0, "geojson_features_skipped": 0, "needs_review_location_unknown": 0},
        "rejections": {"total": 0, "by_reason": {}},
        "source_performance": {"by_domain": {}},
    }

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
    funnel["search"]["results"] = len(page_candidates)
    gated_pages: list[PageCandidate] = []
    page_counts = {"accept": 0, "reject": 0, "review": 0}
    for page in page_candidates:
        decision = await scope_agent.evaluate_search_result(page, scope)
        page_counts[decision.decision] = page_counts.get(decision.decision, 0) + 1
        _append_jsonl(settings.log_dir / "search_result_scope_decisions.jsonl", {
            "run_id": run_id,
            "query": page.source_query,
            "result_url": page.url,
            "result_title": page.title,
            "result_snippet": page.snippet,
            "decision": decision.decision,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "risk_flags": decision.risk_flags,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            "raw_llm_response": decision.raw_llm_response,
        })
        if decision.decision == "accept":
            gated_pages.append(page)
    page_candidates = gated_pages
    logger.info("Search result scope gate: accepted={} rejected={} review={}", page_counts.get("accept",0), page_counts.get("reject",0), page_counts.get("review",0))
    funnel["search"]["page_candidates"] = len(page_candidates)

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

    if getattr(args, "enable_feed_discovery", True) and page_candidates:
        feed_agent = FeedDiscoveryAgent()
        feed_result = await feed_agent.discover([p.url for p in page_candidates], max_feed_endpoints=getattr(args, "max_feed_endpoints", 100), max_feed_records=getattr(args, "max_feed_records", 3000))
        funnel["feed_discovery"]["endpoints_discovered"] = feed_result.endpoints_discovered
        funnel["feed_discovery"]["endpoints_parsed"] = feed_result.endpoints_parsed
        funnel["feed_discovery"]["records_extracted"] = feed_result.records_extracted
        for c in (feed_result.candidates or []):
            if c.url not in seen:
                seen.add(c.url)
                candidates.append(c)

    candidate_by_url: dict[str, CameraCandidate] = {}
    stream_candidates: list[StreamCandidate] = []
    for c in candidates:
        candidate_by_url[c.url.strip().lower()] = c
        sq = next((ref[6:] for ref in c.source_refs if ref.startswith("query:")), None)
        src_page = next((ref for ref in c.source_refs if ref.startswith(("http://", "https://"))), None)
        root = src_page.split("/")[0] if src_page else None
        stream_candidates.append(StreamCandidate(run_id=run_id, user_query=args.query, source_query=sq, candidate_url=c.url, source_page=src_page, root_url=src_page, discovery_strategy="search_direct", target_locations=target_locations, camera_types=camera_types, page_relevance_score=0.6, camera_likelihood_score=0.6))
    if getattr(args, "enable_deep_discovery", True) and page_candidates:
        triaged = SearchResultTriageAgent().triage(page_candidates, target_locations, location_search_plan.agencies, camera_types)
        deep_agent = DeepDiscoveryAgent(settings.log_dir, settings.candidates_dir, getattr(args, "max_links_per_page", 25), getattr(args, "max_js_assets_per_page", 20))
        deep_streams = await deep_agent.discover(triaged, args.query, target_locations, location_search_plan.agencies, camera_types, getattr(args, "max_deep_depth", 3), getattr(args, "max_deep_pages", 100), getattr(args, "max_network_capture_pages", 10), getattr(args, "network_capture_timeout", 8))
        stream_candidates.extend(deep_streams)

    stream_counts = {"accept": 0, "reject": 0, "review": 0}
    scoped_stream_candidates: list[StreamCandidate] = []
    for sc in stream_candidates:
        decision = await scope_agent.evaluate_stream_candidate(sc, scope)
        stream_counts[decision.decision] = stream_counts.get(decision.decision, 0) + 1
        _append_jsonl(settings.log_dir / "stream_candidate_scope_decisions.jsonl", {
            "run_id": run_id,
            "candidate_url": sc.candidate_url,
            "source_page": sc.source_page,
            "lineage": sc.parent_pages,
            "decision": decision.decision,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "risk_flags": decision.risk_flags,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            "raw_llm_response": decision.raw_llm_response,
        })
        if decision.decision in {"accept", "review"}:
            scoped_stream_candidates.append(sc)
    stream_candidates = scoped_stream_candidates
    logger.info("Stream candidate scope gate: accepted={} rejected={} review={}", stream_counts.get("accept",0), stream_counts.get("reject",0), stream_counts.get("review",0))

    funnel["candidate_generation"]["raw_camera_candidates"] = len(candidates)
    funnel["candidate_generation"]["stream_candidates_before_priority"] = len(stream_candidates)
    funnel["candidate_generation"]["direct_hls_candidates"] = len(stream_candidates)
    max_stream_candidates = getattr(args, "max_stream_candidates", 2500)
    if len(stream_candidates) > max_stream_candidates:
        stream_candidates = stream_candidates[:max_stream_candidates]
    dedup = {s.candidate_url: s for s in stream_candidates}
    decisions = CandidatePriorityScorer().score(list(dedup.values()), target_locations, location_search_plan.agencies, camera_types)
    validation_candidates = []
    extractor = URLMetadataExtractor()
    for sc, decision in decisions:
        _append_jsonl(settings.log_dir / "candidate_priority.jsonl", decision.model_dump())
        if decision.sent_to_validation:
            original = candidate_by_url.get(sc.candidate_url.strip().lower())
            base = original if original else CameraCandidate(url=sc.candidate_url, source_refs=[x for x in [f"query:{sc.source_query}" if sc.source_query else None, sc.source_page, sc.root_url] if x], source_page=sc.source_page, raw_metadata={"target_locations": sc.target_locations, "camera_types": sc.camera_types, "discovery_strategy": sc.discovery_strategy})
            hints = extractor.extract(base.url, context={"label": base.label, "source_page": base.source_page, "source_query": base.source_query or sc.source_query, "target_locations": target_locations})
            base.url_metadata_hints = hints
            base.location_text_candidates = hints.get("location_text_candidates", [])
            base.source_query = base.source_query or sc.source_query
            base.target_locations = target_locations
            base.source_domain = urlparse(base.url).netloc or None
            funnel["url_metadata_extraction"]["processed"] += 1
            if hints.get("has_location_hint"):
                funnel["url_metadata_extraction"]["with_location_hint"] += 1
            validation_candidates.append(base)
            _append_jsonl(settings.candidates_dir / ("prioritized_candidates.jsonl" if decision.priority in {"high", "medium"} else "deprioritized_candidates.jsonl"), {**decision.model_dump(), "sent_to_validation": True})
        else:
            rejection_reasons[decision.priority_reason] = rejection_reasons.get(decision.priority_reason, 0) + 1
            _append_jsonl(settings.candidates_dir / "rejected_candidates.jsonl", {"candidate_url": sc.candidate_url, "reason": decision.priority_reason, "stage": "malformed_url" if decision.priority_reason == "malformed_url" else "policy"})
        funnel["candidate_priority"][decision.priority] = funnel["candidate_priority"].get(decision.priority, 0) + 1

    funnel["candidate_generation"]["stream_candidates_sent_to_validation"] = len(validation_candidates)
    funnel["validation"]["received"] = len(validation_candidates)
    records = []
    validation_skipped = not validation_enabled
    if validation_enabled:
        validator = ValidationAgent()
        records = await validator.run(candidates=validation_candidates)
        if args.max_streams:
            records = records[: args.max_streams]
    else:
        unvalidated_path = settings.candidates_dir / "unvalidated_stream_candidates.jsonl"
        unvalidated_path.write_text(
            "".join(c.model_dump_json() + "\n" for c in validation_candidates),
            encoding="utf-8",
        )
        validation_skipped_payload = {
            "validation_skipped": True,
            "reason": "disabled_by_cli_flag",
            "unvalidated_candidate_count": len(validation_candidates),
            "warning": "Validation was disabled. Candidates are not confirmed live, reachable, public, or mappable.",
        }
        (settings.log_dir / "validation_skipped.json").write_text(json.dumps(validation_skipped_payload, indent=2), encoding="utf-8")
        logger.warning(
            "Validation skipped by CLI flag; wrote {} unvalidated candidate(s) for review only. Zero mapped cameras will be produced.",
            len(validation_candidates),
        )

    if validation_enabled and settings.visual_stream_analysis_enabled:
        analyzer = VisualStreamAnalysis()
        for record in records:
            try:
                result = await analyzer.analyze(record.url)
            except Exception as exc:
                logger.warning("Visual analysis failed for {}: {}", record.url, exc)
                result = StreamAnalysisResult(
                    url=record.url,
                    stream_status="unknown",
                    stream_substatus="decode_failed",
                    stream_confidence=0.2,
                    stream_reasons=[f"Visual analysis failed: {type(exc).__name__}"],
                    visual_metrics={
                        "frames_decoded": 0,
                        "visual_error": type(exc).__name__,
                    },
                )

            record.status = result.stream_status
            record.stream_substatus = result.stream_substatus
            record.stream_confidence = result.stream_confidence
            record.stream_reasons = result.stream_reasons
            record.visual_metrics = result.visual_metrics
            _append_jsonl(settings.log_dir / "visual_stream_analysis.jsonl", result.model_dump())
            _append_jsonl(settings.log_dir / "temporal_status.jsonl", result.model_dump())

    mapped_records = [r for r in records if r.latitude is not None and r.longitude is not None]
    unknown_location = [r for r in records if r.latitude is None or r.longitude is None]
    funnel["validation"]["records_with_coordinates"] = len(mapped_records)
    funnel["validation"]["records_without_coordinates"] = len(unknown_location)
    funnel["validation"]["validated_records"] = len(records)
    for r in unknown_location:
        _append_jsonl(settings.candidates_dir / "needs_review_location_unknown.jsonl", r.model_dump())

    run_summary = {
        "query": args.query,
        "targets": target_locations,
        "validation": {
            "validation_enabled": validation_enabled,
            "validation_skipped": validation_skipped,
            "ffprobe_validation_enabled": bool(settings.use_ffprobe_validation and validation_enabled),
        },
        "scope": {
            "has_sufficient_scope": scope.has_sufficient_scope,
            "scope_label": scope.scope_label,
            "scope_type": scope.scope_type,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            "search_results_accepted": page_counts.get("accept", 0),
            "search_results_rejected": page_counts.get("reject", 0),
            "search_results_review": page_counts.get("review", 0),
            "stream_candidates_accepted": stream_counts.get("accept", 0),
            "stream_candidates_rejected": stream_counts.get("reject", 0),
            "stream_candidates_review": stream_counts.get("review", 0),
        },
        "candidate_counts": {
            "raw": len(stream_candidates),
            "relevance_passed": len(validation_candidates),
            "robots_passed": len(validation_candidates),
            "http_live": len([r for r in records if r.status == "live"]),
            "http_dead": len([r for r in records if r.status == "dead"]),
            "http_unknown": len([r for r in records if r.status == "unknown"]),
            "http_timeout": 0,
            "http_other": max(0, len(records) - (len([r for r in records if r.status == "live"]) + len([r for r in records if r.status == "dead"]) + len([r for r in records if r.status == "unknown"]))),
            "ffprobe_live": len([r for r in records if r.status == "live"]) if settings.use_ffprobe_validation and validation_enabled else 0,
            "ffprobe_dead": len([r for r in records if r.status == "dead"]) if settings.use_ffprobe_validation and validation_enabled else 0,
            "ffprobe_unknown": len([r for r in records if r.status == "unknown"]) if settings.use_ffprobe_validation and validation_enabled else 0,
            "playlist_live": len([r for r in records if "playlist:live_playlist" in ((getattr(r, "notes", "") or ""))]),
            "playlist_vod": len([r for r in records if "playlist:vod_playlist" in ((getattr(r, "notes", "") or ""))]),
            "playlist_static": len([r for r in records if "playlist:static_playlist" in ((getattr(r, "notes", "") or ""))]),
            "playlist_unknown": len([r for r in records if "playlist:unknown_playlist" in ((getattr(r, "notes", "") or "") or "playlist:playlist_fetch_failed" in ((getattr(r, "notes", "") or "")))]),
            "mapped": len(mapped_records),
            "unvalidated_candidates": len(validation_candidates) if validation_skipped else 0,
            "validated_records": len(records),
            "needs_review_location_unknown": len(unknown_location),
            "rejected": sum(rejection_reasons.values()),
        },
        "rejection_reasons": rejection_reasons,
        "output_files": {"geojson": str(Path(args.output_dir) / "camera.geojson")},
    }
    (settings.log_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
    (settings.log_dir / "scope_summary.json").write_text(json.dumps({"scope": {
        "has_sufficient_scope": scope.has_sufficient_scope,
        "scope_label": scope.scope_label,
        "scope_type": scope.scope_type,
        "provider": settings.planner_provider,
        "model": settings.planner_model,
        "search_results_accepted": page_counts.get("accept", 0),
        "search_results_rejected": page_counts.get("reject", 0),
        "search_results_review": page_counts.get("review", 0),
        "stream_candidates_accepted": stream_counts.get("accept", 0),
        "stream_candidates_rejected": stream_counts.get("reject", 0),
        "stream_candidates_review": stream_counts.get("review", 0),
    }}, indent=2), encoding="utf-8")
    funnel["rejections"]["total"] = sum(rejection_reasons.values())
    funnel["rejections"]["by_reason"] = rejection_reasons
    funnel["run"]["completed_at"] = datetime.utcnow().isoformat()
    by_domain: dict[str, dict] = {}
    for c in validation_candidates:
        domain = urlparse(c.url).netloc or "unknown"
        by_domain.setdefault(domain, {"candidates_sent_to_validation": 0, "live_streams": 0, "mapped_streams": 0, "unknown_location_streams": 0, "candidates_rejected": 0})
        by_domain[domain]["candidates_sent_to_validation"] += 1
    for r in records:
        domain = urlparse(r.url).netloc or "unknown"
        by_domain.setdefault(domain, {"candidates_sent_to_validation": 0, "live_streams": 0, "mapped_streams": 0, "unknown_location_streams": 0, "candidates_rejected": 0})
        if r.status == "live":
            by_domain[domain]["live_streams"] += 1
        if r.latitude is not None and r.longitude is not None:
            by_domain[domain]["mapped_streams"] += 1
        else:
            by_domain[domain]["unknown_location_streams"] += 1
    funnel["source_performance"]["by_domain"] = by_domain
    (settings.log_dir / "discovery_funnel.json").write_text(json.dumps(funnel, indent=2), encoding="utf-8")
    (settings.log_dir / "source_performance.json").write_text(json.dumps({"by_domain": by_domain, "rejection_reasons": rejection_reasons}, indent=2), encoding="utf-8")
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

    if validation_skipped:
        logger.warning("run-agentic complete — validation skipped; mapped=0; unvalidated_candidates={}", len(validation_candidates))
    else:
        logger.info("run-agentic complete — mapped={}, needs_review_location_unknown={}, rejected={}", len(mapped_records), len(unknown_location), sum(rejection_reasons.values()))


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
    run.add_argument("--enable-feed-discovery", action="store_true", default=True)
    run.add_argument("--disable-feed-discovery", action="store_false", dest="enable_feed_discovery")
    run.add_argument("--max-feed-endpoints", type=int, default=100)
    run.add_argument("--max-feed-records", type=int, default=3000)
    run.add_argument("--max-stream-candidates", type=int, default=2500)
    run.add_argument("--per-source-stream-cap", type=int, default=500)
    run.add_argument("--preserve-direct-streams", action="store_true")
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
    run.add_argument("--disable-ffprobe-validation", action="store_true", help="Developer/debug: run ValidationAgent but skip ffprobe/ffmpeg frame-level validation")
    run.add_argument("--disable-validation", action="store_true", help="Developer/debug: skip ValidationAgent entirely and write unvalidated candidates for review only")
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

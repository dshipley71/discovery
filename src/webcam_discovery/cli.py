from __future__ import annotations

import argparse
import asyncio
import json
import uuid
import time
from datetime import datetime
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from pathlib import Path
import math
import sys

from loguru import logger

from webcam_discovery.agents.catalog import CatalogAgent
from webcam_discovery.agents.directory_crawler import DirectoryAgent
from webcam_discovery.agents.planner_agent import PlannerAgent, PlannerContext
from webcam_discovery.agents.search_agent import SearchAgent
from webcam_discovery.agents.scope_enforcement_agent import ScopeEnforcementAgent
from webcam_discovery.agents.scope_enforcement_agent import ScopeInferenceParseError
from webcam_discovery.agents.query_clarification_agent import QueryClarificationAgent
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
from webcam_discovery.skills.agentic_handoff import (
    is_direct_hls_url,
    load_agentic_candidate_handoff,
    normalize_stream_url,
    write_agentic_candidates,
)
from webcam_discovery.models.stream_analysis import StreamAnalysisResult
from webcam_discovery.llm.base import LLMRequestError

def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _normalize_url_for_dedup(url: str) -> str:
    raw = " ".join((url or "").split())
    if not raw:
        return ""
    parsed = urlparse(raw)
    kept = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower().startswith("utm_"):
            continue
        kept.append((k, v))
    normalized = parsed._replace(
        scheme=(parsed.scheme or "").lower(),
        netloc=(parsed.netloc or "").lower(),
        path=(parsed.path or "").rstrip("/") or "/",
        params="",
        query=urlencode(kept, doseq=True),
        fragment="",
    )
    return urlunparse(normalized)


def _status_bucket(status: str | None) -> str:
    value = (status or "unknown").strip().lower()
    if value in {"live", "dead", "unknown", "restricted", "timeout", "offline_http", "decode_failed"}:
        return value
    return "unknown"


def _validation_status_counts(records) -> dict[str, int]:
    counts = {"live": 0, "dead": 0, "unknown": 0, "restricted": 0, "timeout": 0, "offline_http": 0, "decode_failed": 0, "skipped": 0}
    for record in records:
        key = _status_bucket(getattr(record, "status", None))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _write_final_validation_artifacts(records) -> None:
    """Overwrite final validation artifacts so status counts match run_summary/catalog."""
    rows = []
    for record in records:
        refs = list(getattr(record, "source_refs", []) or [])
        rows.append({
            "url": getattr(record, "url", None),
            "camera_id": getattr(record, "id", None),
            "source_page": next((ref for ref in refs if isinstance(ref, str) and ref.startswith(("http://", "https://"))), None),
            "source_query": next((ref[6:] for ref in refs if isinstance(ref, str) and ref.startswith("query:")), None),
            "lineage": refs,
            "http_status": None,
            "hls_status": getattr(record, "hls_status", None),
            "stream_status": getattr(record, "status", "unknown"),
            "stream_substatus": getattr(record, "stream_substatus", None),
            "validation_confidence": getattr(record, "validation_confidence", None),
            "validation_reason": getattr(record, "validation_reason", None),
            "geocode_source": getattr(record, "geocode_source", None),
            "geocode_confidence": getattr(record, "geocode_confidence", None),
            "geocode_precision": getattr(record, "geocode_precision", None),
            "latitude": getattr(record, "latitude", None),
            "longitude": getattr(record, "longitude", None),
        })
    (settings.log_dir / "validation_results.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    counts = {"live": 0, "dead": 0, "unknown": 0, "restricted": 0, "timeout": 0, "offline_http": 0, "decode_failed": 0, "skipped": 0}
    for row in rows:
        key = _status_bucket(row.get("stream_status"))
        counts[key] = counts.get(key, 0) + 1
    (settings.log_dir / "camera_status_summary.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")


def _empty_run_summary(query: str, *, status: str, reason: str | None = None) -> dict:
    return {
        "status": status,
        "query": query,
        "reason": reason,
        "agentic_candidates": {"raw": 0, "unique_hls": 0, "duplicates_removed": 0, "sent_to_validation": 0},
        "scope_gates": {
            "search_results_accepted": 0,
            "search_results_rejected": 0,
            "search_results_review": 0,
            "stream_candidates_accepted": 0,
            "stream_candidates_rejected": 0,
            "stream_candidates_review": 0,
            "stream_candidates_allowed_for_validation_after_fallback": 0,
        },
        "validation": {"live": 0, "dead": 0, "unknown": 0, "restricted": 0, "timeout": 0, "decode_failed": 0, "skipped": 0},
        "geocoding": {"with_coordinates": 0, "without_coordinates": 0, "camera_precision": 0, "city_area_precision": 0, "fallback_precision": 0},
        "catalog": {"features_written": 0, "deduplicated": 0, "review_only": 0},
    }


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
    # Ensure this run's durable audit artifacts are not polluted by a previous run in the same output directory.
    for rel in [
        "agentic_candidates.jsonl",
        "agentic_candidates_unique.jsonl",
        "agentic_candidates_validation_handoff.jsonl",
        "prioritized_candidates.jsonl",
        "deprioritized_candidates.jsonl",
        "rejected_candidates.jsonl",
        "unvalidated_stream_candidates.jsonl",
        "needs_review_location_unknown.jsonl",
        "agentic_candidates_validation_dropped.jsonl",
        "catalog_cap_dropped.jsonl",
    ]:
        try:
            (settings.candidates_dir / rel).unlink()
        except FileNotFoundError:
            pass
    for rel in [
        "search_result_scope_decisions.jsonl",
        "stream_candidate_scope_decisions.jsonl",
        "validation_results.jsonl",
        "http_hls_probe_results.jsonl",
        "http_hls_probe_summary.json",
        "geocoding_results.jsonl",
        "camera_status_summary.json",
        "query_clarification.json",
        "query_clarification_response.json",
        "run_summary.json",
    ]:
        try:
            (settings.log_dir / rel).unlink()
        except FileNotFoundError:
            pass
    configure_logging()

    if getattr(args, "disable_ffprobe_validation", False) and validation_enabled:
        logger.warning("ffprobe validation disabled by CLI flag; HTTP/HLS validation will still run.")
    if not validation_enabled:
        logger.warning("Validation disabled by CLI flag. Unvalidated candidates will be written for review only and will not be cataloged as cameras.")

    if args.llm_provider:
        settings.planner_provider = args.llm_provider
    if args.llm_model:
        settings.planner_model = args.llm_model
    settings.scope_batch_size = int(getattr(args, "scope_batch_size", settings.scope_batch_size))
    settings.max_scope_search_results = int(getattr(args, "max_scope_search_results", settings.max_scope_search_results))
    settings.max_scope_stream_candidates = int(getattr(args, "max_scope_stream_candidates", settings.max_scope_stream_candidates))
    settings.scope_decision_timeout_seconds = float(getattr(args, "scope_decision_timeout_seconds", settings.scope_decision_timeout_seconds))
    settings.scope_gate_total_timeout_seconds = float(getattr(args, "scope_gate_total_timeout_seconds", settings.scope_gate_total_timeout_seconds))
    settings.max_scope_gate_batches = int(getattr(args, "max_scope_gate_batches", settings.max_scope_gate_batches))
    settings.scope_decision_failure_mode = str(getattr(args, "scope_timeout_fallback", getattr(args, "scope_decision_failure_mode", settings.scope_decision_failure_mode)))
    settings.stream_scope_decision_failure_mode = str(getattr(args, "stream_scope_fallback", settings.stream_scope_decision_failure_mode))
    settings.disable_llm_search_result_scope_gate = bool(getattr(args, "disable_llm_search_result_scope_gate", settings.disable_llm_search_result_scope_gate))
    settings.disable_llm_stream_scope_gate = bool(getattr(args, "disable_llm_stream_scope_gate", settings.disable_llm_stream_scope_gate))
    if args.enable_memory:
        settings.memory_enabled = True
    if args.enable_visual_analysis:
        settings.visual_stream_analysis_enabled = True
    if args.enable_video_summary:
        settings.video_summary_enabled = True
    if not validation_enabled:
        settings.visual_stream_analysis_enabled = False
        settings.video_summary_enabled = False
    stop_file = Path("/content/STOP_WEBCAM_DISCOVERY")

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

    # One-time LLM clarification preflight. This runs before scope enforcement and
    # before any discovery so ambiguous places (for example "Paris") or missing
    # scope can be resolved without broad searches. If the user does not provide
    # an answer, normal scope enforcement remains responsible for stopping the run.
    clarification_result = None
    if not getattr(args, "disable_clarification", False):
        try:
            clarification_result = await QueryClarificationAgent().analyze(args.query, plan)
        except LLMRequestError as exc:
            logger.warning(
                "QueryClarificationAgent unavailable at stage={} provider={} model={}; continuing to scope enforcement.",
                exc.stage,
                exc.provider,
                exc.model,
            )
        except Exception as exc:
            logger.warning("QueryClarificationAgent failed; continuing to scope enforcement: {}", exc)

    if clarification_result is not None:
        clarification_payload = {
            "run_id": run_id,
            "query": args.query,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
            **clarification_result.model_dump(),
        }
        (settings.log_dir / "query_clarification.json").write_text(
            json.dumps(clarification_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if clarification_result.needs_clarification:
            questions = clarification_result.questions[:3]
            answer = (getattr(args, "clarification_answer", None) or "").strip()
            if not answer and sys.stdin is not None and sys.stdin.isatty():
                logger.warning("Query clarification required before discovery: {}", clarification_result.reason)
                for idx, question in enumerate(questions, start=1):
                    print(f"Clarification {idx}: {question}")
                answer = input("Answer once, or leave blank to stop before discovery: ").strip()
            if answer:
                original_query = args.query
                clarified_query = f"{original_query} Clarification answer: {answer}"
                args.query = clarified_query
                logger.info("Using clarified query: {}", args.query)
                (settings.log_dir / "query_clarification_response.json").write_text(
                    json.dumps({
                        "run_id": run_id,
                        "original_query": original_query,
                        "questions": questions,
                        "answer": answer,
                        "clarified_query": clarified_query,
                        "provider": settings.planner_provider,
                        "model": settings.planner_model,
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                try:
                    plan = await planner.plan(args.query, context=PlannerContext(memory_hints=memory_hints))
                    _append_jsonl(settings.log_dir / "planner_runs.jsonl", {
                        "timestamp": datetime.utcnow().isoformat(),
                        "run_id": run_id,
                        "query": args.query,
                        "original_query": original_query,
                        "clarified": True,
                        "plan": plan.model_dump(),
                        "memory_hints": memory_hints,
                    })
                except LLMRequestError as exc:
                    logger.error("Planner failed after clarification before discovery started.")
                    payload = {"status": "failed_before_discovery", "failed_stage": exc.stage, "provider": exc.provider, "model": exc.model, "attempts": exc.attempts, "error_type": exc.error_type, "message": exc.message, "query": args.query, "original_query": original_query}
                    (settings.log_dir / "planner_error.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "failed_before_discovery", "failed_stage": exc.stage, "query": args.query, "original_query": original_query, "discovery_started": False, "search_started": False, "feed_discovery_started": False, "deep_discovery_started": False, "validation_started": False, "catalog_started": False, "mapped": 0, "error": {"type": exc.error_type, "message": exc.message}}, indent=2), encoding="utf-8")
                    raise SystemExit(2) from exc
            else:
                logger.warning("Clarification required; no clarification answer provided. Discovery will not run.")
                for question in questions:
                    logger.warning("Clarification question: {}", question)
                for rel in ["search_result_scope_decisions.jsonl", "stream_candidate_scope_decisions.jsonl", "search_result_scope_decision_errors.jsonl", "stream_candidate_scope_decision_errors.jsonl"]:
                    (settings.log_dir / rel).touch()
                summary = _empty_run_summary(args.query, status="needs_clarification", reason=clarification_result.reason)
                summary["clarification"] = {
                    "needed": True,
                    "clarification_type": clarification_result.clarification_type,
                    "questions": questions,
                    "candidate_interpretations": clarification_result.candidate_interpretations,
                    "provider": settings.planner_provider,
                    "model": settings.planner_model,
                    "asked_once": True,
                    "discovery_started": False,
                }
                (settings.log_dir / "run_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                return

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
        for rel in ["search_result_scope_decisions.jsonl", "stream_candidate_scope_decisions.jsonl", "search_result_scope_decision_errors.jsonl", "stream_candidate_scope_decision_errors.jsonl"]:
            (settings.log_dir / rel).touch()
        scope_summary = {"scope": {**scope.model_dump(), **llm_metadata, "search_results_accepted": 0, "search_results_rejected": 0, "search_results_review": 0, "search_result_decision_parse_errors": 0, "stream_candidates_accepted": 0, "stream_candidates_rejected": 0, "stream_candidates_review": 0, "stream_candidate_decision_parse_errors": 0}}
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
    for rel in ["search_result_scope_decisions.jsonl", "stream_candidate_scope_decisions.jsonl", "search_result_scope_decision_errors.jsonl", "stream_candidate_scope_decision_errors.jsonl"]:
        (settings.log_dir / rel).touch()

    target_resolution = TargetResolutionAgent().resolve(args.query, plan)
    (settings.log_dir / "target_resolution.json").write_text(json.dumps(target_resolution.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    if target_resolution.insufficient_target:
        logger.warning(target_resolution.message)
        (settings.log_dir / "run_summary.json").write_text(
            json.dumps(_empty_run_summary(args.query, status="insufficient_target", reason=target_resolution.message), indent=2),
            encoding="utf-8",
        )
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
    search_result_decision_parse_errors = 0
    for page in page_candidates:
        decision = await scope_agent.evaluate_search_result(page, scope)
        fallback_used = "scope_decision_parse_error" in (decision.risk_flags or [])
        normalized = not fallback_used
        if fallback_used:
            search_result_decision_parse_errors += 1
            logger.warning("Scope gate warning: malformed search-result decision for {}; rejected to prevent out-of-scope expansion.", page.url)
            try:
                _append_jsonl(settings.log_dir / "search_result_scope_decision_errors.jsonl", {
                    "timestamp": datetime.utcnow().isoformat(),
                    "stage": "search_result_scope_gate",
                    "query": page.source_query,
                    "scope_label": scope.scope_label,
                    "scope_type": scope.scope_type,
                    "url": page.url,
                    "title": page.title,
                    "snippet": page.snippet,
                    "decision_fallback": decision.decision,
                    "error_type": "ScopeDecisionParseError",
                    "error": decision.reason,
                    "raw_llm_response": decision.raw_llm_response,
                })
            except Exception as exc:
                logger.warning("Failed to write search-result scope decision error artifact: {}", exc)
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
            "normalized": normalized,
            "fallback_used": fallback_used,
            "fallback_reason": "scope_decision_parse_error" if fallback_used else None,
        })
        if decision.decision == "accept":
            gated_pages.append(page)
    if stop_file.exists():
        logger.warning("Manual stop file detected before search-result scope gate: {}", stop_file)
        (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "stopped_by_user", "query": args.query}, indent=2), encoding="utf-8")
        raise SystemExit(0)
    if settings.disable_llm_search_result_scope_gate:
        logger.warning("LLM search-result scope gate disabled (debug only).")
        page_candidates = page_candidates[: settings.max_scope_search_results]
        page_counts["accept"] = len(page_candidates)
    else:
        dedup_pages: dict[str, list[PageCandidate]] = {}
        for p in page_candidates:
            dedup_pages.setdefault(_normalize_url_for_dedup(p.url), []).append(p)
        unique_pages = [v[0] for v in dedup_pages.values()]
        capped_pages = unique_pages[: settings.max_scope_search_results]
        logger.info(
            "Search results before scope gate: raw={} unique={} duplicates_dropped={} cap_per_query={}",
            len(page_candidates), len(unique_pages), max(0, len(page_candidates)-len(unique_pages)), args.max_search_results_per_query
        )
        for skipped in unique_pages[settings.max_scope_search_results:]:
            _append_jsonl(settings.log_dir / "scope_gate_skipped_search_results.jsonl", skipped.model_dump())
        total_batches = min(settings.max_scope_gate_batches, max(1, math.ceil(len(capped_pages) / settings.scope_batch_size)))
        gated_pages = []
        page_counts = {"accept": 0, "reject": 0, "review": 0}
        search_scope_summary = {"enabled": True, "batch_size": settings.scope_batch_size, "max_items": settings.max_scope_search_results, "timeout_seconds": settings.scope_decision_timeout_seconds, "total_timeout_seconds": settings.scope_gate_total_timeout_seconds, "max_batches": settings.max_scope_gate_batches, "failure_mode": settings.scope_decision_failure_mode, "total_seen": len(page_candidates), "search_results_raw": len(page_candidates), "search_results_unique": len(unique_pages), "search_results_duplicates_dropped": max(0, len(page_candidates)-len(unique_pages)), "total_evaluated": 0, "total_skipped_due_to_cap": max(0, len(unique_pages) - len(capped_pages)), "accepted": 0, "rejected": 0, "review": 0, "timeouts": 0, "parse_errors": 0, "fallback_decisions": 0, "elapsed_seconds": 0.0}
        gate_started = time.monotonic()
        logger.info("SearchResultScopeGate: starting raw={} unique={} max_eval={} batch_size={} max_batches={} total_timeout={}s", len(page_candidates), len(unique_pages), len(capped_pages), settings.scope_batch_size, settings.max_scope_gate_batches, settings.scope_gate_total_timeout_seconds)
        for idx in range(0, min(len(capped_pages), settings.max_scope_gate_batches * settings.scope_batch_size), settings.scope_batch_size):
            if stop_file.exists():
                logger.warning("Manual stop file detected during search-result scope gate: {}", stop_file)
                break
            if (time.monotonic() - gate_started) >= settings.scope_gate_total_timeout_seconds:
                logger.warning("SearchResultScopeGate: global timeout reached at {:.1f}s", (time.monotonic()-gate_started))
                break
            batch = capped_pages[idx: idx + settings.scope_batch_size]
            batch_no = (idx // settings.scope_batch_size) + 1
            logger.info("SearchResultScopeGate: batch {}/{} size={} timeout={}s", batch_no, total_batches, len(batch), settings.scope_decision_timeout_seconds)
            decisions = await scope_agent.evaluate_search_results_batch(batch, scope, batch_size=settings.scope_batch_size)
            acc = rej = rev = 0
            for item_idx, (page, decision) in enumerate(zip(batch, decisions)):
                fallback_used = any(flag in {"scope_decision_parse_error", "scope_decision_failure_fallback"} for flag in (decision.risk_flags or []))
                if fallback_used:
                    search_scope_summary["fallback_decisions"] += 1
                if "scope_gate_timeout" in (decision.risk_flags or []):
                    _append_jsonl(settings.log_dir / "search_result_scope_decision_errors.jsonl", {"run_id": run_id, "stage": "search_result_scope_gate", "error_type": "timeout", "error": decision.reason, "timeout_seconds": settings.scope_decision_timeout_seconds, "batch_index": batch_no, "batch_total": total_batches, "affected_urls": [page.url]})
                    search_scope_summary["timeouts"] += 1
                page_counts[decision.decision] = page_counts.get(decision.decision, 0) + 1
                acc += decision.decision == "accept"
                rej += decision.decision == "reject"
                rev += decision.decision == "review"
                search_scope_summary["total_evaluated"] += 1
                _append_jsonl(settings.log_dir / "search_result_scope_decisions.jsonl", {"run_id": run_id, "stage":"search_result_scope_gate","batch_index":batch_no,"batch_total": total_batches,"item_index":item_idx,"result_url":page.url,"title":page.title,"snippet":page.snippet,"source_queries":[p.source_query for p in dedup_pages.get(_normalize_url_for_dedup(page.url), [page]) if p.source_query],"decision":decision.decision,"confidence":decision.confidence,"reason":decision.reason,"matched_scope_terms":decision.matched_scope_terms,"missing_evidence":decision.missing_evidence,"risk_flags":decision.risk_flags,"elapsed_seconds":0.0,"fallback_used":fallback_used,"fallback_reason":"batch_timeout" if "scope_gate_timeout" in (decision.risk_flags or []) else None,"timeout_seconds": settings.scope_decision_timeout_seconds if "scope_gate_timeout" in (decision.risk_flags or []) else None,"provider":settings.planner_provider,"model":settings.planner_model,"raw_llm_response":decision.raw_llm_response,"normalized":not fallback_used,"error":None})
                if decision.decision == "accept":
                    for dup in dedup_pages.get(page.url.strip().lower(), [page]):
                        gated_pages.append(dup)
            logger.info("SearchResultScopeGate: batch {}/{} completed accepted={} rejected={} review={} elapsed={:.1f}s", batch_no, total_batches, acc, rej, rev, (time.monotonic() - gate_started))
            if stop_file.exists():
                logger.warning("Manual stop file detected after search-result scope gate batch: {}", stop_file)
                break
        search_scope_summary["accepted"] = page_counts.get("accept", 0)
        search_scope_summary["rejected"] = page_counts.get("reject", 0)
        search_scope_summary["review"] = page_counts.get("review", 0)
        search_scope_summary["elapsed_seconds"] = (time.monotonic() - gate_started)
        (settings.log_dir / "search_result_scope_gate_summary.json").write_text(json.dumps(search_scope_summary, indent=2), encoding="utf-8")
        (settings.log_dir / "search_result_scope_summary.json").write_text(json.dumps(search_scope_summary, indent=2), encoding="utf-8")
        page_candidates = gated_pages
    if not page_candidates:
        direct_hls_candidates_seen = sum(1 for c in candidates if is_direct_hls_url(c.url))
        if direct_hls_candidates_seen == 0:
            logger.info("Search result scope gate completed: accepted=0 rejected={} review={}. No accepted in-scope pages or direct HLS candidates are available for discovery.", page_counts.get("reject", 0), page_counts.get("review", 0))
            summary = _empty_run_summary(args.query, status="completed_no_in_scope_pages", reason="no accepted pages and no direct HLS candidates")
            summary["scope_gates"].update({
                "search_results_accepted": page_counts.get("accept", 0),
                "search_results_rejected": page_counts.get("reject", 0),
                "search_results_review": page_counts.get("review", 0),
            })
            (settings.log_dir / "run_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            raise SystemExit(0)
        logger.info(
            "Search result scope gate accepted no pages, but {} direct HLS candidate(s) already exist; continuing to agentic_candidates.jsonl handoff.",
            direct_hls_candidates_seen,
        )
    logger.info("Search result scope gate: accepted={} rejected={} review={}", page_counts.get("accept",0), page_counts.get("reject",0), page_counts.get("review",0))
    funnel["search"]["page_candidates"] = len(page_candidates)

    # agentic_candidates.jsonl is written after feed/deep discovery so it represents
    # the complete discovery-to-validation handoff for this run.

    if getattr(args, "enable_feed_discovery", True) and page_candidates:
        if stop_file.exists():
            (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "stopped_by_user", "query": args.query}, indent=2), encoding="utf-8")
            raise SystemExit(0)
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
        candidate_by_url[normalize_stream_url(c.url)] = c
        candidate_by_url[c.url.strip().lower()] = c
        sq = c.source_query or next((ref[6:] for ref in c.source_refs if ref.startswith("query:")), None)
        src_page = c.source_page or next((ref for ref in c.source_refs if ref.startswith(("http://", "https://"))), None)
        stream_candidates.append(StreamCandidate(run_id=run_id, user_query=args.query, source_query=sq, candidate_url=c.url, source_page=src_page, root_url=src_page, discovery_strategy="search_direct", target_locations=target_locations, camera_types=camera_types, page_relevance_score=0.6, camera_likelihood_score=0.6))
    if getattr(args, "enable_deep_discovery", True) and page_candidates:
        if stop_file.exists():
            (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "stopped_by_user", "query": args.query}, indent=2), encoding="utf-8")
            raise SystemExit(0)
        triaged = SearchResultTriageAgent().triage(page_candidates, target_locations, location_search_plan.agencies, camera_types)
        deep_agent = DeepDiscoveryAgent(settings.log_dir, settings.candidates_dir, getattr(args, "max_links_per_page", 25), getattr(args, "max_js_assets_per_page", 20))
        deep_streams = await deep_agent.discover(triaged, args.query, target_locations, location_search_plan.agencies, camera_types, getattr(args, "max_deep_depth", 3), getattr(args, "max_deep_pages", 100), getattr(args, "max_network_capture_pages", 10), getattr(args, "network_capture_timeout", 8))
        stream_candidates.extend(deep_streams)

    agentic_candidates_path = settings.candidates_dir / "agentic_candidates.jsonl"
    write_agentic_candidates(agentic_candidates_path, [*candidates, *stream_candidates], run_id=run_id, user_query=args.query)
    validation_cap = getattr(args, "max_validation_candidates", None)
    if validation_cap is None and getattr(args, "max_streams", None):
        validation_cap = args.max_streams
    handoff_streams, handoff_candidate_by_url, agentic_handoff_summary = load_agentic_candidate_handoff(
        agentic_candidates_path,
        unique_output_path=settings.candidates_dir / "agentic_candidates_unique.jsonl",
        handoff_output_path=settings.candidates_dir / "agentic_candidates_validation_handoff.jsonl",
        fallback_run_id=run_id,
        fallback_user_query=args.query,
        target_locations=target_locations,
        max_candidates=validation_cap,
    )
    for key, candidate in handoff_candidate_by_url.items():
        candidate_by_url[key] = candidate
    stream_candidates = handoff_streams
    logger.info(
        "Agentic candidate handoff: loaded={} unique_hls={} skipped_non_hls={} duplicates={} capped={}",
        agentic_handoff_summary.get("raw", 0),
        agentic_handoff_summary.get("unique_hls", 0),
        agentic_handoff_summary.get("skipped_non_hls", 0),
        agentic_handoff_summary.get("duplicates_removed", 0),
        agentic_handoff_summary.get("capped", 0),
    )
    if not stream_candidates:
        logger.warning("agentic_candidates.jsonl contains no unique direct HLS candidates to validate.")

    stream_counts = {"accept": 0, "reject": 0, "review": 0}
    stream_candidate_decision_parse_errors = 0
    stream_candidates_allowed_after_fallback = 0
    scoped_stream_candidates: list[StreamCandidate] = []
    if settings.disable_llm_stream_scope_gate:
        logger.warning("LLM stream-candidate scope gate disabled (debug only).")
        scoped_stream_candidates = stream_candidates[: settings.max_scope_stream_candidates]
        stream_counts["accept"] = len(scoped_stream_candidates)
    else:
        stream_candidates = stream_candidates[: settings.max_scope_stream_candidates]
        total_batches = max(1, math.ceil(len(stream_candidates) / settings.scope_batch_size))
        gate_started = datetime.utcnow()
        for idx in range(0, len(stream_candidates), settings.scope_batch_size):
            batch = stream_candidates[idx: idx + settings.scope_batch_size]
            batch_no = (idx // settings.scope_batch_size) + 1
            logger.info("ScopeEnforcementAgent: stream candidate scope gate batch {}/{} start count={} timeout={}s", batch_no, total_batches, len(batch), settings.scope_decision_timeout_seconds)
            batch_decisions = await scope_agent.evaluate_stream_candidates_batch(batch, scope, batch_size=settings.scope_batch_size)
            for item_idx, (sc, decision) in enumerate(zip(batch, batch_decisions)):
                fallback_used = any(flag in {"scope_decision_parse_error", "scope_decision_failure_fallback"} for flag in (decision.risk_flags or []))
                validation_allowed = decision.decision == "accept" or (decision.decision == "review" and getattr(args, "allow_scope_review_candidates_to_validation", True))
                if fallback_used and validation_allowed:
                    stream_candidates_allowed_after_fallback += 1
                stream_counts[decision.decision] = stream_counts.get(decision.decision, 0) + 1
                _append_jsonl(settings.log_dir / "stream_candidate_scope_decisions.jsonl", {"run_id":run_id,"stage":"stream_candidate_scope_gate","batch_index":batch_no,"item_index":item_idx,"candidate_url":sc.candidate_url,"source_page":sc.source_page,"lineage":sc.parent_pages,"source_query":sc.source_query,"decision":decision.decision,"confidence":decision.confidence,"reason":decision.reason,"matched_scope_terms":decision.matched_scope_terms,"missing_evidence":decision.missing_evidence,"risk_flags":decision.risk_flags,"elapsed_seconds":0.0,"fallback_used":fallback_used,"fallback_reason":"scope_decision_failure_fallback" if fallback_used else None,"validation_allowed":validation_allowed,"provider":settings.planner_provider,"model":settings.planner_model,"raw_llm_response":decision.raw_llm_response,"normalized":not fallback_used,"error":None})
                if validation_allowed:
                    scoped_stream_candidates.append(sc)
                else:
                    _append_jsonl(settings.candidates_dir / "rejected_candidates.jsonl", {"candidate_url": sc.candidate_url, "reason": decision.reason, "stage": "stream_scope_gate", "fallback_used": fallback_used})
            logger.info("ScopeEnforcementAgent: stream candidate scope gate batch {}/{} done accepted={} rejected={} review={} elapsed={:.1f}s", batch_no, total_batches, stream_counts.get('accept',0), stream_counts.get('reject',0), stream_counts.get('review',0), 0.0)
        (settings.log_dir / "stream_candidate_scope_gate_summary.json").write_text(json.dumps({"enabled": True, "batch_size": settings.scope_batch_size, "max_items": settings.max_scope_stream_candidates, "timeout_seconds": settings.scope_decision_timeout_seconds, "failure_mode": settings.stream_scope_decision_failure_mode, "total_seen": len(stream_candidates), "total_evaluated": len(stream_candidates), "total_skipped_due_to_cap": 0, "accepted": stream_counts.get("accept",0), "rejected": stream_counts.get("reject",0), "review": stream_counts.get("review",0), "timeouts": 0, "parse_errors": stream_candidate_decision_parse_errors, "fallback_allowed_for_validation": stream_candidates_allowed_after_fallback, "elapsed_seconds": (datetime.utcnow()-gate_started).total_seconds()}, indent=2), encoding="utf-8")
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
            original = candidate_by_url.get(normalize_stream_url(sc.candidate_url)) or candidate_by_url.get(sc.candidate_url.strip().lower())
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
            _append_jsonl(settings.candidates_dir / "agentic_candidates_validation_handoff.jsonl", {"candidate_url": base.url, "normalized_stream_url": normalize_stream_url(base.url), "sent_to_validation": True, "priority": decision.priority, "priority_reason": decision.priority_reason, "source_page": base.source_page, "source_query": base.source_query})
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
        if stop_file.exists():
            (settings.log_dir / "run_summary.json").write_text(json.dumps({"status": "stopped_by_user", "query": args.query}, indent=2), encoding="utf-8")
            raise SystemExit(0)
        validator = ValidationAgent()
        records = await validator.run(candidates=validation_candidates)
        if args.max_streams and len(records) > args.max_streams:
            dropped_records = records[args.max_streams:]
            drop_rows = []
            for record in dropped_records:
                drop_rows.append({
                    "url": record.url,
                    "camera_id": record.id,
                    "stage": "catalog_cap",
                    "reason": "max_streams_cap",
                    "cap": args.max_streams,
                    "record_count_before_cap": len(records),
                    "record_count_after_cap": args.max_streams,
                })
            (settings.candidates_dir / "catalog_cap_dropped.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in drop_rows),
                encoding="utf-8",
            )
            rejection_reasons["max_streams_cap"] = rejection_reasons.get("max_streams_cap", 0) + len(dropped_records)
            records = records[: args.max_streams]
            _write_final_validation_artifacts(records)
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
        _write_final_validation_artifacts(records)

    mapped_records = [r for r in records if r.latitude is not None and r.longitude is not None]
    unknown_location = [r for r in records if r.latitude is None or r.longitude is None]
    funnel["validation"]["records_with_coordinates"] = len(mapped_records)
    funnel["validation"]["records_without_coordinates"] = len(unknown_location)
    funnel["validation"]["validated_records"] = len(records)
    for r in unknown_location:
        _append_jsonl(settings.candidates_dir / "needs_review_location_unknown.jsonl", r.model_dump())

    validation_counts = _validation_status_counts(records)
    if validation_skipped:
        validation_counts["skipped"] = len(validation_candidates)
    geocode_summary = {
        "with_coordinates": len(mapped_records),
        "without_coordinates": len(unknown_location),
        "camera_precision": len([r for r in records if getattr(r, "geocode_precision", None) in {"camera", "intersection", "road_segment"}]),
        "city_area_precision": len([r for r in records if getattr(r, "geocode_precision", None) in {"city_area", "city_centroid"}]),
        "fallback_precision": len([r for r in records if getattr(r, "geocode_source", None) == "scope_fallback"]),
    }
    run_summary = {
        "status": "completed",
        "query": args.query,
        "targets": target_locations,
        "agentic_candidates": {
            "raw": agentic_handoff_summary.get("raw", 0),
            "unique_hls": agentic_handoff_summary.get("unique_hls", 0),
            "duplicates_removed": agentic_handoff_summary.get("duplicates_removed", 0),
            "skipped_non_hls": agentic_handoff_summary.get("skipped_non_hls", 0),
            "sent_to_validation": len(validation_candidates),
        },
        "scope_gates": {
            "search_results_accepted": page_counts.get("accept", 0),
            "search_results_rejected": page_counts.get("reject", 0),
            "search_results_review": page_counts.get("review", 0),
            "stream_candidates_accepted": stream_counts.get("accept", 0),
            "stream_candidates_rejected": stream_counts.get("reject", 0),
            "stream_candidates_review": stream_counts.get("review", 0),
            "stream_candidates_allowed_for_validation_after_fallback": stream_candidates_allowed_after_fallback,
            "search_result_decision_parse_errors": search_result_decision_parse_errors,
            "stream_candidate_decision_parse_errors": stream_candidate_decision_parse_errors,
        },
        "validation": {
            **validation_counts,
            "validation_enabled": validation_enabled,
            "validation_skipped": validation_skipped,
            "ffprobe_validation_enabled": bool(settings.use_ffprobe_validation and validation_enabled),
        },
        "geocoding": geocode_summary,
        "catalog": {"features_written": len(mapped_records), "deduplicated": 0, "review_only": len(unknown_location)},
        "scope": {
            "has_sufficient_scope": scope.has_sufficient_scope,
            "scope_label": scope.scope_label,
            "scope_type": scope.scope_type,
            "provider": settings.planner_provider,
            "model": settings.planner_model,
        },
        "candidate_counts": {
            "raw": len(stream_candidates),
            "relevance_passed": len(validation_candidates),
            "validated_records": len(records),
            "mapped": len(mapped_records),
            "needs_review_location_unknown": len(unknown_location),
            "unvalidated_candidates": len(validation_candidates) if validation_skipped else 0,
            "rejected": sum(rejection_reasons.values()),
        },
        "rejection_reasons": rejection_reasons,
        "output_files": {
            "agentic_candidates": str(settings.candidates_dir / "agentic_candidates.jsonl"),
            "agentic_candidates_unique": str(settings.candidates_dir / "agentic_candidates_unique.jsonl"),
            "validation_handoff": str(settings.candidates_dir / "agentic_candidates_validation_handoff.jsonl"),
            "geojson": str(Path(args.output_dir) / "camera.geojson"),
            "map": str(Path(args.output_dir) / "map.html"),
        },
    }
    logger.info(
        "Validation: live={} dead={} restricted={} unknown={} timeout={} decode_failed={} skipped={}",
        validation_counts.get("live", 0),
        validation_counts.get("dead", 0),
        validation_counts.get("restricted", 0),
        validation_counts.get("unknown", 0),
        validation_counts.get("timeout", 0),
        validation_counts.get("decode_failed", 0),
        validation_counts.get("skipped", 0),
    )
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
        "search_result_decision_parse_errors": search_result_decision_parse_errors,
        "stream_candidates_accepted": stream_counts.get("accept", 0),
        "stream_candidates_rejected": stream_counts.get("reject", 0),
        "stream_candidates_review": stream_counts.get("review", 0),
        "stream_candidate_decision_parse_errors": stream_candidate_decision_parse_errors,
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
    catalog_summary_path = settings.log_dir / "catalog_export_summary.json"
    if catalog_summary_path.exists():
        try:
            catalog_summary = json.loads(catalog_summary_path.read_text(encoding="utf-8"))
            run_summary["catalog"] = {
                "features_written": catalog_summary.get("geojson_features_written", 0),
                "deduplicated": catalog_summary.get("records_deduplicated", 0),
                "review_only": len(unknown_location),
            }
            (settings.log_dir / "run_summary.json").write_text(json.dumps(run_summary, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Could not update run_summary.json with catalog summary: {}", exc)

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
    run.add_argument("--clarification-answer", type=str, default=None, help="One-time answer to an LLM clarification question for ambiguous or underspecified queries")
    run.add_argument("--disable-clarification", action="store_true", help="Developer/debug: skip one-time LLM clarification preflight and rely on scope enforcement only")
    run.add_argument("--scope-batch-size", type=int, default=settings.scope_batch_size)
    run.add_argument("--max-scope-search-results", type=int, default=settings.max_scope_search_results)
    run.add_argument("--max-scope-stream-candidates", type=int, default=settings.max_scope_stream_candidates)
    run.add_argument("--scope-decision-timeout-seconds", type=float, default=settings.scope_decision_timeout_seconds)
    run.add_argument("--scope-gate-total-timeout-seconds", type=float, default=settings.scope_gate_total_timeout_seconds)
    run.add_argument("--max-scope-gate-batches", type=int, default=settings.max_scope_gate_batches)
    run.add_argument("--scope-decision-failure-mode", type=str, choices=["review", "reject"], default=settings.scope_decision_failure_mode)
    run.add_argument("--scope-timeout-fallback", type=str, choices=["review", "reject"], default=settings.scope_decision_failure_mode)
    run.add_argument("--validate-agentic-candidates", action=argparse.BooleanOptionalAction, default=True, help="Use candidates/agentic_candidates.jsonl as the validation handoff after discovery")
    run.add_argument("--allow-scope-review-candidates-to-validation", action=argparse.BooleanOptionalAction, default=True, help="Allow stream candidates marked review by the LLM scope gate to proceed to validation")
    run.add_argument("--stream-scope-fallback", type=str, choices=["review", "reject", "accept_for_validation"], default=settings.stream_scope_decision_failure_mode, help="Fallback for stream-candidate LLM scope failures; review/accept_for_validation still records audit risk flags")
    run.add_argument("--max-validation-candidates", type=int, default=None, help="Optional cap on unique direct HLS candidates loaded from agentic_candidates.jsonl")
    run.add_argument("--disable-llm-search-result-scope-gate", action="store_true")
    run.add_argument("--disable-llm-stream-scope-gate", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run-agentic":
        asyncio.run(run_agentic(args))


if __name__ == "__main__":
    main()

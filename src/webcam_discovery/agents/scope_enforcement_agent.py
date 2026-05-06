from __future__ import annotations

import json
import asyncio
import time
from typing import Any
from loguru import logger
from pydantic import ValidationError

from webcam_discovery.agents.planner_agent import PlannerAgent
from webcam_discovery.config import settings
from webcam_discovery.llm.base import get_llm_request_policy
from webcam_discovery.models.deep_discovery import PageCandidate, StreamCandidate
from webcam_discovery.models.planner import PlannerPlan
from webcam_discovery.prompts.scope_enforcement import (
    build_scope_inference_prompt,
    build_search_result_scope_prompt,
    build_stream_scope_prompt,
)
from webcam_discovery.schemas import CameraCandidate, ScopeDecision, ScopeEnforcementResult

SYSTEM_PROMPT = (
    "You are ScopeEnforcementAgent for public webcam discovery. "
    "Return STRICT JSON only. Never assume defaults."
)


class ScopeEnforcementAgent:
    def __init__(self) -> None:
        self.backend = PlannerAgent().backend

    async def infer_scope(self, user_query: str, planner_plan: PlannerPlan) -> ScopeEnforcementResult:
        policy = get_llm_request_policy("scope_inference")
        logger.info("ScopeEnforcementAgent: requesting LLM scope inference provider={} model={} timeout_read={}s attempts={}", settings.planner_provider, settings.planner_model, policy.read_timeout, policy.max_attempts)
        raw = await self.backend.generate(
            build_scope_inference_prompt(user_query, planner_plan.model_dump()),
            system_prompt=SYSTEM_PROMPT,
            stage="scope_inference",
        )
        parsed = self._extract_json(raw)
        normalized = self.normalize_scope_result_payload(parsed)
        try:
            return ScopeEnforcementResult.model_validate({**normalized, "raw_llm_response": parsed})
        except ValidationError as exc:
            raise ScopeInferenceParseError(
                "Scope inference payload failed schema validation.",
                raw_llm_response=parsed,
                normalized_payload=normalized,
            ) from exc

    async def evaluate_search_result(self, page: PageCandidate, scope: ScopeEnforcementResult) -> ScopeDecision:
        decisions = await self.evaluate_search_results_batch([page], scope, batch_size=1)
        return decisions[0]

    async def evaluate_stream_candidate(self, candidate: StreamCandidate | CameraCandidate, scope: ScopeEnforcementResult) -> ScopeDecision:
        decisions = await self.evaluate_stream_candidates_batch([candidate], scope, batch_size=1)
        return decisions[0]

    async def evaluate_search_results_batch(self, pages: list[PageCandidate], scope: ScopeEnforcementResult, *, batch_size: int | None = None) -> list[ScopeDecision]:
        return await self._evaluate_batch(
            items=pages,
            scope=scope,
            stage="search_result_scope_gate",
            llm_stage="scope_search_result",
            batch_size=batch_size,
            prompt_builder=build_search_result_scope_prompt,
        )

    async def evaluate_stream_candidates_batch(self, candidates: list[StreamCandidate | CameraCandidate], scope: ScopeEnforcementResult, *, batch_size: int | None = None) -> list[ScopeDecision]:
        return await self._evaluate_batch(
            items=candidates,
            scope=scope,
            stage="stream_candidate_scope_gate",
            llm_stage="scope_stream_candidate",
            batch_size=batch_size,
            prompt_builder=build_stream_scope_prompt,
        )

    async def _evaluate_batch(self, *, items: list[Any], scope: ScopeEnforcementResult, stage: str, llm_stage: str, batch_size: int | None, prompt_builder: Any) -> list[ScopeDecision]:
        if not items:
            return []
        payload_items = [i.model_dump() if hasattr(i, "model_dump") else dict(i) for i in items]
        prompt_payload = {"items": payload_items, "scope_rules": "Do not accept solely because query text matches target or URL is .m3u8"}
        prompt = prompt_builder(prompt_payload, scope.model_dump())
        timeout = float(settings.scope_decision_timeout_seconds)
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                self.backend.generate(prompt, system_prompt=SYSTEM_PROMPT, stage=llm_stage),
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("ScopeEnforcementAgent: {} batch timed out/failed after {}s; fallback={}", stage, timeout, settings.scope_decision_failure_mode)
            return [self._fallback_for_failure(stage=stage, raw_llm_response={"error": str(exc), "timeout_seconds": timeout}) for _ in items]
        parsed = self._extract_json(raw) if isinstance(raw, str) else raw
        rows = parsed.get("decisions") if isinstance(parsed, dict) else None
        if not isinstance(rows, list):
            rows = [parsed] if isinstance(parsed, dict) else []
        out: list[ScopeDecision] = []
        for idx, item in enumerate(items):
            if idx >= len(rows):
                out.append(self._fallback_for_failure(stage=stage, raw_llm_response={"error": "missing_batch_decision", "raw": parsed}))
                continue
            out.append(self._parse_scope_decision(raw=rows[idx], stage=stage))
        if len(rows) > len(items):
            logger.warning("ScopeEnforcementAgent: {} returned extra decisions; extra={} ignored", stage, len(rows) - len(items))
        logger.debug("ScopeEnforcementAgent: {} batch done count={} elapsed={:.2f}s", stage, len(items), time.perf_counter() - started)
        return out

    def _parse_scope_decision(self, *, raw: Any, stage: str) -> ScopeDecision:
        try:
            parsed = self._extract_json(raw) if isinstance(raw, str) else raw
            if not isinstance(parsed, dict):
                raise ValueError("Scope decision payload is not a JSON object.")
            normalized = self._normalize_scope_decision_payload(parsed)
            return ScopeDecision.model_validate(normalized)
        except Exception as exc:  # never break discovery flow from malformed scope gate payload
            return self._safe_fallback_scope_decision(
                stage=stage,
                reason="LLM scope decision could not be parsed safely; rejecting candidate to prevent out-of-scope expansion.",
                raw_llm_response=raw,
                error=exc,
            )

    @staticmethod
    def _coerce_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                if item is None:
                    continue
                text = str(item).strip()
                if text:
                    result.append(text)
            return result
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def _coerce_float(value: Any, default: float = 0.0) -> float:
        if value is None:
            return default
        try:
            num = float(value)
        except (TypeError, ValueError):
            return default
        if num > 1.0 and num <= 100.0:
            num = num / 100.0
        return max(0.0, min(1.0, num))

    @staticmethod
    def _coerce_decision(value: Any) -> str:
        if value is None:
            return "review"
        text = str(value).strip().lower()
        if text in {"accept", "reject", "review"}:
            return text
        aliases = {"accepted": "accept", "rejected": "reject", "approve": "accept", "deny": "reject"}
        return aliases.get(text, "review")

    @staticmethod
    def _coerce_reason(value: Any) -> str:
        if value is None:
            return "No reason provided by LLM."
        if isinstance(value, str):
            value = value.strip()
            return value or "No reason provided by LLM."
        if isinstance(value, list):
            joined = " ".join(str(v).strip() for v in value if v is not None and str(v).strip())
            return joined or "No reason provided by LLM."
        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                return str(value)
        return str(value)

    def _normalize_scope_decision_payload(self, parsed: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {
            "decision": self._coerce_decision(parsed.get("decision")),
            "confidence": self._coerce_float(parsed.get("confidence"), default=0.0),
            "reason": self._coerce_reason(parsed.get("reason")),
            "matched_scope_terms": self._coerce_list(parsed.get("matched_scope_terms")),
            "missing_evidence": self._coerce_list(parsed.get("missing_evidence")),
            "risk_flags": self._coerce_list(parsed.get("risk_flags")),
            "raw_llm_response": parsed,
        }
        return normalized

    def _safe_fallback_scope_decision(self, *, stage: str, reason: str, raw_llm_response: Any, error: Exception | str | None = None) -> ScopeDecision:
        if error is not None:
            logger.warning("Scope gate parse error at stage={}: {}", stage, error)
        if stage == "stream_candidate_scope_gate":
            mode = (settings.stream_scope_decision_failure_mode or "review").strip().lower()
            decision = "reject" if mode == "reject" else "review"
            risk_flags = ["scope_decision_parse_error", "scope_decision_failure_fallback"]
            if decision == "review":
                risk_flags.append("validation_allowed_after_scope_fallback")
            fallback_reason = (
                "LLM stream-candidate scope decision could not be parsed safely; "
                f"applied fallback={mode}."
            )
        else:
            decision = "reject"
            risk_flags = ["scope_decision_parse_error"]
            fallback_reason = reason
        fallback_payload = {
            "decision": decision,
            "confidence": 0.0,
            "reason": fallback_reason,
            "matched_scope_terms": [],
            "missing_evidence": ["Scope decision parse failure."],
            "risk_flags": risk_flags,
            "raw_llm_response": raw_llm_response,
        }
        try:
            return ScopeDecision.model_validate(fallback_payload)
        except ValidationError:
            return ScopeDecision(decision=decision, confidence=0.0, reason=fallback_reason)

    def _fallback_for_failure(self, *, stage: str, raw_llm_response: Any) -> ScopeDecision:
        if stage == "stream_candidate_scope_gate":
            mode = (settings.stream_scope_decision_failure_mode or "review").strip().lower()
        else:
            mode = (settings.scope_decision_failure_mode or "reject").strip().lower()
        decision = "review" if mode in {"review", "accept_for_validation"} else "reject"
        risk_flags = ["scope_decision_failure_fallback"]
        if stage == "stream_candidate_scope_gate" and decision == "review":
            risk_flags.append("validation_allowed_after_scope_fallback")
        return ScopeDecision(
            decision=decision,
            confidence=0.0,
            reason=f"LLM scope decision unavailable at {stage}; applied fallback={mode}.",
            matched_scope_terms=[],
            missing_evidence=["llm_scope_decision_unavailable"],
            risk_flags=risk_flags,
            raw_llm_response=raw_llm_response,
        )

    @staticmethod
    def _extract_json(text: str) -> dict:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.lower().startswith("json"):
                stripped = stripped[4:].strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                return json.loads(stripped[start : end + 1])
            raise

    @staticmethod
    def normalize_scope_result_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload or {})

        def to_list(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, list):
                return [str(v) for v in value if v is not None and str(v).strip()]
            return [str(value)]

        list_fields = [
            "normalized_targets",
            "target_aliases",
            "included_locations",
            "excluded_locations",
            "included_sources",
            "excluded_sources",
            "hostnames",
            "ip_addresses",
            "camera_types",
        ]
        for field in list_fields:
            normalized[field] = to_list(normalized.get(field))

        legacy_agency = normalized.pop("agency_or_owner", None)
        agencies = normalized.get("agency_or_owners")
        merged_agencies = to_list(agencies)
        if legacy_agency is not None:
            merged_agencies.extend(to_list(legacy_agency))
        normalized["agency_or_owners"] = merged_agencies

        coords = normalized.get("coordinates")
        if isinstance(coords, dict):
            coords = [coords]
        if not isinstance(coords, list):
            coords = []
        normalized["coordinates"] = [c for c in coords if isinstance(c, dict)]

        for scalar_field in ["scope_type", "scope_label", "scope_summary", "insufficient_scope_reason", "user_message"]:
            value = normalized.get(scalar_field)
            if isinstance(value, list):
                normalized[scalar_field] = " ".join(str(v) for v in value if v is not None).strip() or None

        confidence = normalized.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        normalized["confidence"] = max(0.0, min(1.0, confidence))

        has_scope = normalized.get("has_sufficient_scope")
        if not isinstance(has_scope, bool):
            raise ScopeInferenceParseError(
                "Missing or invalid has_sufficient_scope; expected boolean.",
                raw_llm_response=payload,
                normalized_payload=normalized,
            )
        return normalized


class ScopeInferenceParseError(ValueError):
    def __init__(self, message: str, raw_llm_response: Any, normalized_payload: Any) -> None:
        super().__init__(message)
        self.raw_llm_response = raw_llm_response
        self.normalized_payload = normalized_payload

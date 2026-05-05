from __future__ import annotations

import json
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

    async def evaluate_search_result(
        self,
        page: PageCandidate,
        scope: ScopeEnforcementResult,
    ) -> ScopeDecision:
        logger.info("ScopeEnforcementAgent: evaluating search result scope batch=1 count=1")
        raw = await self.backend.generate(
            build_search_result_scope_prompt(page.model_dump(), scope.model_dump()),
            system_prompt=SYSTEM_PROMPT,
            stage="scope_search_result",
        )
        parsed = self._extract_json(raw)
        return ScopeDecision.model_validate({**parsed, "raw_llm_response": parsed})

    async def evaluate_stream_candidate(
        self,
        candidate: StreamCandidate | CameraCandidate,
        scope: ScopeEnforcementResult,
    ) -> ScopeDecision:
        payload = candidate.model_dump() if hasattr(candidate, "model_dump") else {}
        raw = await self.backend.generate(
            build_stream_scope_prompt(payload, scope.model_dump()),
            system_prompt=SYSTEM_PROMPT,
            stage="scope_stream_candidate",
        )
        parsed = self._extract_json(raw)
        return ScopeDecision.model_validate({**parsed, "raw_llm_response": parsed})

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

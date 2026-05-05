from __future__ import annotations

import json
from loguru import logger

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
        return ScopeEnforcementResult.model_validate({**parsed, "raw_llm_response": parsed})

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

from __future__ import annotations

import json
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from webcam_discovery.agents.planner_agent import PlannerAgent
from webcam_discovery.config import settings
from webcam_discovery.models.planner import PlannerPlan
from webcam_discovery.prompts.query_clarification import build_query_clarification_prompt

SYSTEM_PROMPT = (
    "You are QueryClarificationAgent for a public webcam discovery system. "
    "Return STRICT JSON only. Ask at most one clarification turn with 1-3 questions. "
    "Never assume a default location."
)


class QueryClarificationResult(BaseModel):
    needs_clarification: bool = False
    clarification_type: str = "none"
    reason: str = ""
    questions: list[str] = Field(default_factory=list)
    candidate_interpretations: list[str] = Field(default_factory=list)
    adjusted_query: str | None = None
    confidence: float = 0.0
    raw_llm_response: dict[str, Any] | str | None = None


class QueryClarificationAgent:
    def __init__(self) -> None:
        self.backend = PlannerAgent().backend

    async def analyze(self, user_query: str, planner_plan: PlannerPlan) -> QueryClarificationResult:
        logger.info(
            "QueryClarificationAgent: checking query ambiguity provider={} model={}",
            settings.planner_provider,
            settings.planner_model,
        )
        raw = await self.backend.generate(
            build_query_clarification_prompt(user_query, planner_plan.model_dump()),
            system_prompt=SYSTEM_PROMPT,
            stage="query_clarification",
        )
        parsed = self._extract_json(raw)
        normalized = self._normalize(parsed, original_query=user_query)
        try:
            return QueryClarificationResult.model_validate({**normalized, "raw_llm_response": parsed})
        except ValidationError as exc:
            logger.warning("QueryClarificationAgent: malformed response; falling back to no clarification: {}", exc)
            return QueryClarificationResult(
                needs_clarification=False,
                clarification_type="none",
                reason="Clarification response could not be parsed; continuing to scope enforcement.",
                questions=[],
                candidate_interpretations=[],
                adjusted_query=user_query,
                confidence=0.0,
                raw_llm_response=parsed,
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
    def _as_str_list(value: Any, *, limit: int | None = None) -> list[str]:
        if value is None:
            values: list[str] = []
        elif isinstance(value, list):
            values = [str(v).strip() for v in value if v is not None and str(v).strip()]
        else:
            text = str(value).strip()
            values = [text] if text else []
        if limit is not None:
            values = values[:limit]
        return values

    @classmethod
    def _normalize(cls, payload: dict[str, Any], *, original_query: str) -> dict[str, Any]:
        normalized = dict(payload or {})
        needs = bool(normalized.get("needs_clarification", False))
        questions = cls._as_str_list(normalized.get("questions"), limit=3)
        clarification_type = str(normalized.get("clarification_type") or ("insufficient_scope" if needs else "none")).strip().lower()
        if clarification_type not in {"ambiguous_place", "insufficient_scope", "conflicting_scope", "none"}:
            clarification_type = "insufficient_scope" if needs else "none"
        adjusted_query = normalized.get("adjusted_query")
        if adjusted_query is not None:
            adjusted_query = str(adjusted_query).strip() or None
        if needs and not questions:
            questions = ["What specific place, location, landmark, agency, or public website should I use for this camera search?"]
        if not needs and not adjusted_query:
            adjusted_query = original_query
        try:
            confidence = float(normalized.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence if confidence <= 1.0 else confidence / 100.0))
        return {
            "needs_clarification": needs,
            "clarification_type": clarification_type,
            "reason": str(normalized.get("reason") or "").strip(),
            "questions": questions,
            "candidate_interpretations": cls._as_str_list(normalized.get("candidate_interpretations")),
            "adjusted_query": adjusted_query,
            "confidence": confidence,
        }

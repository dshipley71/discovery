from __future__ import annotations

import json
from dataclasses import dataclass

from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.llm.base import LLMBackend
from webcam_discovery.llm.ollama import OllamaCloudBackend
from webcam_discovery.llm.openai_compatible import OpenAICompatibleBackend
from webcam_discovery.models.planner import PlannerIntent, PlannerPlan


SYSTEM_PROMPT = """You are PlannerAgent for a public webcam discovery system.
Return STRICT JSON only, no markdown. Keep reasoning_summary concise.
Never suggest private/restricted/auth sources."
"""


@dataclass(slots=True)
class PlannerContext:
    memory_hints: list[str]


class PlannerAgent:
    def __init__(self, backend: LLMBackend | None = None) -> None:
        self.backend = backend or self._build_backend()

    def _build_backend(self) -> LLMBackend:
        provider = settings.planner_provider
        if provider == "ollama":
            return OllamaCloudBackend()
        if provider == "openai-compatible":
            return OpenAICompatibleBackend()
        raise RuntimeError(
            f"Unsupported planner provider '{provider}'. Supported: ollama, openai-compatible"
        )

    async def plan(self, query: str, context: PlannerContext | None = None) -> PlannerPlan:
        memory_section = "\n".join(f"- {hint}" for hint in (context.memory_hints if context else []))
        prompt = f"""
User query: {query}

Memory hints (may be empty):
{memory_section or '- none'}

Create a JSON object with keys:
original_query, parsed_intent, target_locations, camera_types, discovery_methods,
source_preferences, validation_enabled, visual_stream_analysis_enabled,
video_summary_enabled, output_artifacts, public_source_only, skip_restricted_sources,
reasoning_summary.

parsed_intent must include geography, agencies, camera_types.
Set validation_enabled/public_source_only/skip_restricted_sources true.
Prefer discovery_methods from [directory_search, web_search, known_sources].
""".strip()
        raw = await self.backend.generate(prompt, system_prompt=SYSTEM_PROMPT)
        plan_dict = self._extract_json(raw)
        plan_dict = self._normalize_plan_dict(plan_dict)
        plan = PlannerPlan.model_validate(plan_dict)
        logger.info("PlannerAgent: plan created for query='{}'", query)
        return plan

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
    def _as_list(value: object) -> list[str]:
        """Normalize planner output fields that may be returned as a scalar string."""
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        return [str(value).strip()]

    @classmethod
    def _normalize_plan_dict(cls, plan_dict: dict) -> dict:
        """
        Coerce common LLM schema drift into PlannerPlan-compatible shapes.

        In real runs, many models occasionally emit scalar strings for list
        fields (e.g. "geography": "Pennsylvania"). This normalizes those
        fields without introducing any mock/fallback planning.
        """
        normalized = dict(plan_dict)
        parsed_intent = dict(normalized.get("parsed_intent") or {})

        parsed_intent["geography"] = cls._as_list(parsed_intent.get("geography"))
        parsed_intent["agencies"] = cls._as_list(parsed_intent.get("agencies"))
        parsed_intent["camera_types"] = cls._as_list(parsed_intent.get("camera_types"))

        normalized["parsed_intent"] = parsed_intent
        normalized["target_locations"] = cls._as_list(normalized.get("target_locations"))
        normalized["camera_types"] = cls._as_list(normalized.get("camera_types"))
        normalized["discovery_methods"] = cls._as_list(normalized.get("discovery_methods"))
        normalized["source_preferences"] = cls._as_list(normalized.get("source_preferences"))
        normalized["output_artifacts"] = cls._as_list(normalized.get("output_artifacts"))

        return normalized

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

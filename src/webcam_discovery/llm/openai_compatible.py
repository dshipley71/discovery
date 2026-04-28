from __future__ import annotations

import httpx

from webcam_discovery.config import settings
from webcam_discovery.llm.base import LLMBackend, LLMConfigurationError


class OpenAICompatibleBackend(LLMBackend):
    def __init__(self) -> None:
        if not settings.planner_api_key:
            raise LLMConfigurationError(
                "Missing WCD_PLANNER_API_KEY for openai-compatible planner backend."
            )
        if not settings.planner_base_url:
            raise LLMConfigurationError(
                "Missing WCD_PLANNER_BASE_URL for openai-compatible planner backend."
            )
        self.api_key = settings.planner_api_key
        self.base_url = settings.planner_base_url.rstrip("/")
        self.model = settings.planner_model

    async def generate(self, prompt: str, *, system_prompt: str | None = None) -> str:
        url = self.base_url + "/v1/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or "You are a strict JSON planner."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(timeout=settings.request_timeout * 3) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI-compatible response missing choices")
        return choices[0].get("message", {}).get("content", "")

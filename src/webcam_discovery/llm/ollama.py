from __future__ import annotations

import httpx

from webcam_discovery.config import settings
from webcam_discovery.llm.base import LLMBackend, LLMConfigurationError


class OllamaCloudBackend(LLMBackend):
    def __init__(self) -> None:
        if not settings.ollama_api_key:
            raise LLMConfigurationError(
                "Missing WCD_OLLAMA_API_KEY. Configure a real Ollama Cloud key before running run-agentic."
            )
        self.base_url = settings.planner_base_url or settings.ollama_base_url
        self.model = settings.planner_model or settings.ollama_model

    async def generate(self, prompt: str, *, system_prompt: str | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or "You are a strict JSON planner."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {settings.ollama_api_key}",
            "Content-Type": "application/json",
        }
        url = self.base_url.rstrip("/") + "/api/chat"
        async with httpx.AsyncClient(timeout=settings.request_timeout * 3) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
        message = body.get("message", {})
        content = message.get("content")
        if not content:
            raise RuntimeError("Ollama response missing message.content")
        return content

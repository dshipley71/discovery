from __future__ import annotations

import httpx
import asyncio
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.llm.base import LLMBackend, LLMConfigurationError, LLMRequestError, get_llm_request_policy


class OllamaCloudBackend(LLMBackend):
    def __init__(self) -> None:
        if not settings.ollama_api_key:
            raise LLMConfigurationError(
                "Missing WCD_OLLAMA_API_KEY. Configure a real Ollama Cloud key before running run-agentic."
            )
        self.base_url = settings.planner_base_url or settings.ollama_base_url
        self.model = settings.planner_model or settings.ollama_model

    async def generate(self, prompt: str, *, system_prompt: str | None = None, stage: str = "other") -> str:
        policy = get_llm_request_policy(stage)
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
        timeout = httpx.Timeout(
            connect=policy.connect_timeout,
            read=policy.read_timeout,
            write=policy.write_timeout,
            pool=policy.pool_timeout,
        )
        retryable = (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.WriteTimeout, httpx.ConnectError, httpx.RemoteProtocolError)
        transient_codes = {429, 500, 502, 503, 504}
        last_exc: Exception | None = None
        for attempt in range(1, max(1, policy.max_attempts) + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code in transient_codes:
                    raise httpx.HTTPStatusError(f"Transient status {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                body = resp.json()
                if attempt > 1:
                    logger.info("LLM request stage={} provider=ollama model={} attempt={}/{} succeeded", stage, self.model, attempt, policy.max_attempts)
                break
            except retryable as exc:
                last_exc = exc
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response is None or exc.response.status_code not in transient_codes:
                    raise
            if attempt < policy.max_attempts:
                logger.warning("LLM request stage={} provider=ollama model={} attempt={}/{} failed: {}", stage, self.model, attempt, policy.max_attempts, type(last_exc).__name__)
                logger.warning("Retrying in {}s", policy.retry_backoff_seconds)
                await asyncio.sleep(policy.retry_backoff_seconds)
            else:
                raise LLMRequestError(stage=stage, provider="ollama", model=self.model, attempts=policy.max_attempts, error_type=type(last_exc).__name__, message=f"LLM request failed after {policy.max_attempts} attempts: {type(last_exc).__name__}") from last_exc
        message = body.get("message", {})
        content = message.get("content")
        if not content:
            raise RuntimeError("Ollama response missing message.content")
        return content

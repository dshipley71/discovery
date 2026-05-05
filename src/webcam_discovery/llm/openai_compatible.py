from __future__ import annotations

import httpx
import asyncio
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.llm.base import LLMBackend, LLMConfigurationError, LLMRequestError, get_llm_request_policy


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

    async def generate(self, prompt: str, *, system_prompt: str | None = None, stage: str = "other") -> str:
        policy = get_llm_request_policy(stage)
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
        timeout = httpx.Timeout(connect=policy.connect_timeout, read=policy.read_timeout, write=policy.write_timeout, pool=policy.pool_timeout)
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
                    logger.info("LLM request stage={} provider=openai-compatible model={} attempt={}/{} succeeded", stage, self.model, attempt, policy.max_attempts)
                break
            except retryable as exc:
                last_exc = exc
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response is None or exc.response.status_code not in transient_codes:
                    raise
            if attempt < policy.max_attempts:
                logger.warning("LLM request stage={} provider=openai-compatible model={} attempt={}/{} failed: {}", stage, self.model, attempt, policy.max_attempts, type(last_exc).__name__)
                logger.warning("Retrying in {}s", policy.retry_backoff_seconds)
                await asyncio.sleep(policy.retry_backoff_seconds)
            else:
                raise LLMRequestError(stage=stage, provider="openai-compatible", model=self.model, attempts=policy.max_attempts, error_type=type(last_exc).__name__, message=f"LLM request failed after {policy.max_attempts} attempts: {type(last_exc).__name__}") from last_exc
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("OpenAI-compatible response missing choices")
        return choices[0].get("message", {}).get("content", "")

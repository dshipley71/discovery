from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from webcam_discovery.config import settings


class LLMConfigurationError(RuntimeError):
    """Raised when a real LLM backend is not configured."""


class LLMRequestError(RuntimeError):
    def __init__(
        self,
        *,
        stage: str,
        provider: str,
        model: str,
        attempts: int,
        error_type: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.provider = provider
        self.model = model
        self.attempts = attempts
        self.error_type = error_type
        self.message = message


@dataclass(slots=True)
class LLMRequestPolicy:
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    pool_timeout: float
    max_attempts: int
    retry_backoff_seconds: float


def get_llm_request_policy(stage: str) -> LLMRequestPolicy:
    prefix = "planner" if stage.startswith("planner") else "scope" if stage.startswith("scope") else ""
    return LLMRequestPolicy(
        connect_timeout=settings.resolve_stage_float(prefix, "connect_timeout_seconds", settings.llm_connect_timeout_seconds),
        read_timeout=settings.resolve_stage_float(prefix, "read_timeout_seconds", settings.llm_read_timeout_seconds),
        write_timeout=settings.resolve_stage_float(prefix, "write_timeout_seconds", settings.llm_write_timeout_seconds),
        pool_timeout=settings.resolve_stage_float(prefix, "pool_timeout_seconds", settings.llm_pool_timeout_seconds),
        max_attempts=settings.resolve_stage_int(prefix, "max_attempts", settings.llm_max_attempts),
        retry_backoff_seconds=settings.resolve_stage_float(prefix, "retry_backoff_seconds", settings.llm_retry_backoff_seconds),
    )


class LLMBackend(ABC):
    @abstractmethod
    async def generate(
        self, prompt: str, *, system_prompt: str | None = None, stage: str = "other"
    ) -> str:
        raise NotImplementedError

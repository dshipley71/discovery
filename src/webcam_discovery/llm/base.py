from __future__ import annotations

from abc import ABC, abstractmethod


class LLMConfigurationError(RuntimeError):
    """Raised when a real LLM backend is not configured."""


class LLMBackend(ABC):
    @abstractmethod
    async def generate(self, prompt: str, *, system_prompt: str | None = None) -> str:
        raise NotImplementedError

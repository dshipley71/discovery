from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class MemoryBackend(ABC):
    @abstractmethod
    def search(self, query: str, limit: int = 5) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def write_run_summary(self, slug: str, markdown: str) -> Path:
        raise NotImplementedError

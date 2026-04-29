from __future__ import annotations

from webcam_discovery.config import settings
from webcam_discovery.memory.base import MemoryBackend
from webcam_discovery.memory.memweave_adapter import MemWeaveAdapter


def create_memory_backend() -> MemoryBackend | None:
    if not settings.memory_enabled:
        return None
    if settings.memory_backend != "memweave":
        raise RuntimeError(f"Unsupported memory backend: {settings.memory_backend}")
    return MemWeaveAdapter()

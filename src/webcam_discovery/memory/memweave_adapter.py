from __future__ import annotations

from datetime import date
from pathlib import Path

from webcam_discovery.config import settings
from webcam_discovery.memory.base import MemoryBackend


class MemWeaveAdapter(MemoryBackend):
    def __init__(self, workspace_dir: Path | None = None) -> None:
        try:
            __import__("memweave")
        except Exception as exc:
            raise RuntimeError(
                "MemWeave memory is enabled but memweave is not installed. Install with: pip install 'webcam-discovery[memory]'"
            ) from exc

        self.workspace_dir = workspace_dir or settings.memory_workspace_dir
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        (self.workspace_dir / "runs").mkdir(parents=True, exist_ok=True)

    def search(self, query: str, limit: int = 5) -> list[str]:
        # Minimal markdown sidecar search: real file-backed history.
        matches: list[str] = []
        q = query.casefold()
        for path in sorted((self.workspace_dir / "runs").glob("*.md"), reverse=True):
            text = path.read_text(encoding="utf-8")
            if q in text.casefold():
                matches.append(f"{path.name}: {text.splitlines()[0] if text else ''}")
            if len(matches) >= limit:
                break
        return matches

    def write_run_summary(self, slug: str, markdown: str) -> Path:
        safe_slug = slug.replace("/", "-").replace(" ", "-").lower()
        out = self.workspace_dir / "runs" / f"{date.today().isoformat()}-{safe_slug}.md"
        out.write_text(markdown, encoding="utf-8")
        return out

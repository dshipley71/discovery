#!/usr/bin/env python3
"""
config.py — Environment-aware settings for the webcam discovery pipeline.
All values can be overridden via environment variables prefixed WCD_.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Pipeline configuration loaded from environment / .env file."""

    # Catalog output — camera.geojson, cameras.md, and map.html must be co-located
    catalog_output_dir: Path = Path(".")

    # Runtime directories at project root (all git-ignored)
    log_dir:        Path = Path("logs")
    snapshot_dir:   Path = Path("snapshots")
    candidates_dir: Path = Path("candidates")

    # Pipeline behaviour
    max_concurrency: int   = 10
    request_timeout: float = 20.0
    min_legitimacy:  str   = "low"      # "high" | "medium" | "low"

    # Validation tuning
    validation_timeout_connect: float = 10.0   # seconds to open TCP connection
    validation_timeout_read:    float = 25.0   # seconds to receive first response bytes
    validation_concurrency:     int   = 50     # max simultaneous HTTP probe requests

    # Maintenance schedule
    maintenance_cadence_days: int = 7
    dead_after_n_failures:    int = 2
    prune_after_n_failures:   int = 4

    # LLM geocoding via Ollama — replaces Nominatim when enabled (default: True)
    use_llm_geodecode: bool = True
    ollama_api_key:    str  = ""                          # set via env or Colab secrets
    ollama_base_url:   str  = "https://ollama.com"        # Ollama cloud endpoint (api.ollama.com 301-redirects here)
    ollama_model:      str  = "gemma3:27b"                # model for geocoding

    # Browser-based stream URL discovery via Playwright (opt-in)
    # Many webcam sites load .m3u8 URLs via JavaScript fetch/XHR — static HTML probing
    # misses these.  Enable to run a headless-Chromium second pass on pages that the
    # static prober marks as dead/unknown.  Requires: playwright install chromium.
    use_browser_validation:          bool = False
    browser_validation_concurrency:  int  = 3    # simultaneous browser sessions (heavy)
    browser_validation_timeout:      int  = 15   # seconds to wait for stream URL per page

    # ffprobe/ffmpeg frame-level validation (opt-in, gracefully skipped if ffmpeg absent)
    # Runs ffprobe on confirmed-live HLS URLs to detect blank/frozen streams and
    # downgrades their status (active_blank → "unknown", disabled → "dead").
    # Requires: apt-get install -y ffmpeg  (or brew install ffmpeg on macOS)
    use_ffprobe_validation:          bool = True
    ffprobe_concurrency:             int  = 5    # simultaneous ffprobe subprocess calls

    model_config = {"env_file": ".env", "env_prefix": "WCD_"}

    # ── Hardcoded system constraints ──────────────────────────────────────────
    # HLS-only is a non-negotiable system constraint: only direct .m3u8 streams
    # that auto-play on click (no user interaction) are catalogued.  This is not
    # a user-facing setting and is intentionally not overridable via environment.
    @property
    def hls_only(self) -> bool:
        """Always True — this system catalogues HLS (.m3u8) streams exclusively."""
        return True

    def ensure_dirs(self) -> None:
        """Create all runtime directories if they do not exist."""
        for d in [self.log_dir, self.snapshot_dir, self.candidates_dir]:
            d.mkdir(parents=True, exist_ok=True)


# Module-level singleton — import this everywhere
settings = Settings()

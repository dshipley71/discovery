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
    geo_thread_workers:         int   = 20     # ThreadPoolExecutor size for geocoding

    # Maintenance schedule
    maintenance_cadence_days: int = 7
    dead_after_n_failures:    int = 2
    prune_after_n_failures:   int = 4

    model_config = {"env_file": ".env", "env_prefix": "WCD_"}

    def ensure_dirs(self) -> None:
        """Create all runtime directories if they do not exist."""
        for d in [self.log_dir, self.snapshot_dir, self.candidates_dir]:
            d.mkdir(parents=True, exist_ok=True)


# Module-level singleton — import this everywhere
settings = Settings()

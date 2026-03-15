#!/usr/bin/env python3
"""
pipeline.py — Full pipeline orchestrator for the webcam discovery system.
Runs all agents in execution order: discover → validate → catalog → (maintenance on schedule).
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations
import asyncio
import argparse
import sys
from pathlib import Path
from loguru import logger

from webcam_discovery.config import settings


def configure_logging() -> None:
    """
    Set up loguru handlers for pipeline runs.

    Terminal: INFO and above only (keeps progress bars clean).
    Log file: DEBUG and above (full detail for post-run inspection).
    Call once at the start of any pipeline or agent entry point.
    """
    logger.remove()  # remove the default DEBUG stderr handler
    logger.add(sys.stderr, level="INFO", colorize=True)
    logger.add(
        settings.log_dir / "pipeline.log",
        level="DEBUG",
        rotation="10 MB",
        retention="30 days",
    )


async def run_pipeline(tier: int = 1) -> None:
    """
    Run the full discovery pipeline end-to-end.

    Args:
        tier: Source tier to start discovery from (1 = highest priority).
    """
    settings.ensure_dirs()
    configure_logging()
    logger.info("Pipeline starting — tier={}", tier)

    # Step 1 + 2: Discovery
    logger.info("Step 1/4 — DirectoryAgent + SearchAgent")
    from webcam_discovery.agents.directory_crawler import DirectoryAgent
    from webcam_discovery.agents.search_agent import SearchAgent
    candidates = await DirectoryAgent().run(tier=tier)
    candidates += await SearchAgent().run(tier=tier)
    logger.info("Discovery complete — {} candidates", len(candidates))

    # Step 3: Validation
    logger.info("Step 2/4 — ValidationAgent")
    from webcam_discovery.agents.validator import ValidationAgent
    records = await ValidationAgent().run(candidates=candidates)
    logger.info("Validation complete — {} records", len(records))

    # Step 4: Catalog + export
    logger.info("Step 3/4 — CatalogAgent")
    from webcam_discovery.agents.catalog import CatalogAgent
    await CatalogAgent().run(
        records=records,
        output_dir=settings.catalog_output_dir,
        snapshot_dir=settings.snapshot_dir,
    )
    logger.info("Catalog exported — camera.geojson + cameras.md written to {}", settings.catalog_output_dir)
    logger.info("Pipeline complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full webcam discovery pipeline")
    parser.add_argument("--tier", type=int, default=1, help="Source tier to start from (default: 1)")
    args = parser.parse_args()
    asyncio.run(run_pipeline(tier=args.tier))


if __name__ == "__main__":
    main()

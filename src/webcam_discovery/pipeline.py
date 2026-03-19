#!/usr/bin/env python3
"""
pipeline.py — Full pipeline orchestrator for the webcam discovery system.

Execution model
---------------
Discovery and validation run as a producer-consumer pipeline so that cameras
found early are validated immediately rather than waiting for all discovery to
finish.

  DirectoryAgent.stream()  ─┐
                            ├─► asyncio.Queue ─► ValidationAgent.run_from_queue()
  SearchAgent.stream()     ─┘

Both discovery agents run concurrently (asyncio.gather) and push candidates to
a shared bounded queue.  ValidationAgent processes candidates in batches of
``VALIDATION_BATCH_SIZE`` as they arrive.  A single ``None`` sentinel (put after
both producers finish) signals end-of-stream to the consumer.

CatalogAgent runs only after all records are collected — deduplication requires
the full record set.

Part of the Public Webcam Discovery System.
"""
from __future__ import annotations
import asyncio
import argparse
import sys
from pathlib import Path
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate

# Maximum number of unprocessed candidates buffered between discovery and
# validation.  Backpressure kicks in when this is reached, throttling
# discovery automatically so memory usage stays bounded.
_QUEUE_MAXSIZE = 500

# Number of candidates to collect before triggering a validation pass.
# Larger batches are more efficient (fewer httpx client create/destroy cycles,
# better Nominatim pre-warm coverage); smaller batches reduce latency to first
# validated result.
_VALIDATION_BATCH_SIZE = 100


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


async def _run_discovery_to_queue(
    queue: asyncio.Queue,
    tier: int,
) -> None:
    """
    Run DirectoryAgent and SearchAgent concurrently, feeding all discovered
    candidates into *queue*.  Puts a single ``None`` sentinel when both
    producers have finished.

    Args:
        queue: Shared bounded queue consumed by ValidationAgent.
        tier:  Source / city tier to pass to both discovery agents.
    """
    from webcam_discovery.agents.directory_crawler import DirectoryAgent
    from webcam_discovery.agents.search_agent import SearchAgent

    dir_agent    = DirectoryAgent()
    search_agent = SearchAgent()

    async def produce_dir() -> None:
        count = 0
        async for candidate in dir_agent.stream(tier=tier):
            await queue.put(candidate)
            count += 1
        logger.info("_run_discovery_to_queue: DirectoryAgent done — {} candidates queued", count)

    async def produce_search() -> None:
        count = 0
        async for candidate in search_agent.stream(tier=tier):
            await queue.put(candidate)
            count += 1
        logger.info("_run_discovery_to_queue: SearchAgent done — {} candidates queued", count)

    # Run both discovery agents concurrently; neither depends on the other.
    await asyncio.gather(produce_dir(), produce_search())

    # Both producers are finished — send end-of-stream sentinel.
    await queue.put(None)
    logger.info("_run_discovery_to_queue: sentinel sent, discovery complete")


async def run_pipeline(tier: int = 1) -> None:
    """
    Run the full discovery pipeline end-to-end using a streaming
    producer-consumer architecture.

    Stage 1 — Discovery (parallel, streaming):
        DirectoryAgent.stream() and SearchAgent.stream() run concurrently.
        Candidates are pushed to a bounded asyncio.Queue as they are found.

    Stage 2 — Validation (overlapping with Stage 1):
        ValidationAgent.run_from_queue() processes candidates in batches of
        ``_VALIDATION_BATCH_SIZE`` as they arrive.  Validation begins as soon
        as the first batch fills, without waiting for discovery to finish.

    Stage 3 — Catalog (after all records collected):
        CatalogAgent.run() deduplicates, geo-exports, and renders map.html.

    Args:
        tier: Source tier to start discovery from (1 = highest priority).
    """
    settings.ensure_dirs()
    configure_logging()
    logger.info("Pipeline starting — tier={}, mode=streaming-parallel", tier)

    from webcam_discovery.agents.validator import ValidationAgent
    from webcam_discovery.agents.catalog import CatalogAgent

    # Bounded queue: backpressure keeps memory usage under control when
    # discovery is faster than validation.
    queue: asyncio.Queue[CameraCandidate | None] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

    validation_agent = ValidationAgent()

    logger.info(
        "Step 1+2 — Discovery (parallel) overlapping with Validation "
        "(batch_size={}, queue_maxsize={})",
        _VALIDATION_BATCH_SIZE,
        _QUEUE_MAXSIZE,
    )

    # Stage 1 and Stage 2 run concurrently:
    #   _run_discovery_to_queue feeds the queue; run_from_queue drains it.
    _, records = await asyncio.gather(
        _run_discovery_to_queue(queue, tier=tier),
        validation_agent.run_from_queue(queue, batch_size=_VALIDATION_BATCH_SIZE),
    )

    logger.info("Discovery + Validation complete — {} validated records", len(records))

    # Stage 3: catalog must run after all records are available.
    logger.info("Step 3/3 — CatalogAgent")
    await CatalogAgent().run(
        records=records,
        output_dir=settings.catalog_output_dir,
        snapshot_dir=settings.snapshot_dir,
    )
    logger.info(
        "Catalog exported — camera.geojson + cameras.md written to {}",
        settings.catalog_output_dir,
    )
    logger.info("Pipeline complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full webcam discovery pipeline")
    parser.add_argument("--tier", type=int, default=1, help="Source tier to start from (default: 1)")
    args = parser.parse_args()
    asyncio.run(run_pipeline(tier=args.tier))


if __name__ == "__main__":
    main()

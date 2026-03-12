#!/usr/bin/env python3
"""
directory_crawler.py — Traverses public webcam directories and extracts camera candidates.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path

from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.traversal import DirectoryTraversalSkill, TraversalInput
from webcam_discovery.skills.validation import RobotsPolicySkill, RobotsPolicyInput


# ── Source lists by tier ───────────────────────────────────────────────────────

TIER1_SOURCES: list[str] = [
    "https://www.webcamtaxi.com/en/",
    "https://www.skylinewebcams.com/",
    "https://www.earthcam.com/",
    "https://windy.com/webcams",
    "https://www.insecam.com/",  # only public/non-auth feeds
]

TIER2_SOURCES: list[str] = [
    "https://www.webcamgalore.com/",
    "https://www.worldcam.eu/",
    "https://camvista.com/",
    "https://www.airportwebcams.net/",
    "https://www.portwebcams.net/",
]

BLOCKED_SOURCES: set[str] = {
    "shodan.io",
    "insecam.org",
    "www.insecam.org",
}

_SOURCE_TIERS: dict[int, list[str]] = {
    1: TIER1_SOURCES,
    2: TIER1_SOURCES + TIER2_SOURCES,
}


def _domain_of(url: str) -> str:
    """Extract domain from URL."""
    from urllib.parse import urlparse
    return urlparse(url).netloc.lstrip("www.")


class DirectoryAgent:
    """Traverses public webcam directories and extracts camera candidates.

    Checks robots.txt before crawling each source.
    Runs traversal on allowed sources in batches of 3.
    Deduplicates results by URL.
    """

    BATCH_SIZE = 3

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """
        Traverse webcam directories for the given tier and return candidates.

        Args:
            tier: Source tier to crawl (1 = highest priority sources only).

        Returns:
            list[CameraCandidate] — deduplicated camera candidates.
        """
        sources = _SOURCE_TIERS.get(tier, TIER1_SOURCES)
        robots_skill = RobotsPolicySkill()
        traversal_skill = DirectoryTraversalSkill()

        # Check robots.txt for each source
        allowed_sources: list[str] = []
        for source_url in sources:
            domain = _domain_of(source_url)
            if domain in BLOCKED_SOURCES:
                logger.info("DirectoryAgent: skipping blocked source {}", domain)
                continue
            try:
                robots_result = await robots_skill.run(RobotsPolicyInput(domain=domain))
                if robots_result.allowed:
                    allowed_sources.append(source_url)
                    logger.debug("DirectoryAgent: robots.txt allows {}", domain)
                else:
                    logger.info("DirectoryAgent: robots.txt disallows {} — skipping", domain)
            except Exception as exc:
                logger.warning("DirectoryAgent: robots check error for {}: {}", domain, exc)
                allowed_sources.append(source_url)  # default allow on error

        logger.info("DirectoryAgent: {} of {} sources allowed by robots.txt", len(allowed_sources), len(sources))

        # Traverse in batches of 3
        all_candidates: list[CameraCandidate] = []
        for i in range(0, len(allowed_sources), self.BATCH_SIZE):
            batch = allowed_sources[i:i + self.BATCH_SIZE]
            tasks = [
                traversal_skill.run(TraversalInput(base_url=url, max_depth=2))
                for url in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result, source_url in zip(results, batch):
                if isinstance(result, Exception):
                    logger.warning("DirectoryAgent: traversal error for {}: {}", source_url, result)
                else:
                    all_candidates.extend(result.candidates)
                    logger.debug(
                        "DirectoryAgent: {} candidates from {}",
                        len(result.candidates), source_url,
                    )

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_candidates: list[CameraCandidate] = []
        for c in all_candidates:
            if c.url not in seen_urls:
                seen_urls.add(c.url)
                unique_candidates.append(c)

        logger.info(
            "DirectoryAgent: tier={} → {} unique candidates from {} sources",
            tier, len(unique_candidates), len(allowed_sources),
        )
        return unique_candidates


def main() -> None:
    """CLI entry point for directory crawler."""
    parser = argparse.ArgumentParser(description="Traverse webcam directories and extract candidates.")
    parser.add_argument("--tier", type=int, default=1, help="Source tier to crawl (default: 1)")
    parser.add_argument(
        "--output", type=Path,
        default=settings.candidates_dir / "candidates.jsonl",
        help="Output path for candidates.jsonl",
    )
    args = parser.parse_args()

    candidates = asyncio.run(DirectoryAgent().run(tier=args.tier))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(c.model_dump_json() for c in candidates))
    logger.info("DirectoryAgent: {} candidates → {}", len(candidates), args.output)


if __name__ == "__main__":
    main()

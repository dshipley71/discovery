#!/usr/bin/env python3
"""
directory_crawler.py — Traverses public webcam directories (Windy, webcamtaxi, etc.) and extracts camera candidates.
Part of the Public Webcam Discovery System.

Claude Code: implement this module following AGENTS.md and SKILLS.md.
Read those files before generating code for this agent.
"""
from __future__ import annotations
import asyncio
import argparse
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate, CameraRecord


class DirectoryAgent:
    """Traverses public webcam directories (Windy, webcamtaxi, etc.) and extracts camera candidates.

    Claude Code: implement run() following the spec in AGENTS.md → DirectoryAgent section.
    """

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """
        Traverses public webcam directories (Windy, webcamtaxi, etc.) and extracts camera candidates.

        Returns:
            list[CameraCandidate]
        """
        raise NotImplementedError(
            "Claude Code: implement DirectoryAgent.run() — see AGENTS.md and SKILLS.md"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Traverses public webcam directories (Windy, webcamtaxi, etc.) and extracts camera candidates.")
    parser.add_argument("--tier", type=int, default=1)
    args = parser.parse_args()
    asyncio.run(DirectoryAgent().run(**vars(args)))


if __name__ == "__main__":
    main()

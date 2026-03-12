#!/usr/bin/env python3
"""
validator.py — HTTP validation, feed classification, and legitimacy scoring.
Part of the Public Webcam Discovery System.

Claude Code: implement this module following AGENTS.md → ValidationAgent and
SKILLS.md → FeedValidationSkill, RobotsPolicySkill, FeedTypeClassificationSkill.
"""
from __future__ import annotations
import asyncio
import argparse
import json
from pathlib import Path
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate, CameraRecord


class ValidationAgent:
    """
    Validates CameraCandidate objects via HTTP HEAD/GET checks.
    Classifies feed types and assigns legitimacy scores.
    Rejects low-scoring records; flags medium records for review.

    Claude Code: implement run() following AGENTS.md → ValidationAgent.
    Key skills: FeedValidationSkill, RobotsPolicySkill, FeedTypeClassificationSkill.
    """

    async def run(
        self,
        candidates: list[CameraCandidate],
        input_file: Path | None = None,
    ) -> list[CameraRecord]:
        """
        Validate candidates and return verified CameraRecord objects.

        Args:
            candidates:  List of CameraCandidate objects from discovery agents.
            input_file:  Optional JSONL file path (used when running as CLI).

        Returns:
            list[CameraRecord] — validated, scored records ready for CatalogAgent.
        """
        raise NotImplementedError(
            "Claude Code: implement ValidationAgent.run() — see AGENTS.md and SKILLS.md"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate camera candidates")
    parser.add_argument("--input", type=Path, required=True,
                        help="Path to candidates.jsonl from discovery agents")
    parser.add_argument("--output", type=Path,
                        default=settings.candidates_dir / "validated.jsonl",
                        help="Output path for validated.jsonl")
    args = parser.parse_args()

    candidates = [
        CameraCandidate(**json.loads(line))
        for line in args.input.read_text().splitlines()
        if line.strip()
    ]
    records = asyncio.run(ValidationAgent().run(candidates=candidates))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(r.model_dump_json() for r in records))
    logger.info("Validated {} records → {}", len(records), args.output)


if __name__ == "__main__":
    main()

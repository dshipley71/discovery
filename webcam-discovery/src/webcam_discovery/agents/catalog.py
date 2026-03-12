#!/usr/bin/env python3
"""
catalog.py — Deduplication, geo-enrichment, and GeoJSON/Markdown export.
Part of the Public Webcam Discovery System.

Claude Code: implement this module following AGENTS.md → CatalogAgent and
SKILLS.md → DeduplicationSkill, GeoEnrichmentSkill, GeoJSONExportSkill.
"""
from __future__ import annotations
import asyncio
import argparse
import json
from pathlib import Path
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraRecord


class CatalogAgent:
    """
    Deduplicates, geo-enriches, and exports the validated camera list.
    Produces camera.geojson (primary output) and cameras.md (human-readable).

    Claude Code: implement run() following AGENTS.md → CatalogAgent.
    Key skills: DeduplicationSkill, GeoEnrichmentSkill, GeoJSONExportSkill.
    GeoJSON coordinate order is [longitude, latitude] per RFC 7946.
    """

    async def run(
        self,
        records: list[CameraRecord],
        output_dir: Path = Path("."),
        snapshot_dir: Path | None = None,
        input_file: Path | None = None,
    ) -> None:
        """
        Deduplicate, enrich, and export camera records.

        Args:
            records:      Validated CameraRecord list from ValidationAgent.
            output_dir:   Directory for camera.geojson + cameras.md (default: project root).
            snapshot_dir: Optional dated snapshot directory.
            input_file:   JSONL input path when running as CLI.
        """
        raise NotImplementedError(
            "Claude Code: implement CatalogAgent.run() — see AGENTS.md and SKILLS.md"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate and export camera catalog")
    parser.add_argument("--input", type=Path, required=True,
                        help="Path to validated.jsonl from ValidationAgent")
    parser.add_argument("--output", type=Path, default=Path("."),
                        help="Output directory for camera.geojson + cameras.md (default: .)")
    args = parser.parse_args()

    records = [
        CameraRecord(**json.loads(line))
        for line in args.input.read_text().splitlines()
        if line.strip()
    ]
    asyncio.run(CatalogAgent().run(
        records=records,
        output_dir=args.output,
        snapshot_dir=settings.snapshot_dir,
    ))


if __name__ == "__main__":
    main()

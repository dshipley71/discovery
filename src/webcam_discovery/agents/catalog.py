#!/usr/bin/env python3
"""
catalog.py — Deduplication, geo-enrichment, and GeoJSON/Markdown export.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraRecord
from webcam_discovery.skills.catalog import (
    DeduplicationSkill,
    DeduplicationInput,
    GeoJSONExportSkill,
    GeoJSONExportInput,
)
from webcam_discovery.agents.map_agent import MapAgent


class CatalogAgent:
    """
    Deduplicates, geo-enriches, and exports the validated camera list.
    Produces camera.geojson (primary output) and cameras.md (human-readable).
    GeoJSON coordinate order is [longitude, latitude] per RFC 7946.
    """

    async def run(
        self,
        records: list[CameraRecord],
        output_dir: Path = Path("."),
        snapshot_dir: Optional[Path] = None,
        input_file: Optional[Path] = None,
    ) -> None:
        """
        Deduplicate, enrich, and export camera records.

        Args:
            records:      Validated CameraRecord list from ValidationAgent.
            output_dir:   Directory for camera.geojson + cameras.md (default: project root).
            snapshot_dir: Optional dated snapshot directory.
            input_file:   JSONL input path when running as CLI.
        """
        # Load from file if provided and records is empty
        if input_file and not records:
            records = [
                CameraRecord(**json.loads(line))
                for line in input_file.read_text().splitlines()
                if line.strip()
            ]

        if not records:
            logger.warning("CatalogAgent: no records to catalog")
            return

        logger.info("CatalogAgent: processing {} records", len(records))

        # Step 1: Deduplicate
        dedup_skill = DeduplicationSkill()
        canonical_catalog: list[CameraRecord] = []

        for record in records:
            result = dedup_skill.run(DeduplicationInput(
                candidate_record=record,
                existing_catalog=canonical_catalog,
            ))
            if result.is_duplicate:
                # Replace canonical with merged version
                if result.merged_record and result.canonical_record:
                    idx = next(
                        (i for i, r in enumerate(canonical_catalog) if r.id == result.canonical_record.id),
                        None,
                    )
                    if idx is not None:
                        canonical_catalog[idx] = result.merged_record
                    logger.debug("CatalogAgent: merged duplicate '{}'", record.id)
            else:
                canonical_catalog.append(record)

        logger.info(
            "CatalogAgent: {} unique records after dedup (dropped {})",
            len(canonical_catalog), len(records) - len(canonical_catalog),
        )

        # Step 2: Export to camera.geojson
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        geojson_path = output_dir / "camera.geojson"

        export_skill = GeoJSONExportSkill()
        export_result = export_skill.run(GeoJSONExportInput(
            cameras=canonical_catalog,
            output_path=geojson_path,
        ))
        logger.info(
            "CatalogAgent: exported {} features to '{}' ({} skipped)",
            export_result.exported, geojson_path, export_result.skipped,
        )

        # Step 3: Write cameras.md
        md_path = output_dir / "cameras.md"
        self._write_markdown(canonical_catalog, md_path)
        logger.info("CatalogAgent: cameras.md written to '{}'", md_path)

        # Step 4: Write dated snapshot if snapshot_dir provided
        if snapshot_dir:
            snapshot_dir = Path(snapshot_dir)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            today = date.today().isoformat()
            snapshot_path = snapshot_dir / f"camera_{today}.geojson"
            if not snapshot_path.exists():
                import shutil
                shutil.copy2(geojson_path, snapshot_path)
                logger.info("CatalogAgent: snapshot written to '{}'", snapshot_path)

        # Step 5: Regenerate map.html
        map_path = MapAgent(output_dir=output_dir).run()
        logger.info(
            "CatalogAgent: map.html written to '{}' ({} cameras)",
            map_path,
            export_result.exported,
        )

    def _write_markdown(self, records: list[CameraRecord], path: Path) -> None:
        """Write cameras.md grouped by continent → country → city."""
        # Group records
        groups: dict[str, dict[str, dict[str, list[CameraRecord]]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(list))
        )
        for record in records:
            groups[record.continent][record.country][record.city].append(record)

        lines: list[str] = [
            "# Public Webcam Catalog\n",
            f"Generated: {date.today().isoformat()}\n",
            f"Total cameras: {len(records)}\n",
            "",
        ]

        for continent in sorted(groups):
            lines.append(f"## {continent}\n")
            for country in sorted(groups[continent]):
                lines.append(f"### {country}\n")
                for city in sorted(groups[continent][country]):
                    lines.append(f"#### {city}\n")
                    for record in sorted(groups[continent][country][city], key=lambda r: r.label):
                        status_icon = {"live": "✅", "dead": "❌", "unknown": "⚠️"}.get(record.status, "⚠️")
                        label_link = f"[{record.label}]({record.url})"
                        feed_info = f"`{record.feed_type}`"
                        lines.append(f"- {status_icon} {label_link} — {feed_info}")
                        if record.notes:
                            lines.append(f"  - *{record.notes}*")
                    lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    """CLI entry point for catalog agent."""
    from webcam_discovery.pipeline import configure_logging
    configure_logging()
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

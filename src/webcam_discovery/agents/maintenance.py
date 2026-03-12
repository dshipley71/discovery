#!/usr/bin/env python3
"""
maintenance.py — Scheduled HEAD checks, status updates, and dead-link pruning.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraRecord
from webcam_discovery.skills.maintenance import HealthCheckSkill, HealthCheckInput
from webcam_discovery.skills.catalog import GeoJSONExportSkill, GeoJSONExportInput


def _load_geojson(catalog_path: Path) -> list[CameraRecord]:
    """Load CameraRecord objects from a camera.geojson file."""
    if not catalog_path.exists():
        logger.error("MaintenanceAgent: catalog not found at '{}'", catalog_path)
        return []

    with open(catalog_path, encoding="utf-8") as f:
        data = json.load(f)

    records: list[CameraRecord] = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [None, None])
        try:
            record = CameraRecord(
                **{
                    **props,
                    "longitude": coords[0],
                    "latitude": coords[1],
                }
            )
            records.append(record)
        except Exception as exc:
            logger.warning("MaintenanceAgent: skipping invalid record '{}': {}", props.get("id", "?"), exc)

    return records


def _load_failure_counts(log_path: Path) -> dict[str, int]:
    """Load consecutive failure counts from maintenance_log.jsonl."""
    failure_counts: dict[str, int] = {}
    if not log_path.exists():
        return failure_counts

    for line in log_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            cam_id = entry.get("id")
            if cam_id:
                if entry.get("event") == "status_change" and entry.get("new_status") == "dead":
                    failure_counts[cam_id] = failure_counts.get(cam_id, 0) + 1
                elif entry.get("event") == "status_change" and entry.get("new_status") in ("live", "unknown"):
                    failure_counts[cam_id] = 0
        except json.JSONDecodeError:
            continue

    return failure_counts


class MaintenanceAgent:
    """
    Performs weekly HEAD checks on all cameras in camera.geojson.
    Updates status and last_verified in place.
    Writes a timestamped audit trail to logs/maintenance_log.jsonl.
    Prunes records dead for prune_after_n_failures consecutive checks.
    """

    async def run(
        self,
        catalog: Path = Path("camera.geojson"),
        log_path: Optional[Path] = None,
    ) -> None:
        """
        Run HEAD checks and update camera.geojson in place.

        Args:
            catalog:  Path to camera.geojson (default: project root).
            log_path: Path for maintenance_log.jsonl (default: logs/).
        """
        catalog = Path(catalog)
        if log_path is None:
            log_path = settings.log_dir / "maintenance_log.jsonl"

        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Load records
        records = _load_geojson(catalog)
        if not records:
            logger.warning("MaintenanceAgent: no records to check")
            return

        logger.info("MaintenanceAgent: checking {} records from '{}'", len(records), catalog)

        # Load failure counts from previous runs
        failure_counts = _load_failure_counts(log_path)

        # Run health checks
        health_skill = HealthCheckSkill()
        summary = await health_skill.run(HealthCheckInput(
            records=records,
            concurrency=settings.max_concurrency,
        ))

        today = date.today().isoformat()
        now = datetime.now(timezone.utc).isoformat()

        # Build result map
        result_map = {r.id: r for r in summary.results}

        # Update records in place and track changes
        updated_records: list[CameraRecord] = []
        pruned_ids: list[str] = []
        log_entries: list[dict] = []

        for record in records:
            check = result_map.get(record.id)
            if check is None:
                updated_records.append(record)
                continue

            old_status = record.status
            new_status = check.new_status

            # Update failure count
            if new_status == "dead":
                failure_counts[record.id] = failure_counts.get(record.id, 0) + 1
            elif new_status == "live":
                failure_counts[record.id] = 0
            # unknown: don't increment, don't reset

            consecutive_failures = failure_counts.get(record.id, 0)

            # Prune after N consecutive dead results
            if consecutive_failures >= settings.prune_after_n_failures:
                logger.info(
                    "MaintenanceAgent: pruning '{}' — {} consecutive failures",
                    record.id, consecutive_failures,
                )
                pruned_ids.append(record.id)
                log_entries.append({
                    "timestamp": now,
                    "event": "pruned",
                    "id": record.id,
                    "label": record.label,
                    "url": record.url,
                    "consecutive_failures": consecutive_failures,
                })
                continue

            # Update record
            updated_record = record.model_copy(update={
                "status": new_status,
                "last_verified": today,
            })
            updated_records.append(updated_record)

            # Log status changes
            if old_status != new_status:
                log_entries.append({
                    "timestamp": now,
                    "event": "status_change",
                    "id": record.id,
                    "label": record.label,
                    "url": record.url,
                    "old_status": old_status,
                    "new_status": new_status,
                    "status_code": check.status_code,
                    "fail_reason": check.fail_reason,
                    "consecutive_failures": consecutive_failures,
                })
                logger.info(
                    "MaintenanceAgent: '{}' status {} → {}",
                    record.id, old_status, new_status,
                )

        # Append log entries
        with open(log_path, "a", encoding="utf-8") as f:
            for entry in log_entries:
                f.write(json.dumps(entry, default=str) + "\n")

        # Write updated camera.geojson
        export_skill = GeoJSONExportSkill()
        export_skill.run(GeoJSONExportInput(
            cameras=updated_records,
            output_path=catalog,
        ))

        logger.info(
            "MaintenanceAgent: done — checked={} live={} dead={} unknown={} pruned={}",
            summary.total_checked,
            summary.live_count,
            summary.dead_count,
            summary.unknown_count,
            len(pruned_ids),
        )


def main() -> None:
    """CLI entry point for maintenance agent."""
    parser = argparse.ArgumentParser(description="Run maintenance checks on camera catalog")
    parser.add_argument(
        "--catalog", type=Path, default=Path("camera.geojson"),
        help="Path to camera.geojson (default: camera.geojson)",
    )
    parser.add_argument(
        "--log", type=Path, default=None,
        help="Path for maintenance_log.jsonl",
    )
    args = parser.parse_args()
    asyncio.run(MaintenanceAgent().run(catalog=args.catalog, log_path=args.log))


if __name__ == "__main__":
    main()

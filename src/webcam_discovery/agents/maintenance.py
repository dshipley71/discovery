#!/usr/bin/env python3
"""
maintenance.py — Scheduled status checks and validation review reporting.
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
                if entry.get("event") in {"status_change", "health_check"}:
                    if entry.get("new_status") == "dead":
                        failure_counts[cam_id] = int(entry.get("consecutive_failures") or 0)
                    elif entry.get("new_status") in ("live", "unknown"):
                        failure_counts[cam_id] = 0
        except json.JSONDecodeError:
            continue

    return failure_counts


class MaintenanceAgent:
    """
    Performs weekly status checks on all cameras in camera.geojson.
    Updates status and last_verified in place.
    Writes a timestamped audit trail to logs/maintenance_log.jsonl.
    It does NOT prune links automatically; dead records are retained and
    written to a validation review report so they can be re-run through the
    validation workflow before any manual removal.
    """

    async def run(
        self,
        catalog: Path = Path("camera.geojson"),
        log_path: Optional[Path] = None,
    ) -> None:
        """
        Run status checks and update camera.geojson in place.

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
            concurrency=settings.validation_concurrency,
        ))

        today = date.today().isoformat()
        now = datetime.now(timezone.utc).isoformat()

        # Build result map
        result_map = {r.id: r for r in summary.results}

        review_report_path = log_path.with_name("pending_validation_review.jsonl")

        # Update records in place and track changes
        updated_records: list[CameraRecord] = []
        log_entries: list[dict] = []
        review_entries: list[dict] = []

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

            log_entries.append({
                "timestamp": now,
                "event": "health_check",
                "id": record.id,
                "label": record.label,
                "url": record.url,
                "new_status": new_status,
                "status_code": check.status_code,
                "fail_reason": check.fail_reason,
                "consecutive_failures": consecutive_failures,
            })

            # Dead records stay in the catalog until a human/operator runs them
            # back through the validation workflow and explicitly decides to
            # remove them.
            if consecutive_failures >= settings.prune_after_n_failures:
                review_entry = {
                    "timestamp": now,
                    "event": "pending_validation_review",
                    "id": record.id,
                    "label": record.label,
                    "url": record.url,
                    "status": new_status,
                    "consecutive_failures": consecutive_failures,
                    "fail_reason": check.fail_reason,
                }
                review_entries.append(review_entry)
                log_entries.append(review_entry)

                logger.info(
                    "MaintenanceAgent: '{}' reached {} consecutive failures — retained "
                    "for validation review",
                    record.id, consecutive_failures,
                )
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

        if review_entries:
            with open(review_report_path, "a", encoding="utf-8") as f:
                for entry in review_entries:
                    f.write(json.dumps(entry, default=str) + "\n")
            logger.info(
                "MaintenanceAgent: wrote {} validation-review entries to '{}'",
                len(review_entries),
                review_report_path,
            )

        # Write updated camera.geojson
        export_skill = GeoJSONExportSkill()
        export_skill.run(GeoJSONExportInput(
            cameras=updated_records,
            output_path=catalog,
        ))

        logger.info(
            "MaintenanceAgent: done — checked={} live={} dead={} unknown={} "
            "pending_validation_review={}",
            summary.total_checked,
            summary.live_count,
            summary.dead_count,
            summary.unknown_count,
            len(review_entries),
        )


def main() -> None:
    """CLI entry point for maintenance agent."""
    from webcam_discovery.pipeline import configure_logging
    configure_logging()
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

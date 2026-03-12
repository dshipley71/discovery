#!/usr/bin/env python3
"""
maintenance.py — Liveness HEAD checks, status updates, and dead-link detection.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel

from webcam_discovery.schemas import CameraRecord, CameraStatus


# ── I/O Models ────────────────────────────────────────────────────────────────

class HealthCheckInput(BaseModel):
    """Input for health check skill."""

    records: list[CameraRecord]
    concurrency: int = 10


class HealthCheckResult(BaseModel):
    """Result for a single camera health check."""

    id: str
    url: str
    new_status: CameraStatus
    status_code: Optional[int] = None
    fail_reason: Optional[str] = None


class HealthCheckSummary(BaseModel):
    """Summary of a batch health check run."""

    total_checked: int
    live_count: int
    dead_count: int
    unknown_count: int
    newly_dead: list[str]
    results: list[HealthCheckResult]


# ── HealthCheckSkill ───────────────────────────────────────────────────────────

class HealthCheckSkill:
    """Batch liveness check for existing catalog records."""

    _YOUTUBE_OEMBED = "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"

    async def run(self, input: HealthCheckInput) -> HealthCheckSummary:
        """
        Perform batch HEAD checks with controlled concurrency.

        YouTube feeds are verified via the oEmbed endpoint.
        Maps status: 200-206 → live, 301/302 → redirected (unknown), 401/403/407 → dead, else → dead.
        Timeout → unknown.

        Args:
            input: HealthCheckInput with records and concurrency limit.

        Returns:
            HealthCheckSummary with per-record results and aggregate counts.
        """
        semaphore = asyncio.Semaphore(input.concurrency)
        today = date.today().isoformat()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            headers={"User-Agent": "WebcamDiscoveryBot/1.0"},
        ) as client:
            tasks = [
                self._check_record(client, record, semaphore, today)
                for record in input.records
            ]
            results: list[HealthCheckResult] = await asyncio.gather(*tasks)

        live_count = sum(1 for r in results if r.new_status == "live")
        dead_count = sum(1 for r in results if r.new_status == "dead")
        unknown_count = sum(1 for r in results if r.new_status == "unknown")

        newly_dead = [
            r.id for r, record in zip(results, input.records)
            if r.new_status == "dead" and record.status != "dead"
        ]

        logger.info(
            "HealthCheckSkill: total={} live={} dead={} unknown={} newly_dead={}",
            len(results), live_count, dead_count, unknown_count, len(newly_dead),
        )

        return HealthCheckSummary(
            total_checked=len(results),
            live_count=live_count,
            dead_count=dead_count,
            unknown_count=unknown_count,
            newly_dead=newly_dead,
            results=results,
        )

    async def _check_record(
        self,
        client: httpx.AsyncClient,
        record: CameraRecord,
        semaphore: asyncio.Semaphore,
        today: str,
    ) -> HealthCheckResult:
        """Check a single record and return its health result."""
        async with semaphore:
            return await self._perform_check(client, record, today)

    async def _perform_check(
        self,
        client: httpx.AsyncClient,
        record: CameraRecord,
        today: str,
    ) -> HealthCheckResult:
        """Perform the actual HTTP check for one record."""
        # YouTube live: verify via oEmbed
        if record.feed_type == "youtube_live":
            return await self._check_youtube(client, record)

        # Primary: stream_url, fallback: url
        check_url = record.stream_url or record.url

        try:
            response = await client.head(check_url)
            status_code = response.status_code

            if 200 <= status_code <= 206:
                new_status: CameraStatus = "live"
                fail_reason = None
            elif status_code in (301, 302):
                new_status = "unknown"
                fail_reason = f"redirect_{status_code}"
            elif status_code in (401, 403, 407):
                new_status = "dead"
                fail_reason = f"auth_{status_code}"
            else:
                new_status = "dead"
                fail_reason = f"http_{status_code}"

            logger.debug("HealthCheck {}: {} → {}", record.id, status_code, new_status)
            return HealthCheckResult(
                id=record.id,
                url=check_url,
                new_status=new_status,
                status_code=status_code,
                fail_reason=fail_reason,
            )

        except httpx.TimeoutException:
            logger.warning("HealthCheck timeout: {} ({})", record.id, check_url)
            return HealthCheckResult(
                id=record.id,
                url=check_url,
                new_status="unknown",
                fail_reason="timeout",
            )
        except Exception as exc:
            logger.warning("HealthCheck error on {}: {}", record.id, exc)
            return HealthCheckResult(
                id=record.id,
                url=check_url,
                new_status="unknown",
                fail_reason=str(exc)[:120],
            )

    async def _check_youtube(
        self,
        client: httpx.AsyncClient,
        record: CameraRecord,
    ) -> HealthCheckResult:
        """Verify YouTube feed via oEmbed endpoint."""
        video_id = record.video_id
        if not video_id:
            # Try to extract from stream_url
            if record.stream_url:
                import re
                match = re.search(r"/embed/([A-Za-z0-9_-]+)", record.stream_url)
                if match:
                    video_id = match.group(1)

        if not video_id:
            # Fall back to URL check
            check_url = record.stream_url or record.url
            return await self._perform_check(client, record, "")

        oembed_url = self._YOUTUBE_OEMBED.format(video_id=video_id)
        try:
            response = await client.get(oembed_url)
            if response.status_code == 200:
                new_status: CameraStatus = "live"
                fail_reason = None
            elif response.status_code == 404:
                new_status = "dead"
                fail_reason = "youtube_removed"
            else:
                new_status = "unknown"
                fail_reason = f"oembed_http_{response.status_code}"

            return HealthCheckResult(
                id=record.id,
                url=oembed_url,
                new_status=new_status,
                status_code=response.status_code,
                fail_reason=fail_reason,
            )
        except httpx.TimeoutException:
            return HealthCheckResult(
                id=record.id,
                url=oembed_url,
                new_status="unknown",
                fail_reason="timeout",
            )
        except Exception as exc:
            return HealthCheckResult(
                id=record.id,
                url=oembed_url,
                new_status="unknown",
                fail_reason=str(exc)[:120],
            )


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from webcam_discovery.schemas import CameraRecord

    async def _main() -> None:
        record = CameraRecord(
            id="test-london-tower",
            label="Tower Bridge",
            city="London",
            country="United Kingdom",
            continent="Europe",
            latitude=51.5055,
            longitude=-0.0754,
            url="https://www.earthcam.com/world/england/london/towerbridge/",
            feed_type="youtube_live",
            status="live",
            video_id="dQw4w9WgXcQ",
        )
        skill = HealthCheckSkill()
        summary = await skill.run(HealthCheckInput(records=[record]))
        logger.info("{}", summary.model_dump())

    asyncio.run(_main())

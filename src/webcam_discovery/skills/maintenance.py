#!/usr/bin/env python3
"""
maintenance.py — Liveness HEAD checks, status updates, and dead-link detection.
Part of the Public Webcam Discovery System.

Health-check strategy
---------------------
HLS (.m3u8) streams
    ffprobe is used as the primary liveness check.  It opens the playlist,
    decodes a short sample of frames, and classifies the stream as:

      active_streaming → CameraStatus "live"    (frames decoded, real content)
      active_blank     → CameraStatus "unknown" (playlist valid, blank/frozen)
      disabled         → CameraStatus "dead"    (playlist unreachable at frame level)
      does_not_exist   → CameraStatus "dead"    (DNS/404/connection failure)

    If the ffprobe binary is absent (graceful degradation) the check falls back
    to an HTTP HEAD request with the same 200-206/redirect/auth status mapping
    used for non-HLS URLs.

Non-HLS URLs (should not appear when hls_only=True)
    HTTP HEAD — 200-206 → live, 301/302 → unknown, 401/403 → dead, else → dead.

YouTube live feeds
    Verified via the oEmbed endpoint.
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
    """
    Batch liveness check for existing catalog records.

    HLS (.m3u8) streams are checked via ffprobe for accurate frame-level status.
    Non-HLS URLs fall back to HTTP HEAD.  Concurrency is bounded by a semaphore.
    """

    _YOUTUBE_OEMBED = "https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"

    async def run(self, input: HealthCheckInput) -> HealthCheckSummary:
        """
        Perform batch health checks with controlled concurrency.

        HLS (.m3u8) records are verified with ffprobe (frame-level analysis).
        Non-HLS records fall back to HTTP HEAD.  YouTube feeds use oEmbed.

        Status mapping:
          ffprobe active_streaming → "live"
          ffprobe active_blank     → "unknown"
          ffprobe disabled/missing → "dead"
          HTTP 200-206             → "live"
          HTTP 301/302             → "unknown"
          HTTP 401/403/407         → "dead"
          HTTP other / timeout     → "unknown" or "dead"

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

        live_count    = sum(1 for r in results if r.new_status == "live")
        dead_count    = sum(1 for r in results if r.new_status == "dead")
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
        """
        Dispatch to the appropriate check strategy for one record.

        HLS (.m3u8) → ffprobe frame analysis (with HTTP HEAD fallback)
        YouTube live  → oEmbed endpoint verification
        Other         → HTTP HEAD
        """
        # YouTube live: verify via oEmbed
        if record.feed_type == "youtube_live":
            return await self._check_youtube(client, record)

        check_url = record.url

        # For HLS streams, use ffprobe for accurate frame-level status detection.
        # An "active" stream is one where clicking the URL plays video immediately
        # with no user interaction — confirmed by actual decoded frames.
        if ".m3u8" in check_url.lower():
            return await self._check_hls_ffprobe(record, check_url)

        # Non-HLS: HTTP HEAD fallback
        return await self._head_check(client, record, check_url)

    async def _check_hls_ffprobe(
        self,
        record: CameraRecord,
        check_url: str,
    ) -> HealthCheckResult:
        """
        Check an HLS (.m3u8) stream using ffprobe for frame-level status.

        Classifies the stream as active/unknown/dead based on decoded frames:
          active_streaming → "live"    (real video content present)
          active_blank     → "unknown" (valid playlist, blank/frozen frames)
          disabled         → "dead"    (playlist unreachable at segment level)
          does_not_exist   → "dead"    (DNS failure, 404, connection refused)

        Falls back to HTTP HEAD if ffprobe is not installed.
        """
        from webcam_discovery.skills.ffprobe_validation import FfprobeValidationSkill

        skill = FfprobeValidationSkill(concurrency=1)
        try:
            results = await skill.run([check_url])
        except Exception as exc:
            logger.warning("HealthCheckSkill: ffprobe error for {}: {}", record.id, exc)
            results = []

        if not results:
            return HealthCheckResult(
                id=record.id,
                url=check_url,
                new_status="unknown",
                fail_reason="ffprobe_no_result",
            )

        fp = results[0]

        # ffprobe binary not found — fall back to HTTP HEAD
        if not fp.ffprobe_available:
            logger.debug(
                "HealthCheckSkill: ffprobe unavailable for {} — falling back to HEAD",
                record.id,
            )
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=False,
                headers={"User-Agent": "WebcamDiscoveryBot/1.0"},
            ) as fallback_client:
                return await self._head_check(fallback_client, record, check_url)

        # Map FfprobeResult → CameraStatus
        # fp.camera_status returns "live" | "unknown" | "dead" | None
        raw_status = fp.camera_status
        if raw_status is None:
            # stream_status=None means ffprobe skipped this URL (e.g. non-HLS
            # slipped through) — treat as unknown rather than changing status.
            new_status: CameraStatus = "unknown"
            fail_reason = fp.detail or "ffprobe_skip"
        else:
            new_status = raw_status  # type: ignore[assignment]
            fail_reason = fp.detail if new_status != "live" else None

        logger.debug(
            "HealthCheckSkill ffprobe {}: stream_status={} → {}",
            record.id, fp.stream_status, new_status,
        )
        return HealthCheckResult(
            id=record.id,
            url=check_url,
            new_status=new_status,
            fail_reason=fail_reason,
        )

    async def _head_check(
        self,
        client: httpx.AsyncClient,
        record: CameraRecord,
        check_url: str,
    ) -> HealthCheckResult:
        """
        Perform an HTTP HEAD check and map the response status to CameraStatus.

        200-206 → live, 301/302 → unknown (redirect), 401/403/407 → dead,
        other 4xx/5xx → dead, timeout → unknown.
        """
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

            logger.debug("HealthCheck HEAD {}: {} → {}", record.id, status_code, new_status)
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
        import re

        # Try to extract video_id from the record URL
        video_id: Optional[str] = None
        match = re.search(r"/embed/([A-Za-z0-9_-]+)", record.url)
        if match:
            video_id = match.group(1)

        if not video_id:
            # Fall back to HEAD check on the record URL
            return await self._head_check(client, record, record.url)

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
    import asyncio

    async def _main() -> None:
        record = CameraRecord(
            id="test-hls-stream",
            label="Test HLS Stream",
            city="Test City",
            country="Test Country",
            continent="Test Continent",
            url="https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8",
            feed_type="HLS_stream",
            status="unknown",
        )
        skill = HealthCheckSkill()
        summary = await skill.run(HealthCheckInput(records=[record]))
        logger.info("{}", summary.model_dump())

    asyncio.run(_main())

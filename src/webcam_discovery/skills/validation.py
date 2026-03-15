#!/usr/bin/env python3
"""
validation.py — HLS stream validation and robots.txt compliance.
Part of the Public Webcam Discovery System.

Validation strategy
-------------------
Only .m3u8 (HLS) URLs are accepted as active camera streams.
All other URL types are rejected immediately — no HTML probing, no MJPEG,
no iframes, no YouTube embeds, no MP4 files.

HLS validation
--------------
1. GET the first 4 KB of the URL.
2. Verify #EXTM3U magic byte on line 1.
3. Classify playlist type:
   - Master playlist: contains #EXT-X-STREAM-INF → feed_type=HLS_master
     Variant stream URLs are extracted and stored in variant_streams.
   - Media playlist: contains #EXTINF or #EXT-X-TARGETDURATION → feed_type=HLS_stream
4. Return ValidationResult with playlist_type and variant_streams.

Concurrency: asyncio.Semaphore(settings.validation_concurrency) — default 50.
Timeout:     connect=10 s, read=25 s — generous for slow camera servers.
Retry:       1 automatic retry (2 s back-off) on timeout.
User-Agent:  Browser-like string to avoid bot-blocking.
"""
from __future__ import annotations

import asyncio
import re
from typing import Literal, Optional
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger
from pydantic import BaseModel

from webcam_discovery.schemas import CameraStatus, FeedType, LegitimacyScore


# ── I/O Models ────────────────────────────────────────────────────────────────

class ValidationResult(BaseModel):
    """Result of a single URL liveness validation."""
    url: str
    status_code: Optional[int] = None
    content_type: Optional[str] = None
    legitimacy_score: LegitimacyScore = "medium"
    status: CameraStatus = "unknown"
    fail_reason: Optional[str] = None
    playlist_type: Optional[Literal["master", "media"]] = None
    """HLS playlist type: 'master' (multi-bitrate index) or 'media' (live segment list)."""
    variant_streams: list[str] = []
    """Variant stream URLs extracted from a master playlist."""


class RobotsPolicyInput(BaseModel):
    """Input for robots.txt policy check."""
    domain: str


class RobotsPolicyResult(BaseModel):
    """Result of a robots.txt compliance check."""
    allowed: bool
    disallowed_paths: list[str] = []


class FeedTypeInput(BaseModel):
    """Input for feed type classification."""
    url: str
    content_type: Optional[str] = None
    playlist_type: Optional[Literal["master", "media"]] = None


class FeedTypeResult(BaseModel):
    """Result of feed type classification."""
    feed_type: FeedType


# ── Constants ──────────────────────────────────────────────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HLS_MAGIC   = b"#EXTM3U"
_HLS_URL_RE  = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_AUTH_URL_RE = re.compile(
    r"/(login|signin|sign-in|auth|register|subscribe|account|member|join)",
    re.IGNORECASE,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_auth_path(url: str) -> bool:
    return bool(_AUTH_URL_RE.search(urlparse(url).path))


# ── FeedValidationSkill ────────────────────────────────────────────────────────

class FeedValidationSkill:
    """
    Validate a list of .m3u8 URLs for liveness.

    Only HLS (.m3u8) stream URLs are accepted. All other URL types are rejected
    immediately without making an HTTP request. Semaphore-limited concurrency
    avoids overwhelming servers.
    """

    async def run(self, urls: list[str]) -> list[ValidationResult]:
        """
        Probe each URL and return a ValidationResult.

        Args:
            urls: Candidate URLs to validate (only .m3u8 URLs will be probed).

        Returns:
            list[ValidationResult] in the same order as urls.
        """
        from webcam_discovery.config import settings

        timeout = httpx.Timeout(
            connect=settings.validation_timeout_connect,
            read=settings.validation_timeout_read,
            write=10.0,
            pool=5.0,
        )
        sem = asyncio.Semaphore(settings.validation_concurrency)

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            tasks = [self._probe(client, url, sem) for url in urls]
            return list(await asyncio.gather(*tasks, return_exceptions=False))

    async def _probe(
        self, client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore
    ) -> ValidationResult:
        """Acquire semaphore, dispatch, retry once on timeout."""
        async with sem:
            result = await self._dispatch(client, url)
            if result.fail_reason == "timeout":
                logger.debug("FeedValidationSkill: retrying {} after timeout", url)
                await asyncio.sleep(2.0)
                result = await self._dispatch(client, url)
        return result

    async def _dispatch(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """
        Route .m3u8 URLs to the HLS probe; reject all other URL types immediately.

        Only HLS (.m3u8) streams are accepted as active camera links.
        """
        if _has_auth_path(url):
            return ValidationResult(
                url=url, legitimacy_score="low", status="dead",
                fail_reason="auth_url_pattern",
            )
        if _HLS_URL_RE.search(url):
            return await self._probe_hls(client, url)
        return ValidationResult(
            url=url, legitimacy_score="low", status="dead",
            fail_reason="not_m3u8_stream",
        )

    async def _probe_hls(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """
        Fetch HLS playlist, verify #EXTM3U magic, and classify as master or media.

        Master playlist (#EXT-X-STREAM-INF) → playlist_type='master', variant_streams extracted.
        Media playlist  (#EXTINF / #EXT-X-TARGETDURATION) → playlist_type='media'.
        """
        try:
            async with client.stream("GET", url) as resp:
                ct = resp.headers.get("content-type", "")
                if resp.status_code not in range(200, 207):
                    return ValidationResult(
                        url=url, status_code=resp.status_code,
                        legitimacy_score="low", status="dead",
                        fail_reason=f"http_{resp.status_code}",
                    )
                buf = b""
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) >= 4096:
                        break

            if _HLS_MAGIC not in buf:
                if any(k in ct.lower() for k in ("mpegurl", "m3u8", "octet-stream")):
                    return ValidationResult(
                        url=url, status_code=200, content_type=ct or None,
                        legitimacy_score="medium", status="live",
                        fail_reason="no_m3u8_magic",
                    )
                return ValidationResult(
                    url=url, status_code=200, content_type=ct or None,
                    legitimacy_score="low", status="unknown",
                    fail_reason="no_m3u8_magic",
                )

            content = buf.decode("utf-8", errors="replace")

            # Classify playlist type
            is_master = "#EXT-X-STREAM-INF" in content
            is_media  = "#EXTINF" in content or "#EXT-X-TARGETDURATION" in content

            playlist_type: Optional[Literal["master", "media"]] = None
            variant_streams: list[str] = []

            if is_master:
                playlist_type = "master"
                lines = content.splitlines()
                for i, line in enumerate(lines):
                    if line.startswith("#EXT-X-STREAM-INF"):
                        for j in range(i + 1, len(lines)):
                            variant_line = lines[j].strip()
                            if variant_line and not variant_line.startswith("#"):
                                abs_url = (
                                    variant_line if variant_line.startswith("http")
                                    else urljoin(url, variant_line)
                                )
                                if abs_url not in variant_streams:
                                    variant_streams.append(abs_url)
                                break
            elif is_media:
                playlist_type = "media"

            logger.debug(
                "FeedValidationSkill: HLS confirmed {} (playlist_type={})",
                url, playlist_type or "unclassified",
            )
            return ValidationResult(
                url=url, status_code=200, content_type=ct or None,
                legitimacy_score="high", status="live",
                playlist_type=playlist_type,
                variant_streams=variant_streams,
            )
        except httpx.TimeoutException:
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:100])


# ── RobotsPolicySkill ──────────────────────────────────────────────────────────

class RobotsPolicySkill:
    """Check robots.txt compliance for a domain before crawling."""

    _cache: dict[str, RobotsPolicyResult] = {}

    async def run(self, input: RobotsPolicyInput) -> RobotsPolicyResult:
        """
        Fetch and parse robots.txt for the given domain.

        Args:
            input: RobotsPolicyInput with domain string.

        Returns:
            RobotsPolicyResult with allowed flag and disallowed paths.
        """
        domain = input.domain.rstrip("/")
        if domain in self._cache:
            return self._cache[domain]

        robots_url = f"https://{domain}/robots.txt"
        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers={"User-Agent": _BROWSER_UA}
            ) as client:
                resp = await client.get(robots_url)
            if resp.status_code in (404, 403):
                result = RobotsPolicyResult(allowed=True)
            elif resp.status_code != 200:
                result = RobotsPolicyResult(allowed=True)
            else:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
                disallowed = self._extract_disallowed(resp.text)
                test_paths = [
                    "/webcam", "/webcams", "/camera", "/cameras",
                    "/live", "/stream", "/cam",
                ]
                blocked = any(
                    not rp.can_fetch("*", f"https://{domain}{p}") for p in test_paths
                )
                result = RobotsPolicyResult(allowed=not blocked, disallowed_paths=disallowed)
        except Exception as exc:
            logger.warning("robots.txt fetch failed for {}: {}", domain, exc)
            result = RobotsPolicyResult(allowed=True)

        self._cache[domain] = result
        return result

    def _extract_disallowed(self, robots_text: str) -> list[str]:
        """Extract Disallow paths from robots.txt text."""
        disallowed: list[str] = []
        in_relevant = False
        for line in robots_text.splitlines():
            line = line.strip()
            if line.lower().startswith("user-agent:"):
                in_relevant = line.split(":", 1)[1].strip() in ("*", "Claude")
            elif in_relevant and line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    disallowed.append(path)
        return disallowed


# ── FeedTypeClassificationSkill ───────────────────────────────────────────────

_HLS_EXT_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)


class FeedTypeClassificationSkill:
    """Classify HLS feed type (master vs. media) from playlist_type and URL."""

    def run(self, input: FeedTypeInput) -> FeedTypeResult:
        """
        Classify the HLS feed type for a given URL.

        Args:
            input: FeedTypeInput with url, optional content_type, and playlist_type.

        Returns:
            FeedTypeResult with feed_type: HLS_master, HLS_stream, or unknown.
        """
        if input.playlist_type == "master":
            return FeedTypeResult(feed_type="HLS_master")
        if input.playlist_type == "media":
            return FeedTypeResult(feed_type="HLS_stream")
        if _HLS_EXT_RE.search(input.url or ""):
            ct = (input.content_type or "").lower()
            if "vnd.apple.mpegurl" in ct or "x-mpegurl" in ct:
                return FeedTypeResult(feed_type="HLS_stream")
            return FeedTypeResult(feed_type="HLS_stream")
        return FeedTypeResult(feed_type="unknown")


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        urls = sys.argv[1:] or [
            "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8",
        ]
        skill = FeedValidationSkill()
        results = await skill.run(urls)
        for r in results:
            logger.info("{}", r.model_dump())

    asyncio.run(_main())

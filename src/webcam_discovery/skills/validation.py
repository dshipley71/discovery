#!/usr/bin/env python3
"""
validation.py — HLS (.m3u8) validation and robots.txt compliance.
Part of the Public Webcam Discovery System.

Validation strategy
-------------------
Only direct HLS (.m3u8) URLs are probed for liveness.

HLS (.m3u8)
  1. GET the first 4 KB of the URL.
  2. Verify #EXTM3U magic bytes near the start of the response.
  3. Classify playlist type:
     - Master playlist: #EXT-X-STREAM-INF → playlist_type='master', variant_streams extracted.
     - Media playlist:  #EXTINF / #EXT-X-TARGETDURATION → playlist_type='media'.

Any non-.m3u8 URL is rejected immediately with fail_reason='not_hls'.

Concurrency: asyncio.Semaphore(settings.validation_concurrency) — default 50.
Timeout:     connect=10 s, read=25 s.
Retry:       1 automatic retry (2 s back-off) on timeout.
User-Agent:  Browser-like string to avoid bot-blocking.

Fixes applied (2026-03-23)
--------------------------
unwrap_player_url() is called at the top of _dispatch() as a final safety net.

The primary unwrap happens in ValidationAgent._unwrap_candidates() before any
candidates reach this skill.  This second unwrap in _dispatch() covers edge
cases where wrapper URLs enter FeedValidationSkill directly — for example,
from MaintenanceAgent re-checking catalog entries stored before the fix was
applied, or from any future caller that bypasses ValidationAgent.
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
from tqdm.asyncio import tqdm_asyncio

from webcam_discovery.schemas import CameraStatus, FeedType, LegitimacyScore
from webcam_discovery.skills.traversal import unwrap_player_url   # ← FIX: import unwrapper


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

_HLS_MAGIC      = b"#EXTM3U"
_HLS_URL_RE     = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_AUTH_URL_RE    = re.compile(
    r"/(login|signin|sign-in|auth|register|subscribe|account|member|join)",
    re.IGNORECASE,
)

_BROAD_HLS_RE    = re.compile(r"""['"]([^'"]{4,500}\.m3u8[^'"]{0,100})['"]""",  re.IGNORECASE)
_JSON_HLS_RE     = re.compile(
    r'"(?:url|src|stream|hls|hlsUrl|hlsSrc|m3u8|streamUrl|videoUrl|liveUrl|'
    r'feedUrl|playbackUrl|mediaUrl|contentUrl|manifestUrl)"\s*:\s*"([^"]{4,500}\.m3u8[^"]{0,100})"',
    re.IGNORECASE,
)
_DATA_ATTR_HLS_RE = re.compile(
    r'data-(?:src|stream|url|hls|m3u8|video|live|feed|manifest)\s*=\s*["\']([^"\']{4,500}\.m3u8[^"\']{0,100})["\']',
    re.IGNORECASE,
)
_JS_VAR_HLS_RE   = re.compile(
    r'(?:var|let|const)\s+\w*(?:hls|stream|url|src|m3u8|video|live|feed)\w*\s*=\s*["\']([^"\']{4,500}\.m3u8[^"\']{0,100})["\']',
    re.IGNORECASE,
)

_OFFLINE_MARKERS_RE = re.compile(
    r'\b(?:camera\s+(?:is\s+)?(?:offline|unavailable|disabled|not\s+available)|'
    r'stream\s+(?:is\s+)?(?:offline|unavailable)|'
    r'no\s+signal|temporarily\s+unavailable|currently\s+unavailable|'
    r'webcam\s+(?:is\s+)?(?:offline|unavailable))\b',
    re.IGNORECASE,
)

_LIVE_CONTENT_TYPES = (
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _has_auth_path(url: str) -> bool:
    return bool(_AUTH_URL_RE.search(urlparse(url).path))


# ── FeedValidationSkill ────────────────────────────────────────────────────────

class FeedValidationSkill:
    """
    Validate a list of direct HLS (.m3u8) URLs for liveness.

    Any non-.m3u8 URL is rejected immediately with status="dead" and
    fail_reason="not_hls". Semaphore-limited concurrency avoids overwhelming
    servers.
    """

    async def run(
        self,
        urls: list[str],
        referers: Optional[dict[str, str]] = None,
    ) -> list[ValidationResult]:
        """
        Probe each URL and return a ValidationResult.

        Args:
            urls:     Candidate URLs to validate.
            referers: Optional mapping of url → Referer header value.

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
        _referers: dict[str, str] = referers or {}

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            tasks = [self._probe(client, url, sem, _referers.get(url)) for url in urls]
            return list(await tqdm_asyncio.gather(
                *tasks,
                desc="Probing URLs",
                unit="url",
                ncols=90,
            ))

    async def _probe(
        self,
        client: httpx.AsyncClient,
        url: str,
        sem: asyncio.Semaphore,
        referer: Optional[str] = None,
    ) -> ValidationResult:
        """Acquire semaphore, dispatch, retry once on timeout."""
        async with sem:
            result = await self._dispatch(client, url, referer=referer)
            if result.fail_reason == "timeout":
                logger.debug("FeedValidationSkill: retrying {} after timeout", url)
                await asyncio.sleep(2.0)
                result = await self._dispatch(client, url, referer=referer)
        return result

    async def _dispatch(
        self,
        client: httpx.AsyncClient,
        url: str,
        referer: Optional[str] = None,
    ) -> ValidationResult:
        """
        Route each URL to the HLS prober or reject it immediately.

        FIX: unwrap_player_url() is called first so that any player-wrapper
        URL that reaches this method directly (bypassing ValidationAgent's
        pre-processing pass) is resolved to its inner .m3u8 before probing.

        Only direct `.m3u8` URLs are valid inputs for this system.
        """
        # ── FIX: unwrap player-wrapper URLs before any other check ────────────
        clean_url = unwrap_player_url(url)
        if clean_url != url:
            logger.debug(
                "FeedValidationSkill._dispatch: unwrapped '{}' → '{}'",
                url, clean_url,
            )
            url = clean_url

        # Reject URLs without a valid HTTP/HTTPS scheme.
        if not url.lower().startswith(("http://", "https://")):
            return ValidationResult(
                url=url,
                legitimacy_score="low",
                status="dead",
                fail_reason="missing_protocol",
            )
        if _has_auth_path(url):
            return ValidationResult(
                url=url,
                legitimacy_score="low",
                status="dead",
                fail_reason="auth_url_pattern",
            )
        if _HLS_URL_RE.search(url):
            return await self._probe_hls(client, url, referer=referer)
        return ValidationResult(
            url=url,
            legitimacy_score="low",
            status="dead",
            fail_reason="not_hls",
        )

    async def _probe_hls(
        self,
        client: httpx.AsyncClient,
        url: str,
        referer: Optional[str] = None,
    ) -> ValidationResult:
        """
        Fetch HLS playlist, verify #EXTM3U magic, and classify as master or media.

        Master playlist (#EXT-X-STREAM-INF) → playlist_type='master', variant_streams extracted.
        Media playlist  (#EXTINF / #EXT-X-TARGETDURATION) → playlist_type='media'.
        """
        extra_headers = {"Referer": referer} if referer else {}
        try:
            async with client.stream("GET", url, headers=extra_headers) as resp:
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
            logger.debug("robots.txt fetch failed for {}: {}", domain, exc)
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

_HLS_EXT_RE  = re.compile(r"\.m3u8(\?|$)",  re.IGNORECASE)


class FeedTypeClassificationSkill:
    """Classify feed type (HLS master/stream) from playlist_type, URL, and content-type."""

    def run(self, input: FeedTypeInput) -> FeedTypeResult:
        """
        Classify the stream feed type for a given URL.

        Args:
            input: FeedTypeInput with url, optional content_type, and playlist_type.

        Returns:
            FeedTypeResult with feed_type: HLS_master, HLS_stream, or unknown.
        """
        if input.playlist_type == "master":
            return FeedTypeResult(feed_type="HLS_master")
        if input.playlist_type == "media":
            return FeedTypeResult(feed_type="HLS_stream")
        ct = (input.content_type or "").lower()
        if _HLS_EXT_RE.search(input.url or "") or "mpegurl" in ct:
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

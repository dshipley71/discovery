#!/usr/bin/env python3
"""
validation.py — Stream validation (HLS + MJPEG) and robots.txt compliance.
Part of the Public Webcam Discovery System.

Validation strategy
-------------------
Three URL types are probed for liveness:

HLS (.m3u8)
  1. GET the first 4 KB of the URL.
  2. Verify #EXTM3U magic byte on line 1.
  3. Classify playlist type:
     - Master playlist: #EXT-X-STREAM-INF → playlist_type='master', variant_streams extracted.
     - Media playlist:  #EXTINF / #EXT-X-TARGETDURATION → playlist_type='media'.

MJPEG (.mjpg / .mjpeg)
  Streaming GET — response headers only.  A 2xx with content-type containing
  'multipart', 'video', 'mjpeg', or 'jpeg' → live.  Any other 2xx on a .mjpg
  URL → optimistically live (medium legitimacy).

HTML embed pages (all other URLs)
  GET up to 32 KB.  Content-type checked first:
  - multipart/x-mixed-replace or video/* → live MJPEG/video stream.
  - text/html → scan body for embedded .m3u8 and .mjpeg URLs; probe each.
  - Anything else → dead (non_stream_content).

YouTube embeds, iframes with no extractable URL, and MP4-only pages are
rejected (no_stream_found_in_html or non_stream_content).

Concurrency: asyncio.Semaphore(settings.validation_concurrency) — default 50.
Timeout:     connect=10 s, read=25 s.
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
from tqdm.asyncio import tqdm_asyncio

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

_HLS_MAGIC      = b"#EXTM3U"
_HLS_URL_RE     = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_MJPEG_URL_RE   = re.compile(r"\.mjpe?g(\?|$)", re.IGNORECASE)
_AUTH_URL_RE    = re.compile(
    r"/(login|signin|sign-in|auth|register|subscribe|account|member|join)",
    re.IGNORECASE,
)

# Broad catch-all patterns for scanning raw HTML bodies.
_BROAD_HLS_RE   = re.compile(r"""['"]([^'"]{4,500}\.m3u8[^'"]{0,100})['"]""",  re.IGNORECASE)
_BROAD_MJPEG_RE = re.compile(r"""['"]([^'"]{4,500}\.mjpe?g[^'"]{0,100})['"]""", re.IGNORECASE)

# Content-type substrings that indicate a live stream is being served directly.
_LIVE_CONTENT_TYPES = (
    "multipart/x-mixed-replace",
    "video/",
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "video/x-motion-jpeg",
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

    async def run(
        self,
        urls: list[str],
        referers: Optional[dict[str, str]] = None,
    ) -> list[ValidationResult]:
        """
        Probe each URL and return a ValidationResult.

        Args:
            urls:     Candidate URLs to validate.
            referers: Optional mapping of url → Referer header value.  When
                      provided, the Referer is sent with HLS requests so that
                      CDN hotlink-protection rules (which gate .m3u8 delivery
                      to the originating webcam site) pass correctly.

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
        Route each URL to the appropriate prober.

        .m3u8  → _probe_hls    (HLS playlist validation)
        .mjpg  → _probe_mjpeg  (MJPEG stream header check)
        other  → _probe_generic (HTML embed page scan for embedded streams)
        """
        if _has_auth_path(url):
            return ValidationResult(
                url=url, legitimacy_score="low", status="dead",
                fail_reason="auth_url_pattern",
            )
        if _HLS_URL_RE.search(url):
            return await self._probe_hls(client, url, referer=referer)
        if _MJPEG_URL_RE.search(url):
            return await self._probe_mjpeg(client, url)
        return await self._probe_generic(client, url)

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

        Args:
            referer: If provided, sent as the HTTP ``Referer`` header so that
                     CDN hotlink-protection rules (which restrict .m3u8 delivery
                     to requests originating from the source webcam site) pass.
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

    async def _probe_mjpeg(
        self, client: httpx.AsyncClient, url: str
    ) -> ValidationResult:
        """
        Verify a .mjpg/.mjpeg URL is a live MJPEG stream.

        Opens a streaming GET and inspects response headers only (no body read)
        to avoid downloading the infinite MJPEG byte stream.  A 2xx whose
        content-type contains 'multipart', 'video', 'mjpeg', or 'jpeg' → live
        (high legitimacy).  Any other 2xx on a .mjpg URL is optimistically live
        (medium legitimacy) since many cameras omit the correct content-type.
        """
        try:
            async with client.stream("GET", url) as resp:
                ct = resp.headers.get("content-type", "").lower()
                if resp.status_code not in range(200, 207):
                    return ValidationResult(
                        url=url, status_code=resp.status_code,
                        legitimacy_score="low", status="dead",
                        fail_reason=f"http_{resp.status_code}",
                    )
                # Check headers only — do NOT read the body
            if any(k in ct for k in ("multipart", "video", "mjpeg", "jpeg", "mpegurl", "octet-stream")):
                return ValidationResult(
                    url=url, status_code=200, content_type=ct,
                    legitimacy_score="high", status="live",
                )
            return ValidationResult(
                url=url, status_code=200, content_type=ct or None,
                legitimacy_score="medium", status="live",
            )
        except httpx.TimeoutException:
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:100])

    async def _probe_generic(
        self, client: httpx.AsyncClient, url: str
    ) -> ValidationResult:
        """
        Probe an HTML embed page for embedded stream URLs.

        Reads up to 32 KB of the response body and:
        1. Returns live immediately if the content-type signals a direct stream
           (multipart/x-mixed-replace, video/*, application/x-mpegurl, …).
        2. For text/html responses, scans the body for quoted .m3u8 and .mjpeg
           URLs and probes each in turn, returning on the first live result.
        3. Returns dead (no_stream_found_in_html) if nothing live is found.
        """
        try:
            async with client.stream("GET", url) as resp:
                ct = resp.headers.get("content-type", "").lower()
                if resp.status_code not in range(200, 207):
                    return ValidationResult(
                        url=url, status_code=resp.status_code,
                        legitimacy_score="low", status="dead",
                        fail_reason=f"http_{resp.status_code}",
                    )

                # Direct live stream — no body needed
                if any(k in ct for k in _LIVE_CONTENT_TYPES):
                    return ValidationResult(
                        url=url, status_code=200, content_type=ct,
                        legitimacy_score="high", status="live",
                    )

                # Non-HTML, non-stream content
                if ct and "text/html" not in ct:
                    return ValidationResult(
                        url=url, status_code=200, content_type=ct,
                        legitimacy_score="low", status="dead",
                        fail_reason="non_stream_content",
                    )

                # HTML — read up to 32 KB to scan for embedded stream URLs
                buf = b""
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) >= 32768:
                        break

            body = buf.decode("utf-8", errors="replace")

            # Probe any .m3u8 URLs found in the page
            hls_seen: set[str] = set()
            for m in _BROAD_HLS_RE.finditer(body):
                raw = m.group(1)
                abs_url = raw if raw.startswith("http") else urljoin(url, raw)
                if abs_url not in hls_seen:
                    hls_seen.add(abs_url)
                    result = await self._probe_hls(client, abs_url)
                    if result.status == "live":
                        return result

            # Probe any .mjpg/.mjpeg URLs found in the page
            mjpeg_seen: set[str] = set()
            for m in _BROAD_MJPEG_RE.finditer(body):
                raw = m.group(1)
                abs_url = raw if raw.startswith("http") else urljoin(url, raw)
                if abs_url not in mjpeg_seen:
                    mjpeg_seen.add(abs_url)
                    result = await self._probe_mjpeg(client, abs_url)
                    if result.status == "live":
                        return result

            return ValidationResult(
                url=url, status_code=200, content_type=ct or None,
                legitimacy_score="low", status="dead",
                fail_reason="no_stream_found_in_html",
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
_MJPEG_EXT_RE = re.compile(r"\.mjpe?g(\?|$)", re.IGNORECASE)


class FeedTypeClassificationSkill:
    """Classify feed type (HLS master/stream or MJPEG) from playlist_type, URL, and content-type."""

    def run(self, input: FeedTypeInput) -> FeedTypeResult:
        """
        Classify the stream feed type for a given URL.

        Args:
            input: FeedTypeInput with url, optional content_type, and playlist_type.

        Returns:
            FeedTypeResult with feed_type: HLS_master, HLS_stream, MJPEG, or unknown.
        """
        if input.playlist_type == "master":
            return FeedTypeResult(feed_type="HLS_master")
        if input.playlist_type == "media":
            return FeedTypeResult(feed_type="HLS_stream")
        ct = (input.content_type or "").lower()
        if _HLS_EXT_RE.search(input.url or "") or "mpegurl" in ct:
            return FeedTypeResult(feed_type="HLS_stream")
        if _MJPEG_EXT_RE.search(input.url or "") or "multipart/x-mixed-replace" in ct or "mjpeg" in ct:
            return FeedTypeResult(feed_type="MJPEG")
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

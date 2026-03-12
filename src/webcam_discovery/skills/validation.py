#!/usr/bin/env python3
"""
validation.py — Feed liveness validation, robots.txt compliance, and feed type classification.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse
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
    page_html: Optional[str] = None


class FeedTypeResult(BaseModel):
    """Result of feed type classification."""

    feed_type: FeedType


# ── Auth detection helpers ─────────────────────────────────────────────────────

_AUTH_URL_PATTERNS = re.compile(
    r"/(login|signin|sign-in|auth|register|subscribe|account|member|join)", re.IGNORECASE
)

_MEDIA_CONTENT_TYPES = {
    "multipart/x-mixed-replace",
    "image/jpeg",
    "image/png",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/ogg",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "video/x-flv",
    "video/mpeg",
    "application/octet-stream",
}


def _is_youtube_nocookie(url: str) -> bool:
    """Return True if the URL is a YouTube no-cookie embed."""
    parsed = urlparse(url)
    return parsed.netloc in ("www.youtube-nocookie.com", "youtube-nocookie.com") and "/embed/" in parsed.path


def _has_auth_url_pattern(url: str) -> bool:
    """Return True if the URL path contains an auth-related segment."""
    parsed = urlparse(url)
    return bool(_AUTH_URL_PATTERNS.search(parsed.path))


def _classify_response(
    url: str,
    status_code: int,
    content_type: str,
    response_headers: dict,
) -> tuple[LegitimacyScore, CameraStatus, Optional[str]]:
    """Return (legitimacy_score, status, fail_reason) from response data."""
    ct_lower = content_type.lower() if content_type else ""

    # WWW-Authenticate header → auth gated
    if "www-authenticate" in response_headers or "x-auth-required" in response_headers:
        return "low", "dead", "auth_header_present"

    if status_code in (401, 403, 407):
        return "low", "dead", f"http_{status_code}"

    if status_code == 404:
        return "medium", "dead", "http_404"

    if status_code in range(301, 303):
        return "medium", "unknown", "redirect"

    if status_code not in range(200, 207):
        return "medium", "dead", f"http_{status_code}"

    # 200–206 range
    # Check for auth URL pattern
    if _has_auth_url_pattern(url):
        return "low", "dead", "auth_url_pattern"

    # Check content-type
    ct_base = ct_lower.split(";")[0].strip()
    if any(ct_base.startswith(m) for m in _MEDIA_CONTENT_TYPES):
        return "high", "live", None

    if "text/html" in ct_lower:
        return "medium", "live", "html_content_type"

    # Unknown content-type but 200
    return "medium", "live", None


# ── FeedValidationSkill ────────────────────────────────────────────────────────

class FeedValidationSkill:
    """Validate a list of URLs for liveness and public accessibility."""

    async def run(self, urls: list[str]) -> list[ValidationResult]:
        """
        Perform HTTP HEAD (fallback GET) checks on each URL.

        Args:
            urls: List of URLs to validate.

        Returns:
            List of ValidationResult objects.
        """
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=True,
            max_redirects=2,
        ) as client:
            tasks = [self._check(client, url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=False)
        return list(results)

    async def _check(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """Check a single URL and return a ValidationResult."""
        # YouTube no-cookie URLs are exempt from content-type check
        if _is_youtube_nocookie(url):
            logger.debug("YouTube nocookie exempt: {}", url)
            return ValidationResult(
                url=url,
                status_code=200,
                content_type="text/html",
                legitimacy_score="medium",
                status="live",
                fail_reason=None,
            )

        # Auth URL pattern fast-fail
        if _has_auth_url_pattern(url):
            return ValidationResult(
                url=url,
                legitimacy_score="low",
                status="dead",
                fail_reason="auth_url_pattern",
            )

        try:
            response = await client.head(url)
            content_type = response.headers.get("content-type", "")
            headers_dict = dict(response.headers)

            # Fall back to GET if HEAD returned no content-type or unsupported
            if response.status_code in (405, 501) or not content_type:
                try:
                    async with client.stream("GET", url) as stream_resp:
                        await stream_resp.aread()
                        response = stream_resp
                        content_type = response.headers.get("content-type", "")
                        headers_dict = dict(response.headers)
                except Exception:
                    pass

            legit, status, fail = _classify_response(
                url, response.status_code, content_type, headers_dict
            )
            return ValidationResult(
                url=url,
                status_code=response.status_code,
                content_type=content_type or None,
                legitimacy_score=legit,
                status=status,
                fail_reason=fail,
            )

        except httpx.TimeoutException:
            logger.warning("Timeout validating {}", url)
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            logger.warning("Error validating {}: {}", url, exc)
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:120])


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
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(robots_url)
            if resp.status_code == 404:
                result = RobotsPolicyResult(allowed=True)
            elif resp.status_code != 200:
                result = RobotsPolicyResult(allowed=True)
            else:
                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
                disallowed = self._extract_disallowed(resp.text)
                # Check webcam-relevant paths
                test_paths = [
                    "/webcam", "/webcams", "/camera", "/cameras",
                    "/live", "/stream", "/cam",
                ]
                blocked = False
                for path in test_paths:
                    if not rp.can_fetch("*", f"https://{domain}{path}"):
                        blocked = True
                        break
                result = RobotsPolicyResult(
                    allowed=not blocked,
                    disallowed_paths=disallowed,
                )
        except Exception as exc:
            logger.warning("robots.txt fetch failed for {}: {}", domain, exc)
            result = RobotsPolicyResult(allowed=True)

        self._cache[domain] = result
        return result

    def _extract_disallowed(self, robots_text: str) -> list[str]:
        """Extract Disallow paths from robots.txt text."""
        disallowed = []
        in_relevant_agent = False
        for line in robots_text.splitlines():
            line = line.strip()
            if line.lower().startswith("user-agent:"):
                agent = line.split(":", 1)[1].strip()
                in_relevant_agent = agent in ("*", "Claude")
            elif in_relevant_agent and line.lower().startswith("disallow:"):
                path = line.split(":", 1)[1].strip()
                if path:
                    disallowed.append(path)
        return disallowed


# ── FeedTypeClassificationSkill ───────────────────────────────────────────────

_YOUTUBE_PATTERN = re.compile(r"(youtube\.com/embed/|youtu\.be/)", re.IGNORECASE)
_HLS_EXTENSIONS = re.compile(r"\.(m3u8)(\?|$)", re.IGNORECASE)
_MJPEG_EXTENSIONS = re.compile(r"\.(mjpg|mjpeg)(\?|$)", re.IGNORECASE)
_JPEG_EXTENSIONS = re.compile(r"\.(jpg|jpeg|png)(\?|$)", re.IGNORECASE)
_MP4_EXTENSIONS = re.compile(r"\.mp4(\?|$)", re.IGNORECASE)

_JS_PLAYER_PATTERNS = re.compile(
    r"(jwplayer|hls\.loadSource|videojs|Video\.js|data-setup|Hls\.js|flowplayer)", re.IGNORECASE
)
_META_REFRESH = re.compile(r'<meta[^>]+http-equiv=["\']refresh["\']', re.IGNORECASE)
_IFRAME_PATTERN = re.compile(r"<iframe", re.IGNORECASE)


class FeedTypeClassificationSkill:
    """Classify feed type from URL, content-type, and page HTML."""

    def run(self, input: FeedTypeInput) -> FeedTypeResult:
        """
        Classify the feed type for a given URL.

        Args:
            input: FeedTypeInput with url, optional content_type and page_html.

        Returns:
            FeedTypeResult with feed_type string.
        """
        url = input.url or ""
        ct = (input.content_type or "").lower()
        html = input.page_html or ""

        # YouTube
        if _YOUTUBE_PATTERN.search(url):
            return FeedTypeResult(feed_type="youtube_live")
        if "youtube-nocookie.com/embed/" in url:
            return FeedTypeResult(feed_type="youtube_live")

        # HLS
        if _HLS_EXTENSIONS.search(url) or "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
            return FeedTypeResult(feed_type="HLS")

        # MJPEG
        if _MJPEG_EXTENSIONS.search(url) or "multipart/x-mixed-replace" in ct:
            return FeedTypeResult(feed_type="MJPEG")

        # MP4 stream
        if _MP4_EXTENSIONS.search(url) and "video/mp4" in ct:
            return FeedTypeResult(feed_type="HLS")

        # Static image refresh
        if _JPEG_EXTENSIONS.search(url) and ("image/jpeg" in ct or "image/png" in ct):
            return FeedTypeResult(feed_type="static_refresh")
        if html and (_META_REFRESH.search(html) or "setInterval" in html) and "image/" in ct:
            return FeedTypeResult(feed_type="static_refresh")

        # JS player
        if html and _JS_PLAYER_PATTERNS.search(html):
            return FeedTypeResult(feed_type="js_player")

        # iframe embed
        if html and _IFRAME_PATTERN.search(html):
            return FeedTypeResult(feed_type="iframe")

        # Text/html with no player detected
        if "text/html" in ct:
            return FeedTypeResult(feed_type="iframe")

        return FeedTypeResult(feed_type="unknown")


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        urls = sys.argv[1:] or ["https://example.com/stream.mjpg"]
        skill = FeedValidationSkill()
        results = await skill.run(urls)
        for r in results:
            logger.info("{}", r.model_dump())

    asyncio.run(_main())

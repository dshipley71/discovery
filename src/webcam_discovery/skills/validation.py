#!/usr/bin/env python3
"""
validation.py — Feed liveness validation, robots.txt compliance, and feed type classification.
Part of the Public Webcam Discovery System.

Validation strategy by URL type
---------------------------------
HLS  (.m3u8)    — GET first 1 KB; verify #EXTM3U magic → high/live.
MJPEG (.mjpeg)  — HEAD for multipart/x-mixed-replace; byte-probe if HEAD returns 405.
MP4/WebM        — HEAD for video/* content-type.
YouTube embed   — Exempt from stream-content check; return medium/live.
HTML page       — GET full page; scan for live-player patterns (HLS.js, JW Player,
                  Video.js, flowplayer, data-setup, .m3u8 src, MJPEG img).
                  → high   if live-player pattern found in HTML
                  → medium if <video> tag or camera-keyword present
                  → low    if only static images detected, no video

Concurrency: asyncio.Semaphore(settings.validation_concurrency) — default 50.
Timeout:     connect=10 s, read=25 s — generous for slow camera servers.
Retry:       1 automatic retry (2 s back-off) on timeout.
User-Agent:  Browser-like string to avoid bot-blocking.
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


# ── Constants ──────────────────────────────────────────────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HLS_MAGIC   = b"#EXTM3U"
_JPEG_MAGIC  = b"\xff\xd8\xff"

_HLS_URL_RE    = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_MJPEG_URL_RE  = re.compile(r"\.(mjpg|mjpeg)(\?|$)", re.IGNORECASE)
_STATIC_IMG_RE = re.compile(r"\.(jpg|jpeg|png|gif|bmp|webp)(\?|$)", re.IGNORECASE)
_MP4_URL_RE    = re.compile(r"\.(mp4|webm|ogv)(\?|$)", re.IGNORECASE)
_YOUTUBE_RE    = re.compile(r"youtube(?:-nocookie)?\.com/embed/", re.IGNORECASE)
_AUTH_URL_RE   = re.compile(
    r"/(login|signin|sign-in|auth|register|subscribe|account|member|join)",
    re.IGNORECASE,
)

# Live-stream indicators inside HTML — any match → HIGH legitimacy
_LIVE_HTML_HIGH_RE = re.compile(
    r"""(?:
        \.m3u8['"\s]                            |
        multipart/x-mixed-replace               |
        hls\.loadSource\s*\(                    |
        jwplayer\s*\(                           |
        (?<!\w)videojs\s*\(                     |
        Hls\.js                                 |
        flowplayer\s*\(                         |
        data-setup\s*=                          |
        ['"]stream[Uu]rl['"]                    |
        ['"]hls[Uu]rl['"]                       |
        ['"]stream_url['"]                      |
        src\s*=\s*['"][^'"]*\.m3u8              |
        data-src\s*=\s*['"][^'"]*\.m3u8         |
        <img[^>]+src\s*=\s*[^>]*\.mjpe?g
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Secondary indicators → MEDIUM legitimacy
_LIVE_HTML_MED_RE = re.compile(
    r"(?:<video[\s>]|autoplay|webcam|live\s*cam|camera\s*feed|live\s*stream)",
    re.IGNORECASE,
)

_MEDIA_CONTENT_TYPES = frozenset({
    "multipart/x-mixed-replace", "image/jpeg", "image/png",
    "video/mp4", "video/webm", "video/ogg",
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "video/x-flv", "video/mpeg",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_youtube(url: str) -> bool:
    p = urlparse(url)
    return p.netloc in (
        "www.youtube-nocookie.com", "youtube-nocookie.com",
        "www.youtube.com", "youtube.com",
    ) and "/embed/" in p.path


def _has_auth_path(url: str) -> bool:
    return bool(_AUTH_URL_RE.search(urlparse(url).path))


def _ct_base(ct: str) -> str:
    return ct.split(";")[0].strip().lower() if ct else ""


def _classify_by_status(
    url: str, code: int, ct: str, headers: dict
) -> tuple[LegitimacyScore, CameraStatus, Optional[str]]:
    """Classify purely from HTTP status + content-type (no HTML inspection)."""
    if "www-authenticate" in headers or "x-auth-required" in headers:
        return "low", "dead", "auth_header"
    if code in (401, 403, 407):
        return "low", "dead", f"http_{code}"
    if code == 404:
        return "low", "dead", "http_404"
    if code in range(301, 308):
        return "medium", "unknown", "redirect"
    if code not in range(200, 207):
        return "low", "dead", f"http_{code}"
    if _has_auth_path(url):
        return "low", "dead", "auth_url_pattern"
    base = _ct_base(ct)
    if any(base.startswith(m) for m in _MEDIA_CONTENT_TYPES):
        return "high", "live", None
    if "text/html" in ct.lower():
        return "medium", "live", "html_no_probe"
    return "medium", "live", None


# ── FeedValidationSkill ────────────────────────────────────────────────────────

class FeedValidationSkill:
    """
    Validate a list of URLs for liveness and live-video content.

    Routes each URL to a specialised probe: HLS playlist magic check,
    MJPEG multipart probe, video/* content-type check, or HTML deep-scan
    for live-player patterns.  Semaphore-limited to avoid overwhelming servers.
    """

    async def run(self, urls: list[str]) -> list[ValidationResult]:
        """
        Probe each URL and return a ValidationResult.

        Args:
            urls: Candidate URLs to validate.

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
        """Route to the correct probe based on URL type."""
        if _is_youtube(url):
            return ValidationResult(
                url=url, status_code=200, content_type="text/html",
                legitimacy_score="medium", status="live",
            )
        if _has_auth_path(url):
            return ValidationResult(
                url=url, legitimacy_score="low", status="dead",
                fail_reason="auth_url_pattern",
            )
        if _HLS_URL_RE.search(url):
            return await self._probe_hls(client, url)
        if _MJPEG_URL_RE.search(url):
            return await self._probe_mjpeg(client, url)
        if _MP4_URL_RE.search(url):
            return await self._probe_video(client, url)
        if _STATIC_IMG_RE.search(url):
            return await self._probe_static_image(client, url)
        return await self._probe_html(client, url)

    # ── Per-type probes ───────────────────────────────────────────────────────

    async def _probe_hls(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """Fetch first 1 KB of HLS playlist and verify #EXTM3U magic."""
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
                    if len(buf) >= 1024:
                        break

            if _HLS_MAGIC in buf:
                logger.debug("FeedValidationSkill: HLS confirmed {}", url)
                return ValidationResult(
                    url=url, status_code=200, content_type=ct or None,
                    legitimacy_score="high", status="live",
                )
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
        except httpx.TimeoutException:
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:100])

    async def _probe_mjpeg(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """HEAD for multipart/x-mixed-replace; fall back to byte-probe GET."""
        try:
            resp = await client.head(url)
            ct = resp.headers.get("content-type", "")

            if "multipart/x-mixed-replace" in ct.lower():
                return ValidationResult(
                    url=url, status_code=resp.status_code, content_type=ct,
                    legitimacy_score="high", status="live",
                )
            if resp.status_code in (405, 501) or not ct:
                async with client.stream("GET", url) as gr:
                    ct = gr.headers.get("content-type", "")
                    if "multipart/x-mixed-replace" in ct.lower():
                        return ValidationResult(
                            url=url, status_code=gr.status_code, content_type=ct,
                            legitimacy_score="high", status="live",
                        )
                    buf = b""
                    async for chunk in gr.aiter_bytes():
                        buf += chunk
                        if len(buf) >= 512:
                            break
                    if buf.startswith(_JPEG_MAGIC):
                        return ValidationResult(
                            url=url, status_code=gr.status_code, content_type=ct or None,
                            legitimacy_score="high", status="live",
                        )
            legit, status, fail = _classify_by_status(url, resp.status_code, ct, dict(resp.headers))
            return ValidationResult(
                url=url, status_code=resp.status_code, content_type=ct or None,
                legitimacy_score=legit, status=status, fail_reason=fail,
            )
        except httpx.TimeoutException:
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:100])

    async def _probe_video(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """HEAD probe for MP4/WebM — confirm video/* content-type."""
        try:
            resp = await client.head(url)
            ct = resp.headers.get("content-type", "")
            base = _ct_base(ct)
            if base.startswith("video/") or base == "application/octet-stream":
                return ValidationResult(
                    url=url, status_code=resp.status_code, content_type=ct,
                    legitimacy_score="high", status="live",
                )
            legit, status, fail = _classify_by_status(url, resp.status_code, ct, dict(resp.headers))
            return ValidationResult(
                url=url, status_code=resp.status_code, content_type=ct or None,
                legitimacy_score=legit, status=status, fail_reason=fail,
            )
        except httpx.TimeoutException:
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:100])

    async def _probe_static_image(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """Accessible static images are marked low legitimacy (not a live feed)."""
        try:
            resp = await client.head(url)
            ct = resp.headers.get("content-type", "")
            if resp.status_code in range(200, 207):
                return ValidationResult(
                    url=url, status_code=resp.status_code, content_type=ct or None,
                    legitimacy_score="low", status="live",
                    fail_reason="static_image",
                )
            legit, status, fail = _classify_by_status(url, resp.status_code, ct, dict(resp.headers))
            return ValidationResult(
                url=url, status_code=resp.status_code, content_type=ct or None,
                legitimacy_score=legit, status=status, fail_reason=fail,
            )
        except httpx.TimeoutException:
            return ValidationResult(url=url, status="unknown", fail_reason="timeout")
        except Exception as exc:
            return ValidationResult(url=url, status="unknown", fail_reason=str(exc)[:100])

    async def _probe_html(self, client: httpx.AsyncClient, url: str) -> ValidationResult:
        """
        GET page and scan the first 64 KB for live-video player patterns.

        high   → live-player pattern (_LIVE_HTML_HIGH_RE matched)
        medium → <video> tag or camera keyword present
        low    → 200 OK but no video detected
        """
        try:
            resp = await client.get(url)
            ct = resp.headers.get("content-type", "")
            if resp.status_code not in range(200, 207):
                legit, status, fail = _classify_by_status(
                    url, resp.status_code, ct, dict(resp.headers)
                )
                return ValidationResult(
                    url=url, status_code=resp.status_code, content_type=ct or None,
                    legitimacy_score=legit, status=status, fail_reason=fail,
                )

            html = resp.text[:65_536]

            if _LIVE_HTML_HIGH_RE.search(html):
                logger.debug("FeedValidationSkill: live-player detected in HTML {}", url)
                return ValidationResult(
                    url=url, status_code=200, content_type=ct or None,
                    legitimacy_score="high", status="live",
                )
            if _LIVE_HTML_MED_RE.search(html):
                return ValidationResult(
                    url=url, status_code=200, content_type=ct or None,
                    legitimacy_score="medium", status="live",
                    fail_reason="html_content_type",
                )
            return ValidationResult(
                url=url, status_code=200, content_type=ct or None,
                legitimacy_score="low", status="unknown",
                fail_reason="no_live_video_detected",
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

_YT_PATTERN      = re.compile(r"(youtube\.com/embed/|youtu\.be/)", re.IGNORECASE)
_HLS_EXT_RE      = re.compile(r"\.(m3u8)(\?|$)", re.IGNORECASE)
_MJPEG_EXT_RE    = re.compile(r"\.(mjpg|mjpeg)(\?|$)", re.IGNORECASE)
_JPEG_EXT_RE     = re.compile(r"\.(jpg|jpeg|png)(\?|$)", re.IGNORECASE)
_MP4_EXT_RE      = re.compile(r"\.(mp4|webm)(\?|$)", re.IGNORECASE)
_JS_PLAYER_RE    = re.compile(
    r"(jwplayer|hls\.loadSource|videojs|Video\.js|data-setup|Hls\.js|flowplayer)",
    re.IGNORECASE,
)
_META_REFRESH_RE = re.compile(r'<meta[^>]+http-equiv=["\']refresh["\']', re.IGNORECASE)
_IFRAME_RE       = re.compile(r"<iframe", re.IGNORECASE)


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
        url  = input.url or ""
        ct   = (input.content_type or "").lower()
        html = input.page_html or ""

        if _YT_PATTERN.search(url) or "youtube-nocookie.com/embed/" in url:
            return FeedTypeResult(feed_type="youtube_live")
        if _HLS_EXT_RE.search(url) or "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
            return FeedTypeResult(feed_type="HLS")
        if _MJPEG_EXT_RE.search(url) or "multipart/x-mixed-replace" in ct:
            return FeedTypeResult(feed_type="MJPEG")
        if _MP4_EXT_RE.search(url) and "video/" in ct:
            return FeedTypeResult(feed_type="HLS")
        if _JPEG_EXT_RE.search(url) and ("image/jpeg" in ct or "image/png" in ct):
            return FeedTypeResult(feed_type="static_refresh")
        if html and (_META_REFRESH_RE.search(html) or "setInterval" in html) and "image/" in ct:
            return FeedTypeResult(feed_type="static_refresh")
        if html and _JS_PLAYER_RE.search(html):
            return FeedTypeResult(feed_type="js_player")
        if html and _IFRAME_RE.search(html):
            return FeedTypeResult(feed_type="iframe")
        if "text/html" in ct:
            return FeedTypeResult(feed_type="iframe")
        return FeedTypeResult(feed_type="unknown")


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        urls = sys.argv[1:] or [
            "https://www.webcamtaxi.com/en/usa/new-york-state/times-square.html",
        ]
        skill = FeedValidationSkill()
        results = await skill.run(urls)
        for r in results:
            logger.info("{}", r.model_dump())

    asyncio.run(_main())

#!/usr/bin/env python3
"""
browser_validation.py — Headless-browser stream URL discovery.
Part of the Public Webcam Discovery System.

Why this exists
---------------
Static HTML probing (FeedValidationSkill._probe_generic) reads up to 32 KB of
raw HTML.  The majority of modern webcam sites do NOT embed the stream URL in
the HTML at all — they load it dynamically via JavaScript fetch() / XHR calls
triggered by page render or a user clicking the play button.

This skill opens each candidate page in headless Chromium, intercepts every
network response, clicks common play-button selectors, waits a few seconds for
the stream to start, and returns the first HLS (.m3u8) or MJPEG URL that appears
in the browser's network traffic.

It is designed as an optional second pass:  run it only on pages that the static
prober classified as dead / unknown.  Enable it by setting:
    WCD_USE_BROWSER_VALIDATION=true

Playwright must be installed and Chromium must be available:
    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from loguru import logger
from pydantic import BaseModel
from tqdm.asyncio import tqdm_asyncio


# ── Constants ─────────────────────────────────────────────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_HLS_RE   = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
_MJPEG_RE = re.compile(r"\.mjpe?g(\?|$)", re.IGNORECASE)

# Content-type substrings that signal a live video stream in a network response.
_STREAM_CONTENT_TYPES = (
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "video/mp2t",
    "multipart/x-mixed-replace",
    "video/x-motion-jpeg",
)

# CSS selectors tried in order when looking for a play button.
# The first selector that successfully produces a click wins; the rest are skipped.
_PLAY_SELECTORS: list[str] = [
    "[aria-label*='play' i]",       # generic accessible play button
    "button.play",
    "[class*='play-btn']",
    "[class*='play-button']",
    "[class*='PlayButton']",
    ".vjs-play-control",            # Video.js
    ".jw-icon-play",                # JW Player
    ".jw-state-idle .jw-display",
    ".mejs__play",                  # MediaElement.js
    ".plyr__control--overlaid",     # Plyr
    ".fc-play",                     # Flowplayer
    ".fp-play",                     # Flowplayer 7+
    "[data-action='play']",
    "video",                        # clicking <video> itself often starts playback
]


# ── I/O Models ────────────────────────────────────────────────────────────────

class BrowserValidationInput(BaseModel):
    """Input for browser-based stream URL discovery."""
    urls: list[str]


class BrowserValidationOutput(BaseModel):
    """Maps each probed page URL to the discovered direct stream URL, when found."""
    stream_map: dict[str, str] = {}


# ── BrowserValidationSkill ────────────────────────────────────────────────────

class BrowserValidationSkill:
    """
    Discover stream URLs from webcam embed pages using a headless browser.

    Many modern webcam sites load their .m3u8 stream URL dynamically via
    JavaScript after page render.  This skill opens each page in headless
    Chromium, intercepts all network responses, clicks common play-button
    selectors, and returns the first HLS or MJPEG URL it observes in the
    browser's network traffic.

    Concurrency is bounded by ``settings.browser_validation_concurrency``
    (default 3) because each browser session is memory-heavy (~100 MB RAM).
    The skill is a no-op if ``playwright`` is not importable.
    """

    async def run(self, urls: list[str]) -> BrowserValidationOutput:
        """
        Probe each URL in a headless browser and return a mapping of
        page URL → direct stream URL for every page where a stream was found.

        Direct .m3u8 / .mjpeg URLs are silently skipped — they are already
        handled by FeedValidationSkill and need no browser.

        Args:
            urls: HTML page URLs to probe.

        Returns:
            BrowserValidationOutput with stream_map populated for pages where
            a stream URL was discovered.
        """
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            logger.warning(
                "BrowserValidationSkill: playwright not importable — "
                "install playwright and run 'playwright install chromium'"
            )
            return BrowserValidationOutput()

        from webcam_discovery.config import settings

        # Only probe HTML pages — direct stream URLs are handled by FeedValidationSkill
        html_urls = [u for u in urls if not _HLS_RE.search(u) and not _MJPEG_RE.search(u)]
        if not html_urls:
            return BrowserValidationOutput()

        logger.info(
            "BrowserValidationSkill: second-pass probing {} pages with headless browser "
            "(concurrency={}, timeout={}s)",
            len(html_urls),
            settings.browser_validation_concurrency,
            settings.browser_validation_timeout,
        )

        sem = asyncio.Semaphore(settings.browser_validation_concurrency)
        stream_map: dict[str, str] = {}

        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-setuid-sandbox",
                    "--disable-extensions",
                    "--mute-audio",
                ],
            )

            tasks = [
                self._probe_page(browser, url, sem, settings.browser_validation_timeout)
                for url in html_urls
            ]
            results: list[Optional[str]] = list(await tqdm_asyncio.gather(
                *tasks,
                desc="Browser probing",
                unit="page",
                ncols=90,
            ))

            await browser.close()

        for url, stream_url in zip(html_urls, results):
            if stream_url:
                stream_map[url] = stream_url

        logger.info(
            "BrowserValidationSkill: found stream URLs for {}/{} pages",
            len(stream_map),
            len(html_urls),
        )
        return BrowserValidationOutput(stream_map=stream_map)

    async def _probe_page(
        self,
        browser,
        page_url: str,
        sem: asyncio.Semaphore,
        timeout_s: int,
    ) -> Optional[str]:
        """
        Open one page in the browser and return the first live stream URL found.

        Strategy:
        1. Navigate to the page (wait for domcontentloaded to save time).
        2. Intercept all network responses; collect URLs that look like streams.
        3. Try clicking common play-button selectors (first match wins).
        4. Wait ``timeout_s`` seconds (capped at 6 s) for stream URLs to appear.
        5. Return first HLS URL found; fall back to MJPEG; return None if nothing.

        Args:
            browser:   Shared Playwright Browser instance.
            page_url:  HTML page to open.
            sem:       Semaphore bounding concurrent browser sessions.
            timeout_s: Maximum seconds to wait for a stream URL.

        Returns:
            Direct stream URL string, or None if nothing was discovered.
        """
        async with sem:
            context = await browser.new_context(
                user_agent=_BROWSER_UA,
                ignore_https_errors=True,
                java_script_enabled=True,
            )
            page = await context.new_page()
            found: list[str] = []

            def on_response(response) -> None:
                """Collect stream URLs from intercepted network responses."""
                try:
                    url = response.url
                    ct = response.headers.get("content-type", "").lower()
                    if (
                        _HLS_RE.search(url)
                        or _MJPEG_RE.search(url)
                        or any(k in ct for k in _STREAM_CONTENT_TYPES)
                    ):
                        found.append(url)
                        logger.debug(
                            "BrowserValidationSkill: intercepted stream {} on {}",
                            url, page_url,
                        )
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=15_000,
                )

                # Try clicking a play button — many cameras only start streaming
                # after user interaction with the play button.
                for selector in _PLAY_SELECTORS:
                    try:
                        await page.click(selector, timeout=1_500, force=True)
                        logger.debug(
                            "BrowserValidationSkill: clicked '{}' on {}",
                            selector, page_url,
                        )
                        break
                    except Exception:
                        pass

                # Wait for the stream to start appearing in network traffic.
                # Cap at 6 s to keep per-page overhead reasonable.
                wait_ms = min(timeout_s * 1_000, 6_000)
                await page.wait_for_timeout(wait_ms)

            except Exception as exc:
                logger.debug(
                    "BrowserValidationSkill: navigation error for {}: {}",
                    page_url, str(exc)[:120],
                )
            finally:
                await context.close()

            # Prefer HLS (.m3u8) streams over MJPEG — wider browser support.
            hls_urls = [u for u in found if _HLS_RE.search(u)]
            return hls_urls[0] if hls_urls else (found[0] if found else None)


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        urls = sys.argv[1:] or [
            "https://www.skylinewebcams.com/en/webcam/france/ile-de-france/paris/eiffel-tower.html",
        ]
        skill = BrowserValidationSkill()
        result = await skill.run(urls)
        if result.stream_map:
            for page_url, stream_url in result.stream_map.items():
                logger.info("  {} → {}", page_url, stream_url)
        else:
            logger.info("No stream URLs discovered.")

    asyncio.run(_main())

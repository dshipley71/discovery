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

_HLS_RE   = re.compile(r"\.m3u8(\?|$|/)", re.IGNORECASE)
_MJPEG_RE = re.compile(r"\.mjpe?g(\?|$)", re.IGNORECASE)
_DASH_RE  = re.compile(r"\.mpd(\?|$)", re.IGNORECASE)

# Content-type substrings that signal a live video stream in a network response.
_STREAM_CONTENT_TYPES = (
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "video/mp2t",
    "multipart/x-mixed-replace",
    "video/x-motion-jpeg",
    "application/dash+xml",
)

# Text markers indicating camera is offline — detected in rendered page content.
_OFFLINE_MARKERS = (
    "camera offline",
    "camera unavailable",
    "camera is offline",
    "stream unavailable",
    "stream offline",
    "no signal",
    "temporarily unavailable",
    "currently unavailable",
    "webcam offline",
    "webcam unavailable",
    "not available",
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
    offline_pages: list[str] = []
    """Page URLs where offline/unavailable markers were detected in rendered content."""


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
        offline_pages: list[str] = []

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
            # Each result is (stream_url_or_None, is_offline)
            results: list[tuple[Optional[str], bool]] = list(await tqdm_asyncio.gather(
                *tasks,
                desc="Browser probing",
                unit="page",
                ncols=90,
            ))

            await browser.close()

        for url, (stream_url, is_offline) in zip(html_urls, results):
            if stream_url:
                stream_map[url] = stream_url
            elif is_offline:
                offline_pages.append(url)

        logger.info(
            "BrowserValidationSkill: found stream URLs for {}/{} pages; {} marked offline",
            len(stream_map),
            len(html_urls),
            len(offline_pages),
        )
        return BrowserValidationOutput(stream_map=stream_map, offline_pages=offline_pages)

    async def _probe_page(
        self,
        browser,
        page_url: str,
        sem: asyncio.Semaphore,
        timeout_s: int,
    ) -> tuple[Optional[str], bool]:
        """
        Open one page in the browser and return (stream_url_or_None, is_offline).

        Strategy:
        1. Navigate to the page waiting for ``networkidle`` (catches async XHR/fetch
           calls that fire after initial DOM load) — falls back to ``domcontentloaded``
           on timeout so we still process pages that never fully settle.
        2. Intercept all network responses; collect URLs that look like streams
           (HLS .m3u8, MJPEG .mjpeg, MPEG-DASH .mpd, or matching content-types).
        3. Extract ``currentSrc`` / ``src`` from ``<video>`` and ``<source>`` DOM
           elements — many players set these attributes without a network request.
        4. Try clicking common play-button selectors to trigger stream initialisation.
        5. Wait up to ``timeout_s`` seconds for stream URLs to appear in network traffic.
        6. Check rendered page text for offline / unavailable markers.
        7. Return (first_hls_url, False) | (first_other_url, False) | (None, is_offline).

        Args:
            browser:   Shared Playwright Browser instance.
            page_url:  HTML page to open.
            sem:       Semaphore bounding concurrent browser sessions.
            timeout_s: Maximum seconds to wait for a stream URL after click.

        Returns:
            Tuple of (direct stream URL or None, offline flag).
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
                    resp_url = response.url
                    ct = response.headers.get("content-type", "").lower()
                    if (
                        _HLS_RE.search(resp_url)
                        or _MJPEG_RE.search(resp_url)
                        or _DASH_RE.search(resp_url)
                        or any(k in ct for k in _STREAM_CONTENT_TYPES)
                    ):
                        found.append(resp_url)
                        logger.debug(
                            "BrowserValidationSkill: intercepted stream {} on {}",
                            resp_url, page_url,
                        )
                except Exception:
                    pass

            page.on("response", on_response)
            is_offline = False

            try:
                # Prefer networkidle — waits until no network activity for 500 ms,
                # which catches async XHR/fetch calls that fire after DOM load.
                # Fall back to domcontentloaded if the page never settles.
                try:
                    await page.goto(
                        page_url,
                        wait_until="networkidle",
                        timeout=min(timeout_s * 1_000, 20_000),
                    )
                except Exception:
                    # networkidle timed out; try again with a lighter wait condition
                    try:
                        await page.goto(
                            page_url,
                            wait_until="domcontentloaded",
                            timeout=10_000,
                        )
                    except Exception as exc2:
                        logger.debug(
                            "BrowserValidationSkill: navigation error for {}: {}",
                            page_url, str(exc2)[:120],
                        )

                # ── DOM extraction: currentSrc / src on <video> and <source> ──
                # Many players set the video src attribute without making a new
                # network request that the response interceptor would catch.
                try:
                    dom_urls: list[str] = await page.eval_on_selector_all(
                        "video, video source, source",
                        """els => els.flatMap(el => [
                            el.currentSrc,
                            el.src,
                            el.getAttribute('src'),
                            el.getAttribute('data-src'),
                            el.getAttribute('data-stream'),
                            el.getAttribute('data-hls'),
                        ]).filter(u => u && u.startsWith('http'))""",
                    )
                    for dom_url in dom_urls:
                        if (
                            _HLS_RE.search(dom_url)
                            or _MJPEG_RE.search(dom_url)
                            or _DASH_RE.search(dom_url)
                        ) and dom_url not in found:
                            found.append(dom_url)
                            logger.debug(
                                "BrowserValidationSkill: found stream in DOM {} on {}",
                                dom_url, page_url,
                            )
                except Exception:
                    pass

                # ── Offline marker detection ────────────────────────────────────
                # Check the visible rendered text before attempting to click play,
                # so we avoid wasting time on cameras that are explicitly offline.
                try:
                    page_text = (await page.inner_text("body")).lower()
                    if any(marker in page_text for marker in _OFFLINE_MARKERS):
                        is_offline = True
                        logger.debug(
                            "BrowserValidationSkill: offline marker detected on {}",
                            page_url,
                        )
                except Exception:
                    pass

                # ── Click play button (skip if already offline) ─────────────────
                if not is_offline and not found:
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

                    # Wait for stream URLs to appear in network traffic after click.
                    wait_ms = min(timeout_s * 1_000, 8_000)
                    await page.wait_for_timeout(wait_ms)

            except Exception as exc:
                logger.debug(
                    "BrowserValidationSkill: page error for {}: {}",
                    page_url, str(exc)[:120],
                )
            finally:
                await context.close()

            # Prefer HLS (.m3u8) > DASH (.mpd) > MJPEG > anything else.
            hls_urls  = [u for u in found if _HLS_RE.search(u)]
            dash_urls = [u for u in found if _DASH_RE.search(u)]
            if hls_urls:
                return hls_urls[0], False
            if dash_urls:
                return dash_urls[0], False
            if found:
                return found[0], False
            return None, is_offline


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

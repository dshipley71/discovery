#!/usr/bin/env python3
"""
traversal.py — Directory traversal and feed URL extraction from webcam listing pages.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from pydantic import BaseModel

from webcam_discovery.schemas import CameraCandidate


# ── I/O Models ────────────────────────────────────────────────────────────────

class TraversalInput(BaseModel):
    """Input for directory traversal skill."""

    base_url: str
    city_filter: Optional[str] = None
    max_depth: int = 2


class TraversalOutput(BaseModel):
    """Output from directory traversal skill."""

    candidates: list[CameraCandidate]
    pages_fetched: int
    source_directory: str


class FeedExtractionInput(BaseModel):
    """Input for feed URL extraction skill."""

    page_url: str


class FeedExtractionOutput(BaseModel):
    """Output from feed URL extraction skill."""

    direct_stream_url: Optional[str] = None
    embed_url: Optional[str] = None
    feed_type_hint: Optional[str] = None
    embedded_links: list[str] = []
    """All stream/embed URLs collected from the page (direct streams + iframes + YouTube).
    Used by DirectoryAgent to create sub-candidates when the page is a listing."""


# ── Patterns ──────────────────────────────────────────────────────────────────

_JW_PLAYER_RE = re.compile(
    r"""jwplayer\s*\([^)]*\)\s*\.setup\s*\(\s*\{[^}]*['"]file['"]\s*:\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.DOTALL,
)
_HLS_LOAD_RE = re.compile(
    r"""[Hh]ls\.loadSource\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_VIDEOJS_RE = re.compile(
    r"""data-setup\s*=\s*['"][^'"]*"src"\s*:\s*"([^"]+)""",
    re.IGNORECASE,
)
_STREAM_VAR_RE = re.compile(
    r"""(?:streamUrl|hlsUrl|videoSrc|stream_url|hls_url)\s*[=:]\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_DATA_CAM_RE = re.compile(r"""data-cam-url\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_DATA_STREAM_RE = re.compile(r"""data-stream\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)
_DATA_SRC_RE = re.compile(r"""data-src\s*=\s*['"]([^'"]+\.(?:m3u8|mjpg|mjpeg)[^'"]*)['"]""", re.IGNORECASE)

_STREAM_EXTENSIONS = re.compile(r"\.(m3u8|mjpg|mjpeg)(\?|$)", re.IGNORECASE)
_YOUTUBE_EMBED_RE  = re.compile(r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]+)", re.IGNORECASE)

_NEXT_PAGE_RE = re.compile(r"""(?:href|src)\s*=\s*['"]([^'"]*(?:page[=/]\d+|next|p=\d+)[^'"]*)['"]""", re.IGNORECASE)

# Patterns for iframe live-video verification
_LIVE_PLAYER_RE = re.compile(
    r"(?:\.m3u8|multipart/x-mixed-replace|hls\.loadSource|jwplayer\s*\("
    r"|(?<!\w)videojs\s*\(|Hls\.js|flowplayer\s*\(|data-setup\s*=)",
    re.IGNORECASE,
)


def _extract_domain(url: str) -> str:
    """Return the netloc of a URL as a simple string."""
    parsed = urlparse(url)
    return parsed.netloc or url


def _absolute(url: str, base: str) -> str:
    """Resolve a potentially-relative URL against base."""
    return urljoin(base, url)


def _youtube_autoplay_url(url: str) -> str:
    """
    Add autoplay=1 and mute=1 to a YouTube embed URL.

    autoplay=1 triggers immediate playback; mute=1 satisfies browser autoplay
    policies that block unmuted autoplay.
    """
    parsed = urlparse(url)
    params: dict[str, str] = {}
    if parsed.query:
        for part in parsed.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = v
    params["autoplay"] = "1"
    params["mute"] = "1"
    new_query = urlencode(params)
    return urlunparse(parsed._replace(query=new_query))


# ── DirectoryTraversalSkill ────────────────────────────────────────────────────

class DirectoryTraversalSkill:
    """Systematically enumerate all camera listings from a public webcam directory."""

    async def run(self, input: TraversalInput) -> TraversalOutput:
        """
        Fetch directory index, find city subpages, and extract camera entries.

        Args:
            input: TraversalInput with base_url, optional city_filter, max_depth.

        Returns:
            TraversalOutput with candidates list and fetch statistics.
        """
        source_directory = _extract_domain(input.base_url)
        candidates: list[CameraCandidate] = []
        pages_fetched = 0
        visited: set[str] = set()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            max_redirects=3,
            headers={"User-Agent": "WebcamDiscoveryBot/1.0 (+https://github.com/webcam-discovery)"},
        ) as client:
            pages_fetched_ref = [0]
            new_candidates = await self._fetch_page(
                client,
                input.base_url,
                source_directory,
                input.city_filter,
                input.max_depth,
                visited,
                pages_fetched_ref,
            )
            candidates.extend(new_candidates)
            pages_fetched = pages_fetched_ref[0]

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_candidates: list[CameraCandidate] = []
        for c in candidates:
            if c.url not in seen_urls:
                seen_urls.add(c.url)
                unique_candidates.append(c)

        logger.info(
            "DirectoryTraversalSkill: {} candidates from {} ({} pages)",
            len(unique_candidates), source_directory, pages_fetched,
        )
        return TraversalOutput(
            candidates=unique_candidates,
            pages_fetched=pages_fetched,
            source_directory=source_directory,
        )

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        source_directory: str,
        city_filter: Optional[str],
        depth: int,
        visited: set[str],
        pages_fetched_ref: list[int],
    ) -> list[CameraCandidate]:
        """Fetch a single directory page and extract camera candidates."""
        if url in visited or depth < 0:
            return []
        visited.add(url)

        html = await self._get_with_retry(client, url)
        if html is None:
            return []
        pages_fetched_ref[0] += 1

        soup = BeautifulSoup(html, "html.parser")
        candidates: list[CameraCandidate] = []

        # Extract camera links from this page
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            abs_href = _absolute(href, url)
            text = link.get_text(strip=True)

            # Only follow links within the same domain
            if _extract_domain(abs_href) != source_directory:
                continue

            # City filter
            if city_filter and city_filter.lower() not in abs_href.lower() and city_filter.lower() not in text.lower():
                continue

            # Extract city/country from URL path segments
            path_parts = urlparse(abs_href).path.strip("/").split("/")
            city = None
            country = None
            label = text or None

            if len(path_parts) >= 3:
                country = path_parts[-2].replace("-", " ").title() if len(path_parts) >= 3 else None
                city = path_parts[-1].replace("-", " ").title()
            elif len(path_parts) == 2:
                city = path_parts[-1].replace("-", " ").title()

            # Look for media stream indicators in the linked page context
            parent = link.find_parent()
            img_src = None
            if parent:
                img = parent.find("img")
                if img and img.get("src"):
                    img_src = _absolute(str(img["src"]), url)

            candidates.append(CameraCandidate(
                url=abs_href,
                label=label,
                city=city,
                country=country,
                source_directory=source_directory,
                source_refs=[url],
                notes=f"thumbnail:{img_src}" if img_src else None,
            ))

        # Follow pagination links if depth > 0
        if depth > 0:
            next_links = self._find_next_links(soup, url, source_directory)
            for next_url in next_links[:5]:  # Limit pagination depth
                if next_url not in visited:
                    sub_candidates = await self._fetch_page(
                        client, next_url, source_directory,
                        city_filter, depth - 1, visited, pages_fetched_ref
                    )
                    candidates.extend(sub_candidates)

        return candidates

    def _find_next_links(self, soup: BeautifulSoup, base_url: str, source_domain: str) -> list[str]:
        """Find pagination 'next page' links."""
        next_links: list[str] = []
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            text = link.get_text(strip=True).lower()
            abs_href = _absolute(href, base_url)
            if _extract_domain(abs_href) != source_domain:
                continue
            if any(kw in text for kw in ("next", "›", "»", "more", "следующая")):
                next_links.append(abs_href)
            elif re.search(r"[?&]page=\d+|/page/\d+", href):
                next_links.append(abs_href)
        return next_links

    async def _get_with_retry(
        self, client: httpx.AsyncClient, url: str, retries: int = 3
    ) -> Optional[str]:
        """Fetch URL with retry on 429 and error handling."""
        for attempt in range(retries):
            try:
                response = await client.get(url)
                if response.status_code == 429:
                    wait = 30 * (attempt + 1)
                    logger.warning("Rate limited on {} — waiting {}s (attempt {}/{})", url, wait, attempt + 1, retries)
                    await asyncio.sleep(wait)
                    continue
                if response.status_code in (403, 404):
                    logger.debug("Skipping {} — HTTP {}", url, response.status_code)
                    return None
                if response.status_code != 200:
                    logger.debug("Skipping {} — HTTP {}", url, response.status_code)
                    return None
                return response.text
            except httpx.TimeoutException:
                logger.warning("Timeout fetching {} — skipping", url)
                return None
            except Exception as exc:
                logger.warning("Error fetching {}: {}", url, exc)
                return None
        return None


# ── FeedExtractionSkill ────────────────────────────────────────────────────────

class FeedExtractionSkill:
    """Extract raw stream URLs from embed or player pages."""

    async def run(self, input: FeedExtractionInput) -> FeedExtractionOutput:
        """
        Fetch a page, extract stream/embed URLs, then verify non-YouTube iframes.

        Generic iframe URLs are fetched and checked for live-player patterns or
        direct stream URLs before being included as candidates.  This prevents
        plain HTML pages (that happen to contain iframes) from flooding the
        pipeline with non-camera links.

        Args:
            input: FeedExtractionInput with page_url.

        Returns:
            FeedExtractionOutput with verified stream/embed URLs.
        """
        url = input.page_url
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
                headers={"User-Agent": "WebcamDiscoveryBot/1.0"},
            ) as client:
                response = await client.get(url)
                if response.status_code != 200:
                    return FeedExtractionOutput(embed_url=url, feed_type_hint="iframe")
                html = response.text

                result = self._extract_from_html(html, url)

                # Verify each embedded link:
                #   - direct stream URLs (.m3u8 / .mjpeg) → always accept
                #   - YouTube embeds (already normalised to autoplay) → always accept
                #   - generic iframe URLs → fetch and check for live-player patterns
                verified: list[str] = []
                for link in result.embedded_links:
                    if _STREAM_EXTENSIONS.search(link) or _YOUTUBE_EMBED_RE.search(link):
                        verified.append(link)
                    elif await self._iframe_has_live_video(client, link):
                        verified.append(link)
                    else:
                        logger.debug(
                            "FeedExtractionSkill: dropping non-live iframe {}", link
                        )

                if verified == result.embedded_links:
                    return result  # nothing filtered — return unchanged

                # Rebuild output with only verified links
                direct_streams = [l for l in verified if _STREAM_EXTENSIONS.search(l)]
                embed_list     = [l for l in verified if not _STREAM_EXTENSIONS.search(l)]
                best_direct = direct_streams[0] if direct_streams else None
                best_embed  = next(
                    (u for u in embed_list if _YOUTUBE_EMBED_RE.search(u)),
                    embed_list[0] if embed_list else None,
                )
                return FeedExtractionOutput(
                    direct_stream_url=best_direct,
                    embed_url=best_embed if best_embed else (url if not best_direct else None),
                    feed_type_hint=self._guess_feed_type(best_direct or best_embed or ""),
                    embedded_links=verified,
                )

        except httpx.TimeoutException:
            logger.warning("FeedExtractionSkill timeout: {}", url)
            return FeedExtractionOutput(embed_url=url, feed_type_hint="iframe")
        except Exception as exc:
            logger.warning("FeedExtractionSkill error on {}: {}", url, exc)
            return FeedExtractionOutput(embed_url=url, feed_type_hint="iframe")

    async def _iframe_has_live_video(
        self, client: httpx.AsyncClient, url: str
    ) -> bool:
        """
        Return True if the URL serves live-video player markup or a direct stream.

        Fetches up to 32 KB of the response and checks for:
        - Live-player JS patterns (HLS.js, JW Player, Video.js, flowplayer, data-setup)
        - YouTube embed URLs in the page source
        - Direct stream extensions (.m3u8, .mjpeg)
        """
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return False
            text = resp.text[:32_768]
            return bool(
                _LIVE_PLAYER_RE.search(text)
                or _YOUTUBE_EMBED_RE.search(text)
                or _STREAM_EXTENSIONS.search(text)
            )
        except Exception:
            return False

    def _extract_from_html(self, html: str, base_url: str) -> FeedExtractionOutput:
        """
        Extract all stream/embed URLs from HTML content.

        Collects every match across all pattern types (finditer, not search) so
        that listing pages with multiple embedded cameras surface all of them.
        Results are stored in `embedded_links`; the single best link is promoted
        to `direct_stream_url` or `embed_url`.
        """
        soup = BeautifulSoup(html, "html.parser")
        direct_streams: list[str] = []
        embed_urls: list[str] = []

        def _add_direct(raw: str) -> None:
            abs_url = _absolute(raw, base_url) if raw.startswith("/") else raw
            if abs_url not in direct_streams:
                direct_streams.append(abs_url)

        def _add_embed(raw: str) -> None:
            abs_url = _absolute(raw, base_url)
            if abs_url not in embed_urls:
                embed_urls.append(abs_url)

        # 1. <source src="..."> with stream extensions
        for source in soup.find_all("source"):
            src = source.get("src", "")
            if src and _STREAM_EXTENSIONS.search(src):
                _add_direct(src)

        # 2. All iframes — YouTube (with autoplay) or generic embed
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if not src:
                continue
            abs_src = _absolute(src, base_url)
            if _YOUTUBE_EMBED_RE.search(abs_src):
                # Normalise YouTube embeds to autoplay so playback starts immediately
                autoplay_src = _youtube_autoplay_url(abs_src)
                if autoplay_src not in embed_urls:
                    embed_urls.append(autoplay_src)
            else:
                _add_embed(src)

        # 3. JS player patterns — collect ALL matches via finditer
        scripts = " ".join(tag.get_text() for tag in soup.find_all("script"))
        for pattern in (_JW_PLAYER_RE, _HLS_LOAD_RE, _STREAM_VAR_RE):
            for m in pattern.finditer(scripts):
                _add_direct(m.group(1))

        # 4. data-* attributes — collect ALL matches via finditer
        for pattern in (_DATA_CAM_RE, _DATA_STREAM_RE, _DATA_SRC_RE):
            for m in pattern.finditer(html):
                _add_direct(m.group(1))

        # 5. <a href> with direct stream extensions (listing pages linking to streams)
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            if _STREAM_EXTENSIONS.search(href):
                _add_direct(href)

        # Build deduplicated embedded_links (direct streams first, then embeds)
        seen: set[str] = set()
        embedded_links: list[str] = []
        for url in direct_streams + embed_urls:
            if url not in seen:
                seen.add(url)
                embedded_links.append(url)

        # Choose best single direct stream and best embed
        best_direct = direct_streams[0] if direct_streams else None
        best_embed = next(
            (u for u in embed_urls if _YOUTUBE_EMBED_RE.search(u)),
            embed_urls[0] if embed_urls else None,
        )

        if best_direct:
            logger.debug(
                "FeedExtractionSkill: {} direct stream(s) on {}", len(direct_streams), base_url
            )
            return FeedExtractionOutput(
                direct_stream_url=best_direct,
                embed_url=best_embed or base_url,
                feed_type_hint=self._guess_feed_type(best_direct),
                embedded_links=embedded_links,
            )

        if best_embed:
            logger.debug(
                "FeedExtractionSkill: {} embed(s) on {}", len(embed_urls), base_url
            )
            ft = "youtube_live" if _YOUTUBE_EMBED_RE.search(best_embed) else "iframe"
            return FeedExtractionOutput(
                embed_url=best_embed,
                feed_type_hint=ft,
                embedded_links=embedded_links,
            )

        # Fallback: return page URL itself; embedded_links may still carry useful hints
        return FeedExtractionOutput(
            embed_url=base_url,
            feed_type_hint="iframe",
            embedded_links=embedded_links,
        )

    def _guess_feed_type(self, url: str) -> str:
        """Guess feed type from URL extension."""
        lower = url.lower()
        if ".m3u8" in lower:
            return "HLS"
        if ".mjpg" in lower or ".mjpeg" in lower:
            return "MJPEG"
        if "youtube" in lower or "youtu.be" in lower:
            return "youtube_live"
        return "unknown"


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        url = sys.argv[1] if len(sys.argv) > 1 else "https://www.webcamtaxi.com/en/usa/new-york-state/"
        skill = DirectoryTraversalSkill()
        result = await skill.run(TraversalInput(base_url=url, max_depth=1))
        logger.info("Fetched {} pages, found {} candidates", result.pages_fetched, len(result.candidates))
        for c in result.candidates[:5]:
            logger.info("  {}", c.model_dump())

    asyncio.run(_main())

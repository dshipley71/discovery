#!/usr/bin/env python3
"""
traversal.py — Directory traversal and feed URL extraction from webcam listing pages.
Part of the Public Webcam Discovery System.

Traversal strategy
------------------
DirectoryTraversalSkill fetches a webcam directory's root page and recursively
follows both same-domain sub-category links and pagination links to find
individual camera pages.  max_depth=5 (default) lets it reach cameras that are
buried 4–5 URL segments deep (e.g. /en/usa/new-york-state/niagara/niagara.html).

Limits:
  MAX_PAGES_PER_SOURCE  = 100  — total HTTP fetches per source before stopping.
  MAX_SUB_LINKS_PER_PAGE = 10  — sub-category links followed per page.

FeedExtractionSkill is the companion tool that fetches individual camera pages
and returns all .m3u8/.mjpeg stream URLs found in the static HTML.
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
    max_depth: int = 5


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

# Only HLS and MJPEG are accepted as live streams; MP4 is a static video format.
_STREAM_EXTENSIONS = re.compile(r"\.(m3u8|mjpg|mjpeg)(\?|$)", re.IGNORECASE)
_YOUTUBE_EMBED_RE = re.compile(r"youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]+)", re.IGNORECASE)

_NEXT_PAGE_RE = re.compile(r"""(?:href|src)\s*=\s*['"]([^'"]*(?:page[=/]\d+|next|p=\d+)[^'"]*)['"]""", re.IGNORECASE)

# Broad patterns: catch ANY quoted .m3u8 or .mjpeg URL regardless of variable name or context.
# These supplement the specific patterns above for sites that use non-standard naming.
_BROAD_HLS_RE  = re.compile(r"""['"]([^'"]{4,500}\.m3u8[^'"]{0,100})['"]""", re.IGNORECASE)
_BROAD_MJPEG_RE = re.compile(r"""['"]([^'"]{4,500}\.mj(?:pg|peg)[^'"]{0,100})['"]""", re.IGNORECASE)

# Per-source traversal limits — prevent runaway crawls on large directories.
MAX_PAGES_PER_SOURCE   = 100   # total HTTP GETs per source URL
MAX_SUB_LINKS_PER_PAGE = 10    # sub-category links to recursively follow per page


def _extract_domain(url: str) -> str:
    """Return the netloc of a URL as a simple string."""
    parsed = urlparse(url)
    return parsed.netloc or url


def _absolute(url: str, base: str) -> str:
    """Resolve a potentially-relative URL against base."""
    return urljoin(base, url)


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
        """
        Fetch a single directory page, extract camera candidates, and recurse.

        Recursion strategy
        ------------------
        - Pagination links  → followed at the SAME depth (horizontal sweep).
        - Sub-category links → followed at depth-1 (vertical descent).
          Limited to MAX_SUB_LINKS_PER_PAGE per page; prefer shorter paths
          (broad directory nodes) over deep single-camera URLs.
        - MAX_PAGES_PER_SOURCE guards total HTTP requests across all recursion.
        """
        if url in visited or depth < 0:
            return []
        if pages_fetched_ref[0] >= MAX_PAGES_PER_SOURCE:
            logger.debug(
                "DirectoryTraversalSkill: page limit ({}) reached for {}",
                MAX_PAGES_PER_SOURCE, source_directory,
            )
            return []
        visited.add(url)

        html = await self._get_with_retry(client, url)
        if html is None:
            return []
        pages_fetched_ref[0] += 1

        soup = BeautifulSoup(html, "html.parser")
        candidates: list[CameraCandidate] = []
        sub_pages: list[tuple[int, str]] = []  # (path_depth, abs_url) for sorting

        # ── Collect all same-domain links from this page ───────────────────────
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            abs_href = _absolute(href, url)
            text = link.get_text(strip=True)

            # Same domain only
            if _extract_domain(abs_href) != source_directory:
                continue
            if abs_href in visited:
                continue

            # City filter
            if (city_filter
                    and city_filter.lower() not in abs_href.lower()
                    and city_filter.lower() not in text.lower()):
                continue

            # Skip anchors, javascript, mailto, etc.
            parsed = urlparse(abs_href)
            if parsed.scheme not in ("http", "https"):
                continue

            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
            path_depth = len(path_parts)
            city    = None
            country = None
            label   = text or None

            if path_depth >= 3:
                country = path_parts[-2].replace("-", " ").title()
                city    = path_parts[-1].replace("-", " ").title()
            elif path_depth == 2:
                city = path_parts[-1].replace("-", " ").title()
            elif path_depth == 1:
                city = path_parts[0].replace("-", " ").title()

            candidates.append(CameraCandidate(
                url=abs_href,
                label=label,
                city=city,
                country=country,
                source_directory=source_directory,
                source_refs=[url],
            ))

            # Queue sub-category pages for recursive descent (depth > 0)
            if depth > 0 and 1 <= path_depth <= 5:
                sub_pages.append((path_depth, abs_href))

        # ── Recursive descent into sub-category pages ──────────────────────────
        if depth > 0 and sub_pages:
            # Prefer shallower paths first (directory roots → cities → cameras)
            sub_pages.sort(key=lambda t: t[0])
            for _, sub_url in sub_pages[:MAX_SUB_LINKS_PER_PAGE]:
                if sub_url not in visited and pages_fetched_ref[0] < MAX_PAGES_PER_SOURCE:
                    sub_candidates = await self._fetch_page(
                        client, sub_url, source_directory,
                        city_filter, depth - 1, visited, pages_fetched_ref,
                    )
                    candidates.extend(sub_candidates)

        # ── Pagination (horizontal sweep, same depth) ──────────────────────────
        if depth > 0:
            next_links = self._find_next_links(soup, url, source_directory)
            for next_url in next_links[:5]:
                if next_url not in visited and pages_fetched_ref[0] < MAX_PAGES_PER_SOURCE:
                    sub_candidates = await self._fetch_page(
                        client, next_url, source_directory,
                        city_filter, depth, visited, pages_fetched_ref,
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
        Fetch a page and extract direct stream URL and feed type hint.

        Args:
            input: FeedExtractionInput with page_url.

        Returns:
            FeedExtractionOutput with direct_stream_url, embed_url, feed_type_hint.
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
        except httpx.TimeoutException:
            logger.warning("FeedExtractionSkill timeout: {}", url)
            return FeedExtractionOutput(embed_url=url, feed_type_hint="iframe")
        except Exception as exc:
            logger.warning("FeedExtractionSkill error on {}: {}", url, exc)
            return FeedExtractionOutput(embed_url=url, feed_type_hint="iframe")

        return self._extract_from_html(html, url)

    def _extract_from_html(self, html: str, base_url: str) -> FeedExtractionOutput:
        """
        Extract direct HLS (.m3u8) and MJPEG stream URLs from HTML content.

        Only actual stream URLs are collected — iframe embeds and YouTube links are
        ignored because they are HTML pages, not active camera feeds.  All stream
        URL matches are collected via finditer so listing pages with multiple
        embedded cameras surface all of them.  Results are stored in
        `embedded_links`; the single best stream is promoted to `direct_stream_url`.
        """
        soup = BeautifulSoup(html, "html.parser")
        direct_streams: list[str] = []

        def _add_direct(raw: str) -> None:
            if not raw:
                return
            abs_url = _absolute(raw, base_url) if not raw.startswith("http") else raw
            # Only accept HLS and MJPEG — the two live-stream formats
            if _STREAM_EXTENSIONS.search(abs_url) and abs_url not in direct_streams:
                direct_streams.append(abs_url)

        # 1. <source src="..."> with stream extensions
        for source in soup.find_all("source"):
            _add_direct(source.get("src", ""))

        # 2. JS player patterns — collect ALL matches via finditer
        scripts = " ".join(tag.get_text() for tag in soup.find_all("script"))
        for pattern in (_JW_PLAYER_RE, _HLS_LOAD_RE, _STREAM_VAR_RE):
            for m in pattern.finditer(scripts):
                _add_direct(m.group(1))

        # 3. data-* attributes — collect ALL matches via finditer
        for pattern in (_DATA_CAM_RE, _DATA_STREAM_RE, _DATA_SRC_RE):
            for m in pattern.finditer(html):
                _add_direct(m.group(1))

        # 4. Broad catch-all: any quoted .m3u8 or .mjpeg URL anywhere in HTML.
        #    Runs over the full raw HTML so it catches stream URLs in JSON blobs,
        #    window.__data__ assignments, and any non-standard variable names.
        for pattern in (_BROAD_HLS_RE, _BROAD_MJPEG_RE):
            for m in pattern.finditer(html):
                _add_direct(m.group(1))

        # 5. <a href> with direct stream extensions (listing pages linking to streams)
        for link in soup.find_all("a", href=True):
            _add_direct(str(link["href"]))

        # embedded_links contains only confirmed stream URLs (no iframes, no YouTube)
        best_direct = direct_streams[0] if direct_streams else None

        if best_direct:
            logger.debug(
                "FeedExtractionSkill: {} stream(s) on {}", len(direct_streams), base_url
            )
            return FeedExtractionOutput(
                direct_stream_url=best_direct,
                embed_url=base_url,
                feed_type_hint=self._guess_feed_type(best_direct),
                embedded_links=direct_streams,
            )

        # No stream URLs found — return empty result (page will be dropped or deep-kept)
        return FeedExtractionOutput(
            embed_url=None,
            feed_type_hint=None,
            embedded_links=[],
        )

    def _guess_feed_type(self, url: str) -> str:
        """Guess feed type from URL extension. Only HLS and MJPEG are live streams."""
        lower = url.lower()
        if ".m3u8" in lower:
            return "HLS"
        if ".mjpg" in lower or ".mjpeg" in lower:
            return "MJPEG"
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

#!/usr/bin/env python3
"""
traversal.py — Directory traversal and .m3u8 feed URL extraction from webcam listing pages.
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
and returns all .m3u8 stream URLs found in the static HTML.
Only HLS (.m3u8) streams are collected — all other URL types are ignored.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

import httpx
from bs4 import BeautifulSoup


def _make_soup(content: str) -> BeautifulSoup:
    """
    Return a BeautifulSoup for *content*, choosing the right parser automatically.

    XML documents (``<?xml …>`` or ``<rss …>`` prologues) are parsed with
    lxml's XML parser to avoid the ``XMLParsedAsHTMLWarning``.  All other
    content uses the pure-Python ``html.parser``.
    """
    stripped = content.lstrip()
    if stripped.startswith("<?xml") or stripped.startswith("<rss"):
        try:
            return BeautifulSoup(content, features="xml")
        except Exception:
            pass  # lxml not installed or parse error — fall through
    return BeautifulSoup(content, "html.parser")
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
    """All .m3u8 stream URLs collected from the page.
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
_DATA_SRC_RE = re.compile(r"""data-src\s*=\s*['"]([^'"]+\.m3u8[^'"]*)['"]""", re.IGNORECASE)

# HLS and MJPEG are both accepted as live streams.
_STREAM_EXTENSIONS = re.compile(r"\.(m3u8|mjpe?g)(\?|$)", re.IGNORECASE)

_NEXT_PAGE_RE = re.compile(r"""(?:href|src)\s*=\s*['"]([^'"]*(?:page[=/]\d+|next|p=\d+)[^'"]*)['"]""", re.IGNORECASE)

# Broad catch-all: any quoted stream URL regardless of variable name or context.
_BROAD_HLS_RE   = re.compile(r"""['"]([^'"]{4,500}\.m3u8[^'"]{0,100})['"]""",  re.IGNORECASE)
_BROAD_MJPEG_RE = re.compile(r"""['"]([^'"]{4,500}\.mjpe?g[^'"]{0,100})['"]""", re.IGNORECASE)

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

        soup = _make_soup(html)
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
    """Extract .m3u8 stream URLs from webcam player pages."""

    _CLIENT_DEFAULTS = dict(
        timeout=httpx.Timeout(5.0),
        follow_redirects=True,
        headers={"User-Agent": "WebcamDiscoveryBot/1.0"},
    )

    def __init__(self, client: Optional[httpx.AsyncClient] = None) -> None:
        """
        Initialise the skill.

        Args:
            client: A pre-created AsyncClient to reuse across many calls.
                    When None (default) a fresh client is created per ``run()`` call.
                    Pass a shared client from the caller for better throughput.
        """
        self._shared_client = client

    async def run(self, input: FeedExtractionInput) -> FeedExtractionOutput:
        """
        Fetch a page and extract .m3u8 stream URLs.

        Args:
            input: FeedExtractionInput with page_url.

        Returns:
            FeedExtractionOutput with direct_stream_url and embedded_links (.m3u8 only).
        """
        if self._shared_client is not None:
            return await self._fetch_and_extract(self._shared_client, input.page_url)

        async with httpx.AsyncClient(**self._CLIENT_DEFAULTS) as client:
            return await self._fetch_and_extract(client, input.page_url)

    async def _fetch_and_extract(
        self, client: httpx.AsyncClient, url: str
    ) -> FeedExtractionOutput:
        """Fetch ``url`` using ``client`` and extract HLS streams from the response."""
        try:
            response = await client.get(url)
            if response.status_code != 200:
                return FeedExtractionOutput()
            html = response.text
        except httpx.TimeoutException:
            logger.warning("FeedExtractionSkill timeout: {}", url)
            return FeedExtractionOutput()
        except Exception as exc:
            logger.warning("FeedExtractionSkill error on {}: {}", url, exc)
            return FeedExtractionOutput()

        return self._extract_from_html(html, url)

    def _extract_from_html(self, html: str, base_url: str) -> FeedExtractionOutput:
        """
        Extract HLS (.m3u8) stream URLs from HTML or XML content.

        Only .m3u8 URLs are collected — all other URL types are ignored.
        All matches are collected via finditer so listing pages with multiple
        embedded cameras surface all of them.  Results are stored in
        `embedded_links`; the single best stream is promoted to `direct_stream_url`.

        XML documents (e.g. XSPF playlists, Atom feeds) are parsed with lxml's
        XML parser to avoid the XMLParsedAsHTMLWarning.  Everything else falls
        back to html.parser.
        """
        soup = _make_soup(html)
        direct_streams: list[str] = []

        def _add_direct(raw: str) -> None:
            if not raw:
                return
            abs_url = _absolute(raw, base_url) if not raw.startswith("http") else raw
            if _STREAM_EXTENSIONS.search(abs_url) and abs_url not in direct_streams:
                direct_streams.append(abs_url)

        # 1. <source src="..."> with .m3u8 extension
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

        # 4. <img src="...mjpeg..."> — MJPEG streams embedded as image tags
        for img in soup.find_all("img"):
            _add_direct(img.get("src", ""))

        # 5. Broad catch-all: any quoted .m3u8 or .mjpeg URL anywhere in HTML.
        #    Runs over the full raw HTML so it catches stream URLs in JSON blobs,
        #    window.__data__ assignments, and any non-standard variable names.
        for m in _BROAD_HLS_RE.finditer(html):
            _add_direct(m.group(1))
        for m in _BROAD_MJPEG_RE.finditer(html):
            _add_direct(m.group(1))

        # 6. <a href> with stream extension (listing pages linking directly to streams)
        for link in soup.find_all("a", href=True):
            _add_direct(str(link["href"]))

        best_direct = direct_streams[0] if direct_streams else None
        feed_hint = None
        if best_direct:
            feed_hint = "MJPEG" if _STREAM_EXTENSIONS.search(best_direct) and "mjpe" in best_direct.lower() else "HLS"

        if best_direct:
            return FeedExtractionOutput(
                direct_stream_url=best_direct,
                embed_url=base_url,
                feed_type_hint=feed_hint,
                embedded_links=direct_streams,
            )

        return FeedExtractionOutput(
            embed_url=None,
            feed_type_hint=None,
            embedded_links=[],
        )


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

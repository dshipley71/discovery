#!/usr/bin/env python3
"""
traversal.py — Directory traversal and .m3u8 feed URL extraction from webcam listing pages.
Part of the Public Webcam Discovery System.

Fixes applied (2026-03-23)
--------------------------
FIX: Player-wrapper URL unwrapping added to _normalize_stream_url().

     Some webcam directories (worldcams.tv, etc.) embed the real .m3u8 URL
     inside a player wrapper URL, e.g.:

       https://worldcams.tv/player?url=https://cdn.example.com/stream.m3u8

     _BROAD_HLS_RE matches the entire wrapper string because it ends in .m3u8,
     so the wrapper URL was passed to the validator unchanged.  The validator
     probed the player page (HTML), got no HLS content-type, and dropped the
     stream as dead — discarding a potentially live feed.

     unwrap_player_url() is now called early in _normalize_stream_url():
     - Inspects all query-string parameters for values that are themselves
       valid http(s) URLs containing .m3u8.
     - Returns the inner .m3u8 URL if found; otherwise returns the input
       unchanged so normal processing continues.
     - Handles URL-encoded inner URLs (urllib.parse.unquote).
     - Exported at module level so ValidationAgent can call it as a
       pre-processing step on every candidate URL it receives.

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
import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse, unquote

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

from webcam_discovery.http_client import (
    allow_insecure_ssl_fallback,
    build_async_client,
    is_ssl_cert_failure,
)
from webcam_discovery.schemas import CameraCandidate


# ── Constants ──────────────────────────────────────────────────────────────────

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Domains whose iframes should never be followed (ads, social, maps, CDN).
_SKIP_IFRAME_DOMAINS: frozenset[str] = frozenset({
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "doubleclick.net", "googlesyndication.com", "google.com",
    "maps.google.com", "openstreetmap.org", "disqus.com",
})

# ── Geographic hierarchy extraction ───────────────────────────────────────────

# ISO-639-1 language codes and common locale tags used as path prefixes by
# webcam directory sites (e.g. /en/, /it/, /zh-cn/).
_LANG_PATH_SEGMENTS: frozenset[str] = frozenset({
    "en", "it", "fr", "de", "es", "pt", "ru", "zh", "ja", "ko", "nl", "pl",
    "sv", "tr", "ar", "cs", "da", "fi", "hu", "nb", "ro", "sk", "uk", "el",
    "zh-cn", "zh-tw", "pt-br", "en-us", "en-gb", "en-au",
})

# Structural path tokens used by webcam directories that carry no geographic
# meaning and must be stripped before interpreting the path hierarchy.
_NON_GEO_PATH_SEGMENTS: frozenset[str] = frozenset({
    "webcam", "webcams", "cam", "cams", "camera", "cameras",
    "live", "stream", "streaming", "video", "watch", "view", "views", "feed",
    "world", "global",
})

# Regex: a path segment that looks like a camera-page leaf rather than a place
# (contains a file extension, or duplicates the preceding segment, etc.).
_HAS_EXTENSION_RE = re.compile(r"\.[a-zA-Z0-9]{2,5}$")


def _part_to_place(part: str) -> str:
    """Strip file extension and normalise a URL path segment to a place name."""
    part = re.sub(r"\.[^./]+$", "", part)          # remove extension (.html, .php …)
    return part.replace("-", " ").replace("_", " ").title()


def _extract_geo_hierarchy(
    path_parts: list[str],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Return ``(country, state_region, city)`` extracted from URL path parts.

    Strategy
    --------
    1. Strip a leading language/locale segment (e.g. ``en``, ``it``, ``zh-cn``).
    2. Drop non-geographic structural tokens (``webcam``, ``stream``, etc.).
    3. Strip the trailing camera-page leaf when the remaining depth is ≥ 4
       (the leaf is a specific camera page, not a place) or when it carries a
       file extension.
    4. Interpret the remaining 1–3 segments as the geographic hierarchy from
       general → specific:
         - 3+ segments: country / state-region / city
         - 2 segments:  country / city  (no state known)
         - 1 segment:   city only

    This correctly handles the following real-world URL patterns:
    - ``/en/usa/alaska/sitka/sitka-webcam.html``      → USA / Alaska / Sitka
    - ``/en/canada/british-columbia/port-alberni/``   → Canada / British Columbia / Port Alberni
    - ``/it/webcam/italia/sicilia/catania/``           → Italia / Sicilia / Catania
    - ``/usa/california/santa-monica``                 → USA / California / Santa Monica
    - ``/en/webcam/brazil/rio-de-janeiro/balneario/`` → Brazil / Rio De Janeiro / Balneario
    """
    # 1. Strip leading language segment
    geo: list[str] = []
    for i, part in enumerate(path_parts):
        low = part.lower()
        if i == 0 and low in _LANG_PATH_SEGMENTS:
            continue
        if low in _NON_GEO_PATH_SEGMENTS:
            continue
        geo.append(part)

    if not geo:
        return None, None, None

    # 2. Strip camera-leaf: the last segment is a leaf when it carries a file
    #    extension OR when we already have ≥ 4 geographic segments (country +
    #    region + city + camera-page).
    if _HAS_EXTENSION_RE.search(geo[-1]):
        geo = geo[:-1]
    elif len(geo) >= 4:
        geo = geo[:-1]   # drop camera-name segment; geography is the first 3

    if not geo:
        return None, None, None

    # 3. Assign hierarchy
    if len(geo) >= 3:
        return _part_to_place(geo[0]), _part_to_place(geo[1]), _part_to_place(geo[-1])
    elif len(geo) == 2:
        return _part_to_place(geo[0]), None, _part_to_place(geo[1])
    else:
        return None, None, _part_to_place(geo[0])


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

# Only HLS (.m3u8) streams are accepted.
_STREAM_EXTENSIONS = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)

_NEXT_PAGE_RE = re.compile(r"""(?:href|src)\s*=\s*['"]([^'"]*(?:page[=/]\d+|next|p=\d+)[^'"]*)['"]""", re.IGNORECASE)

# Broad catch-all: any quoted stream URL regardless of variable name or context.
_BROAD_HLS_RE = re.compile(r"""['"]([^'"]{4,500}\.m3u8[^'"]{0,100})['"]""", re.IGNORECASE)

# Per-source traversal limits — prevent runaway crawls on large directories.
MAX_PAGES_PER_SOURCE   = 250   # total HTTP GETs per source URL
MAX_SUB_LINKS_PER_PAGE = 10    # sub-category links to recursively follow per page

# Regex to detect an .m3u8 URL embedded in a query-string parameter value.
# Matches the inner URL whether it is raw or URL-encoded (%3A%2F%2F).
_EMBEDDED_HLS_RE = re.compile(
    r"https?(?:://|%3A%2F%2F).+?\.m3u8",
    re.IGNORECASE,
)


# ── Player-wrapper URL unwrapping ─────────────────────────────────────────────

def unwrap_player_url(url: str) -> str:
    """
    Extract an embedded .m3u8 stream URL from a player-wrapper URL if present.

    Some directories pass the real stream URL as a query-string parameter, e.g.:

        https://worldcams.tv/player?url=https://cdn.example.com/stream.m3u8
        https://example.com/embed?src=https%3A%2F%2Fcdn.example.com%2Fstream.m3u8
        https://example.com/play?stream=https://cdn.example.com/live/index.m3u8&autoplay=1

    This function inspects every query-string parameter value for an embedded
    http(s) URL that contains .m3u8.  If found, that inner URL is returned.
    If no embedded stream URL is detected the original URL is returned unchanged.

    This is exported at module level so ValidationAgent can call it as a
    pre-processing step on every candidate URL it receives.
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url

    params = parse_qs(parsed.query, keep_blank_values=False)
    for values in params.values():
        for value in values:
            # Decode URL-encoded inner URLs first
            decoded = unquote(value)
            if _STREAM_EXTENSIONS.search(decoded) and decoded.startswith("http"):
                logger.debug(
                    "unwrap_player_url: extracted inner stream '{}' from '{}'",
                    decoded, url,
                )
                return decoded

    # No embedded stream found — check for URL-encoded form in the raw query
    # string (handles double-encoded values not caught by parse_qs).
    m = _EMBEDDED_HLS_RE.search(unquote(url))
    if m:
        candidate = m.group(0)
        # Fix any residual %XX encoding on the inner URL
        candidate = unquote(candidate)
        if candidate != url:
            logger.debug(
                "unwrap_player_url: regex-extracted inner stream '{}' from '{}'",
                candidate, url,
            )
            return candidate

    return url


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_domain(url: str) -> str:
    """Return the netloc of a URL as a simple string."""
    parsed = urlparse(url)
    return parsed.netloc or url


def _absolute(url: str, base: str) -> str:
    """Resolve a potentially-relative URL against base."""
    return urljoin(base, url)


def _normalize_stream_url(raw: str, base_url: str) -> Optional[str]:
    """
    Normalise extracted stream URLs before validation.

    Step 0 (FIX): Unwrap player-wrapper URLs so the inner .m3u8 URL is
    extracted before any other normalisation takes place.

    Handles common malformed patterns seen in scraped player HTML:
    - JSON-escaped URLs such as ``https:\\/\\/cdn.example/live.m3u8``
    - protocol-relative URLs such as ``//cdn.example/live.m3u8``
    - accidentally quoted strings and whitespace
    """
    if not raw:
        return None

    candidate = raw.strip().strip("'\"")
    if not candidate:
        return None

    # ── Step 0: unwrap player-wrapper URLs ────────────────────────────────────
    # Must run before JSON-unescape so that URL-encoded inner URLs are
    # decoded correctly by unwrap_player_url().
    candidate = unwrap_player_url(candidate)

    # ── Step 1: JSON-escaped backslashes ──────────────────────────────────────
    if "\\/" in candidate:
        try:
            candidate = json.loads(f'"{candidate}"')
        except Exception:
            candidate = candidate.replace("\\/", "/")

    # ── Step 2: protocol-relative URLs ────────────────────────────────────────
    if candidate.startswith("//"):
        parsed_base = urlparse(base_url)
        scheme = parsed_base.scheme or "https"
        candidate = f"{scheme}:{candidate}"

    # ── Step 3: relative URLs ─────────────────────────────────────────────────
    if not candidate.startswith(("http://", "https://")):
        candidate = _absolute(candidate, base_url)

    return candidate.strip()


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

        async with build_async_client(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            max_redirects=3,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
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
            label      = text or None

            # Use the geo-hierarchy extractor which strips language prefixes,
            # non-geographic structural tokens (e.g. "webcam"), and camera-leaf
            # segments before assigning country / state_region / city.
            country, state_region, city = _extract_geo_hierarchy(path_parts)

            candidates.append(CameraCandidate(
                url=abs_href,
                label=label,
                city=city,
                state_region=state_region,
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
        timeout=httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=3.0),
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA},
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

        The input page_url is unwrapped via unwrap_player_url() before fetching
        so that player-wrapper URLs (e.g. /player?url=https://.../stream.m3u8)
        resolve to the inner stream directly.

        Args:
            input: FeedExtractionInput with page_url.

        Returns:
            FeedExtractionOutput with direct_stream_url and embedded_links (.m3u8 only).
        """
        # FIX: unwrap player-wrapper URLs before fetching
        page_url = unwrap_player_url(input.page_url)
        if page_url != input.page_url:
            logger.debug(
                "FeedExtractionSkill: unwrapped '{}' → '{}'",
                input.page_url, page_url,
            )

        if self._shared_client is not None:
            return await self._fetch_and_extract(self._shared_client, page_url)

        async with build_async_client(**self._CLIENT_DEFAULTS) as client:
            return await self._fetch_and_extract(client, page_url)

    async def _fetch_and_extract(
        self, client: httpx.AsyncClient, url: str
    ) -> FeedExtractionOutput:
        """
        Fetch *url* and extract stream URLs from the response.

        If the server returns a direct HLS/video content-type the URL itself
        is returned as ``direct_stream_url`` without HTML parsing.
        On timeout a single retry is attempted after a 1 s pause.
        After successful HTML retrieval, player iframes are followed one level
        deep when no stream URL was found in the page itself.
        """
        html = await self._get_html(client, url)
        if html is None:
            # If the URL itself is a direct .m3u8 (content-type was mpegurl),
            # return it as the stream directly.
            if _STREAM_EXTENSIONS.search(url):
                return FeedExtractionOutput(
                    direct_stream_url=url,
                    feed_type_hint="HLS",
                    embedded_links=[url],
                )
            return FeedExtractionOutput()

        result = self._extract_from_html(html, url)

        # If nothing found in the page HTML, follow player iframes one level.
        if not result.direct_stream_url:
            result = await self._follow_iframes(client, html, url, result)

        return result

    async def _get_html(
        self, client: httpx.AsyncClient, url: str
    ) -> Optional[str]:
        """
        GET *url* and return the response body as text.

        Returns None on non-200, timeout (after one retry), or error.
        If the content-type signals a direct stream the URL is handled by
        the caller via ``_extract_from_html`` receiving an empty string
        — callers should check the content-type before this path is needed.
        """
        for attempt in range(2):
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    return None
                ct = response.headers.get("content-type", "").lower()
                # Direct stream — content is not HTML; nothing to parse.
                if any(k in ct for k in ("mpegurl", "x-mpegurl", "vnd.apple")):
                    logger.debug("FeedExtractionSkill: direct stream content-type at {}", url)
                    return None
                return response.text
            except httpx.TimeoutException:
                if attempt == 0:
                    logger.debug("FeedExtractionSkill: timeout {}, retrying …", url)
                    await asyncio.sleep(1.0)
                else:
                    logger.warning("FeedExtractionSkill timeout (gave up): {}", url)
                    return None
            except Exception as exc:
                if is_ssl_cert_failure(exc):
                    logger.warning("SSL certificate verification failed for {}", url)
                    if not allow_insecure_ssl_fallback():
                        logger.warning("SSL fallback disabled; skipping URL")
                        return None
                    logger.warning("SSL fallback enabled; retrying public discovery URL without certificate verification")
                    try:
                        async with httpx.AsyncClient(**self._CLIENT_DEFAULTS, verify=False, trust_env=True) as insecure_client:
                            fallback_response = await insecure_client.get(url)
                        if fallback_response.status_code != 200:
                            return None
                        ct = fallback_response.headers.get("content-type", "").lower()
                        if any(k in ct for k in ("mpegurl", "x-mpegurl", "vnd.apple")):
                            logger.debug("FeedExtractionSkill: direct stream content-type at {}", url)
                            return None
                        logger.warning("SSL fallback succeeded for {}", url)
                        return fallback_response.text
                    except Exception as fallback_exc:
                        logger.warning("FeedExtractionSkill SSL fallback failed on {}: {}", url, repr(fallback_exc))
                        return None
                logger.warning("FeedExtractionSkill error on {}: {}", url, repr(exc))
                return None
        return None

    async def _follow_iframes(
        self,
        client: httpx.AsyncClient,
        html: str,
        base_url: str,
        existing: FeedExtractionOutput,
    ) -> FeedExtractionOutput:
        """
        Follow ``<iframe src>`` one level deep to find player-embedded streams.

        Many webcam directories embed a player page from a subdomain
        (e.g. ``<iframe src="https://player.example.com/embed/42">``).
        The player page contains the stream URL but the parent page does not.
        Known ad/social/map domains are skipped.
        """
        soup = _make_soup(html)
        for iframe in soup.find_all("iframe", src=True):
            src = str(iframe["src"]).strip()
            if not src or src.startswith("javascript"):
                continue
            abs_src = _absolute(src, base_url) if not src.startswith("http") else src
            # FIX: also unwrap player-wrapper iframe src URLs
            abs_src = unwrap_player_url(abs_src)
            parsed = urlparse(abs_src)
            if parsed.scheme not in ("http", "https"):
                continue
            domain = parsed.netloc.removeprefix("www.")
            if any(domain == d or domain.endswith("." + d) for d in _SKIP_IFRAME_DOMAINS):
                continue
            iframe_html = await self._get_html(client, abs_src)
            if iframe_html:
                result = self._extract_from_html(iframe_html, abs_src)
                if result.direct_stream_url:
                    logger.debug(
                        "FeedExtractionSkill: found stream via iframe {} → {}",
                        abs_src, result.direct_stream_url,
                    )
                    return result
        return existing

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
            normalized = _normalize_stream_url(raw, base_url)
            if not normalized:
                return
            if _STREAM_EXTENSIONS.search(normalized) and normalized not in direct_streams:
                direct_streams.append(normalized)

        # 1. <source src> and <video src>
        for tag in soup.find_all(["source", "video"]):
            _add_direct(tag.get("src", ""))

        # 2. JS player patterns
        scripts = " ".join(tag.get_text() for tag in soup.find_all("script"))
        for pattern in (_JW_PLAYER_RE, _HLS_LOAD_RE, _STREAM_VAR_RE):
            for m in pattern.finditer(scripts):
                _add_direct(m.group(1))

        # 3. data-* attributes
        for pattern in (_DATA_CAM_RE, _DATA_STREAM_RE, _DATA_SRC_RE):
            for m in pattern.finditer(html):
                _add_direct(m.group(1))

        # 4. Broad catch-all: any quoted .m3u8 URL anywhere in HTML.
        #    _normalize_stream_url() will unwrap any player-wrapper URLs found here.
        for m in _BROAD_HLS_RE.finditer(html):
            _add_direct(m.group(1))

        # 5. <a href> with stream extension (listing pages linking directly to streams)
        for link in soup.find_all("a", href=True):
            _add_direct(str(link["href"]))

        best_direct = direct_streams[0] if direct_streams else None
        feed_hint = "HLS" if best_direct else None

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

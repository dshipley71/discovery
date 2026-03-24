#!/usr/bin/env python3
"""
directory_crawler.py — Traverses public webcam directories and extracts camera candidates.
Part of the Public Webcam Discovery System.

Sources are loaded at runtime from SOURCES.md (project root). The file is the
canonical allow/block registry; this module never hardcodes source lists.

Discovery sources
-----------------
Directory crawl  — recursively traverses all Tier-N SOURCES.md entries up to
                   max_depth=5, following sub-category links and pagination.

Pipeline output: candidates/candidates.jsonl — one CameraCandidate JSON per line,
with `url` set to the most direct stream URL found, or an embed page URL when the
stream URL requires JavaScript to resolve (handled by the validation pipeline).
"""
from __future__ import annotations

import asyncio
import argparse
import re
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import AsyncGenerator, Optional, Sequence
from urllib.parse import urlparse

import httpx
from tqdm.asyncio import tqdm_asyncio

from loguru import logger
from pydantic import BaseModel

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.traversal import (
    DirectoryTraversalSkill,
    FeedExtractionSkill,
    FeedExtractionInput,
    TraversalInput,
    _BROWSER_UA,
)
from webcam_discovery.skills.validation import RobotsPolicySkill, RobotsPolicyInput


# ── SOURCES.md parser ─────────────────────────────────────────────────────────

class SourcesRegistry:
    """
    Parses SOURCES.md and provides source URLs by tier and a set of blocked domains.

    Searches for SOURCES.md first in the current working directory, then relative
    to this module file (project root). Falls back to an empty registry with a
    warning if the file cannot be found.
    """

    _URL_RE             = re.compile(r'\|\s*(https?://[^\s|]+)\s*\|')
    _TIER_RE            = re.compile(r'###\s+Tier\s+(\d+)', re.IGNORECASE)
    _SECTION_RE         = re.compile(r'^##\s+Section\s+(\d+)[^\n]*\n', re.IGNORECASE | re.MULTILINE)
    _BOLD_CELL_RE       = re.compile(r'^\|\s*\*\*([^*|]+)\*\*', re.MULTILINE)
    _DOMAIN_RE          = re.compile(r'^[\w.-]+\.[a-zA-Z]{2,}$')
    _FEED_TYPE_HDR_RE   = re.compile(r'feed\s+type', re.IGNORECASE)

    def __init__(self, sources_path: Optional[Path] = None) -> None:
        """
        Initialise registry.

        Args:
            sources_path: Explicit path to SOURCES.md. Auto-discovered when None.
        """
        if sources_path is None:
            for candidate in [
                Path("SOURCES.md"),
                Path(__file__).parents[3] / "SOURCES.md",
            ]:
                if candidate.exists():
                    sources_path = candidate
                    break

        self._path = sources_path
        self._tier_sources: dict[int, list[str]] = {}
        self._blocked_domains: set[str] = set()
        self._feed_types: dict[str, str] = {}  # url → raw feed-types string from SOURCES.md

        if self._path and self._path.exists():
            self._parse()
        else:
            logger.warning(
                "SourcesRegistry: SOURCES.md not found — source lists will be empty. "
                "Run from the project root or pass sources_path explicitly."
            )

    # ── Public interface ──────────────────────────────────────────────────────

    def sources_for_tier(self, max_tier: int, hls_only: bool = False) -> list[str]:
        """
        Return deduplicated source URLs for tiers 1 through max_tier (inclusive).

        Args:
            max_tier:  Highest tier to include (1 = Tier 1 only, 5 = all tiers).
            hls_only:  When True, skip any source whose SOURCES.md feed-types column
                       is known and does not include "HLS".  Sources with no feed-type
                       data are always included.

        Returns:
            Ordered list of source URLs, higher-priority tiers first.
        """
        seen: set[str] = set()
        result: list[str] = []
        for tier in range(1, max_tier + 1):
            for url in self._tier_sources.get(tier, []):
                if hls_only:
                    feed_types = self._feed_types.get(url, "")
                    if feed_types and "hls" not in feed_types.lower():
                        logger.info(
                            "SourcesRegistry: skipping non-HLS source {} (feed_types={})",
                            url, feed_types,
                        )
                        continue
                if url not in seen:
                    seen.add(url)
                    result.append(url)
        return result

    @property
    def non_hls_domains(self) -> frozenset[str]:
        """
        Domains of known sources whose feed types do not include HLS.

        Only sources with explicit feed-type data in SOURCES.md are included;
        sources with no feed-type entry are not returned here.
        """
        domains: set[str] = set()
        for url, feed_types in self._feed_types.items():
            if feed_types and "hls" not in feed_types.lower():
                netloc = urlparse(url).netloc
                if netloc.startswith("www."):
                    netloc = netloc[4:]
                if netloc:
                    domains.add(netloc)
        return frozenset(domains)

    @property
    def blocked_domains(self) -> frozenset[str]:
        """Immutable set of domain strings that must never be crawled."""
        return frozenset(self._blocked_domains)

    def tier_counts(self) -> dict[int, int]:
        """Return {tier: source_count} for diagnostics."""
        return {t: len(v) for t, v in sorted(self._tier_sources.items())}

    def source_domains_for_tier(self, max_tier: int, hls_only: bool = False) -> list[str]:
        """
        Return ordered, deduplicated source domains for tiers 1..max_tier.

        This is primarily used by SearchAgent to build site-targeted queries
        from the canonical SOURCES.md registry instead of hardcoding directory
        hostnames in multiple places.
        """
        seen: set[str] = set()
        domains: list[str] = []
        for url in self.sources_for_tier(max_tier=max_tier, hls_only=hls_only):
            domain = _domain_of(url)
            if domain and domain not in seen:
                seen.add(domain)
                domains.append(domain)
        return domains

    # ── Parsing helpers ───────────────────────────────────────────────────────

    def _parse(self) -> None:
        """Read and parse SOURCES.md into tier_sources and blocked_domains."""
        text = self._path.read_text(encoding="utf-8")

        # Split text at "## Section N" headings
        parts = self._SECTION_RE.split(text)
        # parts layout: [preamble, "1", section1_body, "2", section2_body, ...]
        sections: dict[str, str] = {}
        for i in range(1, len(parts), 2):
            sections[parts[i].strip()] = parts[i + 1] if i + 1 < len(parts) else ""

        if "1" in sections:
            self._tier_sources = self._parse_tiers(sections["1"])
        if "2" in sections:
            self._blocked_domains.update(self._parse_blocked_domains(sections["2"]))

        logger.info(
            "SourcesRegistry: loaded tiers {} ({} total sources), {} blocked domains from {}",
            dict(self.tier_counts()),
            sum(self.tier_counts().values()),
            len(self._blocked_domains),
            self._path,
        )

    def _parse_tiers(self, content: str) -> dict[int, list[str]]:
        """Extract {tier: [url, ...]} from Section 1 content, capturing feed types."""
        result: dict[int, list[str]] = {}
        current_tier = 0
        feed_type_col_idx: int = -1  # column index of "Feed Types" in current table

        for line in content.splitlines():
            tier_match = self._TIER_RE.search(line)
            if tier_match:
                current_tier = int(tier_match.group(1))
                result.setdefault(current_tier, [])
                feed_type_col_idx = -1
            elif current_tier > 0:
                # Detect "Feed Types" column header row
                if self._FEED_TYPE_HDR_RE.search(line) and "|" in line:
                    cols = [c.strip() for c in line.split("|")]
                    for idx, col in enumerate(cols):
                        if self._FEED_TYPE_HDR_RE.search(col):
                            feed_type_col_idx = idx
                            break

                url_match = self._URL_RE.search(line)
                if url_match:
                    url = url_match.group(1).rstrip("/").rstrip(")").strip()
                    if url not in result[current_tier]:
                        result[current_tier].append(url)
                    # Capture feed type when the column has been detected
                    if feed_type_col_idx >= 0:
                        cols = [c.strip() for c in line.split("|")]
                        if feed_type_col_idx < len(cols):
                            feed_types = cols[feed_type_col_idx]
                            if feed_types and feed_types != "-":
                                self._feed_types[url] = feed_types
        return result

    def _parse_blocked_domains(self, content: str) -> set[str]:
        """
        Extract blocked domains from Section 2 table rows.

        The canonical data lives in the URL column, but we also accept
        domain-like source names for defensive parsing of future edits.
        """
        blocked: set[str] = set()
        for line in content.splitlines():
            if "|" not in line or line.lstrip().startswith("|-"):
                continue

            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            if len(cells) < 2:
                continue

            _, source_url = cells[0], cells[1]

            name_match = self._BOLD_CELL_RE.match(line)
            if name_match:
                name = name_match.group(1).strip().lower()
                first_word = name.split()[0].rstrip(".,;")
                if self._DOMAIN_RE.match(first_word):
                    blocked.add(first_word)

            if source_url.startswith("http://") or source_url.startswith("https://"):
                domain = urlparse(source_url).netloc.lower().removeprefix("www.")
                if domain:
                    blocked.add(domain)
        return blocked


# ── Helpers ───────────────────────────────────────────────────────────────────

# URL path patterns that indicate a listing/navigation page rather than a
# camera page.  Matching pages are skipped in feed extraction without making
# any HTTP request.
_LISTING_PATH_RE = re.compile(
    r"/(?:tag|tags|category|categories|search|sitemap|feed|rss)(?:/|$|\?|\.)",
    re.IGNORECASE,
)

_SHALLOW_NON_CAMERA_TOKENS = frozenset({
    "about",
    "advertise",
    "advertising",
    "brand",
    "brands",
    "contact",
    "docs",
    "faq",
    "help",
    "leaflets",
    "partner",
    "partners",
    "pricing",
    "privacy",
    "resource",
    "resources",
    "support",
    "terms",
})
_COLLECTION_PATH_SEGMENTS = frozenset({
    "cameras",
    "collections",
    "galleries",
    "livecams",
    "streams",
    "webcams",
})
_DETAIL_PATH_SEGMENTS = frozenset({
    "cam",
    "camera",
    "livecam",
    "player",
    "stream",
    "view",
    "webcam",
})

# Prevent extraction from overloading any single host when a traversal yields a
# large burst of same-domain candidates.
PER_HOST_EXTRACT_CONCURRENCY = 3
FORMAT_BUCKETS: tuple[str, ...] = (
    ".m3u8",
    "RTSP",
    "MJPEG",
    "MP4-only",
    "JPEG-refresh",
    "DASH",
    "YouTube-only source",
    "Other/HTML/unknown",
)

# Language prefixes that some directories prepend to every page
# (e.g. /en/camera/x/, /ru/camera/x/, /zh-CN/camera/x/).
# Used to collapse language duplicates of the same camera page.
_LANG_PREFIX_RE = re.compile(r"^/([a-z]{2}(?:[_-][a-zA-Z]{2,4})?)/", re.ASCII)


def _domain_of(url: str) -> str:
    """Extract netloc from URL, stripping a 'www.' prefix if present."""
    netloc = urlparse(url).netloc
    return netloc.removeprefix("www.")


def _canonical_path(url: str) -> str:
    """
    Return a canonical key for *url* by stripping a leading language segment.

    ``/en/camera/usa/foo/`` and ``/ru/camera/usa/foo/`` both map to
    ``example.com/camera/usa/foo/``, so duplicates are dropped before
    feed extraction, saving many redundant HTTP requests.
    """
    parsed = urlparse(url)
    path = _LANG_PREFIX_RE.sub("/", parsed.path, count=1)
    return parsed.netloc + path + ("?" + parsed.query if parsed.query else "")


def _should_skip_feed_extraction(url: str) -> bool:
    """
    Return True when *url* is known to be a non-camera page.

    Generic listing/tag/search pages are skipped for every source. Shallow
    marketing/support pages and obvious collection routes are also skipped so
    feed extraction does not waste requests on URLs that are unlikely to be
    individual camera pages.
    """
    parsed = urlparse(url)
    path = parsed.path or "/"
    path_parts = [part.lower() for part in path.strip("/").split("/") if part]
    path_tokens = {
        token
        for part in path_parts
        for token in re.split(r"[-_]+", part)
        if token
    }

    if _LISTING_PATH_RE.search(path):
        return True

    if 0 < len(path_parts) <= 2 and path_tokens & _SHALLOW_NON_CAMERA_TOKENS:
        return True

    collection_segments = set(path_parts[:-1]) & _COLLECTION_PATH_SEGMENTS
    detail_segments = set(path_parts) & _DETAIL_PATH_SEGMENTS
    if collection_segments and not detail_segments:
        return True

    return False


def _classify_camera_format(url: str) -> str:
    """
    Classify candidate URL into the requested transport/media buckets.

    This is URL-pattern based because DirectoryAgent candidates are pre-validation
    hints; they may still be embed pages when no direct stream URL is discoverable.
    """
    lowered = url.lower()
    parsed = urlparse(lowered)

    if parsed.scheme in {"rtsp", "rtsps"}:
        return "RTSP"
    if lowered.endswith(".m3u8") or ".m3u8?" in lowered:
        return ".m3u8"
    if lowered.endswith(".mpd") or ".mpd?" in lowered or "/dash/" in lowered:
        return "DASH"
    if "youtube.com" in parsed.netloc or "youtu.be" in parsed.netloc:
        return "YouTube-only source"
    if any(token in lowered for token in ("mjpeg", ".mjpg", ".mjpeg", "multipart/x-mixed-replace")):
        return "MJPEG"
    if lowered.endswith(".mp4") or ".mp4?" in lowered:
        return "MP4-only"
    if any(
        lowered.endswith(ext) or f"{ext}?" in lowered
        for ext in (".jpg", ".jpeg")
    ) or "snapshot" in lowered or "refresh" in lowered:
        return "JPEG-refresh"
    return "Other/HTML/unknown"


async def _classify_camera_format_with_probe(
    client: httpx.AsyncClient, candidate: CameraCandidate
) -> str:
    """
    Determine format with a lightweight network probe for non-obvious URLs.

    Strategy:
    1) URL-pattern fast path.
    2) HEAD request content-type.
    3) GET range bytes sniff for raw HLS/DASH signatures.
    4) Full HTML scan for embedded .m3u8 URL patterns (catches JS player pages).
    """
    initial = _classify_camera_format(candidate.url)
    if initial != "Other/HTML/unknown":
        return initial

    try:
        head = await client.head(candidate.url)
        content_type = (head.headers.get("content-type") or "").lower()
        if "application/vnd.apple.mpegurl" in content_type or "application/x-mpegurl" in content_type:
            return ".m3u8"
        if "application/dash+xml" in content_type:
            return "DASH"
        if "multipart/x-mixed-replace" in content_type or "mjpeg" in content_type:
            return "MJPEG"
        if content_type.startswith("video/mp4"):
            return "MP4-only"
        if content_type.startswith("image/jpeg"):
            return "JPEG-refresh"
    except Exception:
        pass

    try:
        get_resp = await client.get(candidate.url, headers={"Range": "bytes=0-8191"})
        body_bytes = get_resp.text[:8192]
        body_upper = body_bytes.upper()

        # Raw manifest / container markers
        if "#EXTM3U" in body_upper:
            return ".m3u8"
        if "<MPD" in body_upper:
            return "DASH"

        # Probe for a quoted .m3u8 URL embedded in HTML/JS — this is the
        # dominant pattern for webcam directory player pages where the stream
        # URL is set via a JS player initialisation call.
        # We read a larger chunk (64 KB) for this step since JS is often at
        # the bottom of the page.
        try:
            full_resp = await client.get(candidate.url)
            html_body = full_resp.text[:65536]
        except Exception:
            html_body = body_bytes

        # Quoted .m3u8 URL anywhere in the page source
        if re.search(r"""['"][^'"]{4,500}\.m3u8[^'"]{0,100}['"]""", html_body, re.IGNORECASE):
            return ".m3u8"
        # RTSP URL in page source
        if re.search(r"""['"]rtsp[s]?://[^'"]{4,400}['"]""", html_body, re.IGNORECASE):
            return "RTSP"
        # MJPEG signature in page
        if re.search(r"""multipart/x-mixed-replace|\.mjpg['"?]|\.mjpeg['"?]""", html_body, re.IGNORECASE):
            return "MJPEG"
        # DASH manifest URL in page
        if re.search(r"""['"][^'"]{4,500}\.mpd[^'"]{0,100}['"]""", html_body, re.IGNORECASE):
            return "DASH"
        # MP4 stream URL in page
        if re.search(r"""['"][^'"]{4,500}\.mp4[^'"]{0,100}['"]""", html_body, re.IGNORECASE):
            return "MP4-only"
        # JPEG snapshot/refresh pattern
        if re.search(r"""['"][^'"]{4,500}\.jpe?g[^'"]{0,100}['"]|snapshot|getimage""",
                     html_body, re.IGNORECASE):
            if "snapshot" in html_body.lower() or "getimage" in html_body.lower():
                return "JPEG-refresh"
    except Exception:
        pass

    return initial


async def _resolve_report_formats(candidates: list[CameraCandidate]) -> list[str]:
    """Return one format bucket per candidate using URL + HTTP probe classification."""
    sem = asyncio.Semaphore(20)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=3.0, read=6.0, write=4.0, pool=3.0),
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA},
    ) as client:
        async def _classify(candidate: CameraCandidate) -> str:
            async with sem:
                return await _classify_camera_format_with_probe(client, candidate)
        return await asyncio.gather(*[_classify(c) for c in candidates])


def _render_format_breakdown_html(
    candidates: list[CameraCandidate], formats: Sequence[str]
) -> str:
    """Render per-city/state/country format counts as a single HTML table.

    Columns: City | State/Region | Country | <format buckets> | Total

    Geographic data is drawn from the ``city``, ``state_region``, and
    ``country`` fields on each CameraCandidate.  When ``state_region`` is
    absent but ``country`` looks like a sub-national region (e.g. a US state
    or Canadian province), it is promoted to ``state_region`` and ``country``
    is left blank so the report renders correctly without needing a full
    geo-enrichment pass.
    """
    # Known sub-national regions that should never appear in the Country column.
    # These are produced when older candidates were crawled before the traversal
    # fix and the country slot holds a state/province instead of a sovereign nation.
    _US_STATES: frozenset[str] = frozenset({
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
        "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
        "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming", "District Of Columbia", "Dc",
    })
    _CA_PROVINCES: frozenset[str] = frozenset({
        "Alberta", "British Columbia", "Manitoba", "New Brunswick",
        "Newfoundland And Labrador", "Northwest Territories", "Nova Scotia",
        "Nunavut", "Ontario", "Prince Edward Island", "Quebec", "Saskatchewan",
        "Yukon",
    })
    _IT_REGIONS: frozenset[str] = frozenset({
        "Abruzzo", "Basilicata", "Calabria", "Campania", "Emilia Romagna",
        "Friuli Venezia Giulia", "Lazio", "Liguria", "Lombardia", "Lombardy",
        "Marche", "Molise", "Piemonte", "Puglia", "Sardegna", "Sardinia",
        "Sicilia", "Sicily", "Toscana", "Tuscany", "Trentino Alto Adige",
        "Umbria", "Valle D Aosta", "Veneto",
        # Italian province capitals that appear as "country" in pre-fix candidates
        "Agrigento", "Bari", "Belluno", "Bologna", "Bolzano", "Caltanissetta",
        "Campobasso", "Catania", "Catanzaro", "Cosenza", "Crotone", "Firenze",
        "Foggia", "Genova", "Messina", "Milano", "Napoli", "Roma", "Salerno",
        "Trento", "Venezia", "Verona",
    })
    _ES_REGIONS: frozenset[str] = frozenset({
        "Alicante", "Almeria", "Murcia", "Cataluna", "Catalonia", "Catalunya",
        "Andalucia", "Galicia", "Aragon", "Castilla", "Valencia",
        "Islas Baleares", "Canarias", "Extremadura", "La Rioja", "Navarra",
        "Pais Vasco", "Cantabria", "Asturias",
        # Province name appearing as country in pre-fix data
        "Santa Cruz De Tenerife",
    })
    # Build a reverse map: sub-national region → sovereign country
    _REGION_TO_COUNTRY: dict[str, str] = {}
    for _r in _US_STATES:    _REGION_TO_COUNTRY[_r] = "United States"
    for _r in _CA_PROVINCES: _REGION_TO_COUNTRY[_r] = "Canada"
    for _r in _IT_REGIONS:   _REGION_TO_COUNTRY[_r] = "Italy"
    for _r in _ES_REGIONS:   _REGION_TO_COUNTRY[_r] = "Spain"
    _SUB_NATIONAL: frozenset[str] = (
        _US_STATES | _CA_PROVINCES | _IT_REGIONS | _ES_REGIONS
    )
    # Normalize known alt-spellings to canonical country names
    _COUNTRY_NORMALIZE: dict[str, str] = {
        "Brasil": "Brazil", "Usa": "United States",
        "Uk": "United Kingdom", "Italia": "Italy",
    }

    # Known noise values that are never valid geographic data
    _GEO_NOISE: frozenset[str] = frozenset({
        "Webcam", "Webcams", "Camera", "Cameras", "Live", "Stream",
        "Live Cams Category", "Help", "Unknown", "",
    })

    # Accumulate counts per (city, state_region, country) tuple
    counts: dict[tuple[str, str, str], defaultdict[str, int]] = {}
    totals_by_bucket: defaultdict[str, int] = defaultdict(int)

    for candidate, format_bucket in zip(candidates, formats):
        city         = (candidate.city or "Unknown").strip()
        state_region = (candidate.state_region or "").strip()
        country      = (candidate.country or "Unknown").strip()

        # Normalize alt-spellings first
        country = _COUNTRY_NORMALIZE.get(country, country)

        # Promote mis-filed sub-national values from country → state_region
        # and resolve the correct sovereign country via the reverse map.
        if country in _SUB_NATIONAL and not state_region:
            state_region = country
            country = _REGION_TO_COUNTRY.get(country, "Unknown")

        # Suppress noise tokens so they don't pollute the table
        if city in _GEO_NOISE:
            city = "Unknown"
        if country in _GEO_NOISE:
            country = "Unknown"
        if state_region in _GEO_NOISE:
            state_region = ""

        key = (city, state_region, country)
        if key not in counts:
            counts[key] = defaultdict(int)
        counts[key][format_bucket] += 1
        totals_by_bucket[format_bucket] += 1

    headers = ["City", "State / Region", "Country", *FORMAT_BUCKETS, "Total"]
    lines = [
        "<!doctype html>",
        "<html lang=\"en\">",
        "<head>",
        "  <meta charset=\"utf-8\" />",
        "  <title>DirectoryAgent format breakdown</title>",
        "  <style>",
        "    body { font-family: Arial, sans-serif; margin: 16px; }",
        "    table { border-collapse: collapse; width: 100%; font-size: 14px; }",
        "    th, td { border: 1px solid #ddd; padding: 6px 8px; }",
        "    th { background: #f3f4f6; position: sticky; top: 0; }",
        "    td.num { text-align: right; font-variant-numeric: tabular-nums; }",
        "    td.geo-sub { color: #555; font-style: italic; }",
        "    tr:hover td { background: #fafafa; }",
        "  </style>",
        "</head>",
        "<body>",
        "  <h1>DirectoryAgent format breakdown</h1>",
        f"  <p>Total candidate URLs: {len(candidates)}</p>",
        "  <table>",
        "    <thead>",
        "      <tr>",
    ]
    lines.extend([f"        <th>{escape(header)}</th>" for header in headers])
    lines.extend(
        [
            "      </tr>",
            "    </thead>",
            "    <tbody>",
        ]
    )

    # Sort: Country → State/Region → City
    for city, state_region, country in sorted(
        counts.keys(), key=lambda row: (row[2], row[1], row[0])
    ):
        row_counts = counts[(city, state_region, country)]
        total = sum(row_counts.values())
        lines.append("      <tr>")
        lines.append(f"        <td>{escape(city)}</td>")
        lines.append(f"        <td class=\"geo-sub\">{escape(state_region)}</td>")
        lines.append(f"        <td>{escape(country)}</td>")
        for bucket in FORMAT_BUCKETS:
            lines.append(f"        <td class=\"num\">{row_counts.get(bucket, 0)}</td>")
        lines.append(f"        <td class=\"num\">{total}</td>")
        lines.append("      </tr>")

    grand_total = sum(totals_by_bucket.values())
    lines.append("      <tr>")
    lines.append("        <td><strong>Total</strong></td>")
    lines.append("        <td></td>")
    lines.append("        <td></td>")
    for bucket in FORMAT_BUCKETS:
        lines.append(f"        <td class=\"num\"><strong>{totals_by_bucket.get(bucket, 0)}</strong></td>")
    lines.append(f"        <td class=\"num\"><strong>{grand_total}</strong></td>")
    lines.append("      </tr>")

    lines.extend(
        [
            "    </tbody>",
            "  </table>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(lines)


# ── DirectoryAgent ────────────────────────────────────────────────────────────

class DirectoryAgent:
    """
    Traverses all public webcam directories listed in SOURCES.md and produces
    a list of CameraCandidate objects with the most direct feed URL available.

    Execution steps
    ---------------
    1. Parse SOURCES.md via SourcesRegistry to get source URLs and blocked domains.
    2. Filter blocked domains; check robots.txt concurrently for all remaining sources.
    3. Traverse each allowed source (batched, max_depth=5) via DirectoryTraversalSkill.
    4. Run FeedExtractionSkill on each candidate page to resolve direct stream URLs.
    5. Deduplicate by URL and return combined list.
    """

    BATCH_SIZE          = 5    # parallel traversal tasks
    EXTRACT_CONCURRENCY = 25   # parallel feed-extraction requests
    MAX_DEPTH           = 5    # URL depth to traverse into source directories

    async def run(self, tier: int = 1, hls_only: bool = False) -> list[CameraCandidate]:
        """
        Traverse webcam directories up to the given tier and return candidates.

        Args:
            tier:     Maximum tier to crawl (1 = Tier 1 only, 5 = all tiers 1–5).
            hls_only: When True, skip source sites whose SOURCES.md feed-types column
                      does not include "HLS", and drop any candidate whose URL does not
                      end with ``.m3u8`` (removes HTML-page and non-HLS candidates).

        Returns:
            Deduplicated list of CameraCandidate objects with resolved feed URLs.
        """
        registry = SourcesRegistry()
        sources = registry.sources_for_tier(tier, hls_only=hls_only)
        blocked = registry.blocked_domains

        if not sources:
            logger.warning("DirectoryAgent: no sources loaded for tier={}", tier)
            return []

        logger.info(
            "DirectoryAgent: tier={} → {} sources across {} tier(s)",
            tier, len(sources), tier,
        )

        # ── Step 1: filter blocked domains ────────────────────────────────────
        filtered: list[str] = []
        for url in sources:
            domain = _domain_of(url)
            if any(domain == b or domain.endswith("." + b) for b in blocked):
                logger.info("DirectoryAgent: skipping blocked source {}", domain)
            else:
                filtered.append(url)

        # ── Step 2: robots.txt checks (concurrent) ────────────────────────────
        robots_skill = RobotsPolicySkill()

        async def _check_robots(source_url: str) -> Optional[str]:
            domain = _domain_of(source_url)
            try:
                result = await robots_skill.run(RobotsPolicyInput(domain=domain))
                if result.allowed:
                    return source_url
                logger.debug("DirectoryAgent: robots.txt disallows {} — skipping", domain)
                return None
            except Exception as exc:
                logger.debug("DirectoryAgent: robots check error for {}: {}", domain, exc)
                return source_url  # default-allow on error

        robots_results = await asyncio.gather(*[_check_robots(url) for url in filtered])
        allowed_sources = [url for url in robots_results if url is not None]

        # ── Step 3: traverse directories ──────────────────────────────────────
        traversal_skill = DirectoryTraversalSkill()
        raw_candidates: list[CameraCandidate] = []

        for i in range(0, len(allowed_sources), self.BATCH_SIZE):
            batch = allowed_sources[i : i + self.BATCH_SIZE]
            for url in batch:
                logger.info("DirectoryAgent: crawling {}", _domain_of(url))
            tasks = [
                traversal_skill.run(TraversalInput(base_url=url, max_depth=self.MAX_DEPTH))
                for url in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result, source_url in zip(results, batch):
                if isinstance(result, Exception):
                    logger.warning(
                        "DirectoryAgent: traversal error for {}: {}", source_url, result
                    )
                else:
                    raw_candidates.extend(result.candidates)

        # ── Step 4: resolve direct feed URLs via FeedExtractionSkill ──────────
        resolved = await self._resolve_feed_urls(raw_candidates)

        # ── Step 5: deduplicate by URL ─────────────────────────────────────────
        seen_urls: set[str] = set()
        unique: list[CameraCandidate] = []
        for c in resolved:
            if c.url not in seen_urls:
                seen_urls.add(c.url)
                unique.append(c)

        # ── Step 6: drop non-HLS URLs when hls_only is set ───────────────────
        if hls_only:
            _hls_re = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)
            before  = len(unique)
            unique  = [c for c in unique if _hls_re.search(c.url)]
            logger.info(
                "DirectoryAgent: hls_only=True — kept {} / {} candidates (.m3u8 only)",
                len(unique), before,
            )

        logger.info(
            "DirectoryAgent: tier={} → {} unique candidates",
            tier, len(unique),
        )
        return unique

    async def stream(
        self, tier: int = 1, hls_only: bool = False
    ) -> AsyncGenerator[CameraCandidate, None]:
        """
        Yield CameraCandidate objects incrementally as each source batch completes.

        This is the streaming counterpart to ``run()``.  The caller receives
        candidates as soon as each group of BATCH_SIZE sources is traversed and
        feed-extracted, so downstream validation can begin while the remaining
        sources are still being crawled.

        Args:
            tier:     Maximum tier to crawl (1 = Tier 1 only, 5 = all tiers 1–5).
            hls_only: When True, skip non-HLS source sites and drop non-.m3u8 URLs.

        Yields:
            CameraCandidate objects, deduplicated by URL across all emitted batches.
        """
        registry = SourcesRegistry()
        sources = registry.sources_for_tier(tier, hls_only=hls_only)
        blocked = registry.blocked_domains

        if not sources:
            logger.warning("DirectoryAgent.stream: no sources loaded for tier={}", tier)
            return

        logger.info(
            "DirectoryAgent.stream: tier={} → {} sources",
            tier, len(sources),
        )

        filtered: list[str] = []
        for url in sources:
            domain = _domain_of(url)
            if any(domain == b or domain.endswith("." + b) for b in blocked):
                logger.info("DirectoryAgent.stream: skipping blocked source {}", domain)
            else:
                filtered.append(url)

        robots_skill = RobotsPolicySkill()

        async def _check_robots(source_url: str) -> Optional[str]:
            domain = _domain_of(source_url)
            try:
                result = await robots_skill.run(RobotsPolicyInput(domain=domain))
                return source_url if result.allowed else None
            except Exception:
                return source_url  # default-allow on error

        robots_results = await asyncio.gather(*[_check_robots(url) for url in filtered])
        allowed_sources = [url for url in robots_results if url is not None]

        traversal_skill = DirectoryTraversalSkill()
        seen_urls: set[str] = set()
        _hls_re = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)

        for i in range(0, len(allowed_sources), self.BATCH_SIZE):
            batch = allowed_sources[i : i + self.BATCH_SIZE]
            for url in batch:
                logger.info("DirectoryAgent.stream: crawling {}", _domain_of(url))

            tasks = [
                traversal_skill.run(TraversalInput(base_url=url, max_depth=self.MAX_DEPTH))
                for url in batch
            ]
            raw_batch: list[CameraCandidate] = []
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result, source_url in zip(results, batch):
                if isinstance(result, Exception):
                    logger.warning(
                        "DirectoryAgent.stream: traversal error for {}: {}", source_url, result
                    )
                else:
                    raw_batch.extend(result.candidates)

            resolved = await self._resolve_feed_urls(raw_batch)

            for c in resolved:
                if hls_only and not _hls_re.search(c.url):
                    continue
                if c.url not in seen_urls:
                    seen_urls.add(c.url)
                    yield c

        logger.info(
            "DirectoryAgent.stream: finished — {} unique candidates emitted", len(seen_urls)
        )

    async def _resolve_feed_urls(
        self, candidates: list[CameraCandidate]
    ) -> list[CameraCandidate]:
        """
        Run FeedExtractionSkill on every candidate page URL and convert results
        into the most direct camera links possible.

        Behaviour per candidate
        -----------------------
        - direct_stream_url found  → candidate URL updated to stream URL.
        - Nothing found, url_path_depth ≥ 3  → keep as HTML embed candidate.
        - Nothing found, url_path_depth < 3  → drop (listing/nav page).
        - embedded_links non-empty → each extra link becomes an additional sub-candidate.

        A shared AsyncClient and semaphore cap concurrent HTTP requests at
        EXTRACT_CONCURRENCY; tqdm shows overall extraction progress.
        """
        # Collapse language-prefix duplicates before extraction.
        # /en/camera/usa/foo/ and /ru/camera/usa/foo/ are the same camera page;
        # keeping only one avoids redundant HTTP requests and log noise.
        _seen_canon: set[str] = set()
        deduped: list[CameraCandidate] = []
        for c in candidates:
            key = _canonical_path(c.url)
            if key not in _seen_canon:
                _seen_canon.add(key)
                deduped.append(c)
        if len(deduped) < len(candidates):
            logger.info(
                "DirectoryAgent: collapsed {} language-duplicate candidates ({} → {})",
                len(candidates) - len(deduped), len(candidates), len(deduped),
            )
        candidates = deduped

        sem = asyncio.Semaphore(self.EXTRACT_CONCURRENCY)
        streams_by_domain: defaultdict[str, int] = defaultdict(int)
        domain_limits: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(PER_HOST_EXTRACT_CONCURRENCY)
        )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=3.0),
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA},
            limits=httpx.Limits(max_connections=self.EXTRACT_CONCURRENCY + 5),
        ) as shared_client:
            skill = FeedExtractionSkill(client=shared_client)

            async def _extract(candidate: CameraCandidate) -> list[CameraCandidate]:
                page_url = candidate.url

                # Skip category/tag/listing pages without an HTTP request.
                if _should_skip_feed_extraction(page_url):
                    return []

                domain_sem = domain_limits[_domain_of(page_url)]
                async with sem:
                    async with domain_sem:
                        try:
                            feed = await skill.run(FeedExtractionInput(page_url=page_url))
                        except Exception as exc:
                            logger.warning(
                                "DirectoryAgent: feed extraction error for {}: {}", page_url, exc
                            )
                            return [candidate]

                results: list[CameraCandidate] = []
                already_seen: set[str] = {page_url}
                best_url: Optional[str] = feed.direct_stream_url

                if best_url:
                    already_seen.add(best_url)
                    refs = list(candidate.source_refs)
                    if page_url not in refs:
                        refs.append(page_url)
                    results.append(
                        candidate.model_copy(update={"url": best_url, "source_refs": refs})
                    )
                    streams_by_domain[_domain_of(page_url)] += 1
                else:
                    # Keep only pages that look like specific camera pages (deep paths),
                    # not shallow listing/navigation pages.
                    url_path_depth = len(
                        [s for s in urlparse(page_url).path.strip("/").split("/") if s]
                    )
                    if url_path_depth >= 3:
                        results.append(candidate)

                for link in feed.embedded_links:
                    if link in already_seen:
                        continue
                    already_seen.add(link)
                    results.append(
                        CameraCandidate(
                            url=link,
                            label=candidate.label,
                            city=candidate.city,
                            country=candidate.country,
                            source_directory=candidate.source_directory,
                            source_refs=[page_url] + list(candidate.source_refs),
                            notes=f"embedded_in:{page_url}",
                        )
                    )

                return results if results else [candidate]

            nested = await tqdm_asyncio.gather(
                *[_extract(c) for c in candidates],
                desc="Extracting feeds",
                unit="page",
                ncols=90,
            )

        flat = [item for sublist in nested for item in sublist]

        # Per-domain stream summary
        if streams_by_domain:
            for domain, count in sorted(streams_by_domain.items(), key=lambda x: -x[1]):
                logger.info("  {}: {} stream(s) found", domain, count)

        return flat


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for the directory crawler (wcd-discover)."""
    from webcam_discovery.pipeline import configure_logging
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Traverse public webcam directories and write candidates.jsonl."
    )
    parser.add_argument(
        "--tier", type=int, default=1,
        help="Maximum source tier to crawl (1–5, default: 1). "
             "Tier N includes all sources from tiers 1 through N.",
    )
    parser.add_argument(
        "--output", type=Path,
        default=settings.candidates_dir / "candidates.jsonl",
        help="Output path for candidates.jsonl (default: candidates/candidates.jsonl)",
    )
    parser.add_argument(
        "--hls-only", action="store_true", default=False,
        help="Skip non-HLS source sites and drop all non-.m3u8 candidate URLs from output.",
    )
    parser.add_argument(
        "--format-report-html",
        type=Path,
        default=None,
        help=(
            "Optional output path for an HTML table with per-city/country "
            "counts by URL format bucket."
        ),
    )
    args = parser.parse_args()

    candidates = asyncio.run(DirectoryAgent().run(tier=args.tier, hls_only=args.hls_only))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(c.model_dump_json() for c in candidates),
        encoding="utf-8",
    )
    logger.info(
        "DirectoryAgent: wrote {} candidates → {}",
        len(candidates), args.output,
    )
    if args.format_report_html is not None:
        args.format_report_html.parent.mkdir(parents=True, exist_ok=True)
        formats = asyncio.run(_resolve_report_formats(candidates))
        args.format_report_html.write_text(
            _render_format_breakdown_html(candidates, formats),
            encoding="utf-8",
        )
        logger.info(
            "DirectoryAgent: wrote format breakdown HTML → {}",
            args.format_report_html,
        )


if __name__ == "__main__":
    main()

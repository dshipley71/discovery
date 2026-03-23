#!/usr/bin/env python3
"""
search_agent.py — Executes multi-language structured queries to discover cameras.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Iterable
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from tqdm.auto import tqdm

from webcam_discovery.config import settings
from webcam_discovery.agents.directory_crawler import SourcesRegistry
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.traversal import FeedExtractionInput, FeedExtractionSkill
from webcam_discovery.skills.search import QueryGenerationSkill, QueryGenerationInput


# ── City lists by tier ────────────────────────────────────────────────────────

TIER1_CITIES: list[str] = [
    "New York City", "London", "Tokyo", "Paris", "Sydney", "Dubai", "Singapore",
    "Hong Kong", "Los Angeles", "Chicago", "Toronto", "Berlin", "Amsterdam",
    "Barcelona", "Rome", "Madrid", "São Paulo", "Mexico City", "Seoul", "Mumbai",
    "Shanghai", "Beijing", "Istanbul", "Cairo", "Johannesburg", "Moscow",
    "Vienna", "Prague", "Budapest", "Warsaw", "Zurich", "Stockholm", "Oslo",
    "Copenhagen", "Helsinki", "Athens", "Lisbon", "Brussels", "Dublin",
]

TIER2_CITIES: list[str] = [
    "Bangkok", "Kuala Lumpur", "Jakarta", "Manila", "Ho Chi Minh City",
    "Taipei", "Osaka", "Kyoto", "Auckland", "Melbourne", "Brisbane",
    "Vancouver", "Montreal", "São Paulo", "Buenos Aires", "Lima", "Bogota",
    "Lagos", "Nairobi", "Cape Town", "Casablanca", "Tunis",
    "Reykjavik", "Tallinn", "Riga", "Vilnius", "Ljubljana", "Zagreb",
    "Sarajevo", "Skopje", "Tirana", "Baku", "Tbilisi", "Yerevan",
    "Almaty", "Tashkent", "Bishkek", "Astana",
    "Karachi", "Dhaka", "Colombo", "Kathmandu",
    "Riyadh", "Doha", "Abu Dhabi", "Kuwait City", "Muscat", "Amman", "Beirut",
    "Tel Aviv", "Baghdad", "Tehran",
    "Accra", "Dakar", "Addis Ababa", "Dar es Salaam", "Kampala",
]

_SOURCES_REGISTRY = SourcesRegistry()
BLOCKED_DOMAINS: frozenset[str] = _SOURCES_REGISTRY.blocked_domains
KNOWN_SOURCE_DOMAINS: tuple[str, ...] = tuple(
    _SOURCES_REGISTRY.source_domains_for_tier(max_tier=3, hls_only=True)
)
_HLS_RE = re.compile(r"\.m3u8(\?|$)", re.IGNORECASE)

_CITY_LANGUAGE_HINTS: dict[str, list[str]] = {
    "Tokyo": ["en", "ja"],
    "Osaka": ["en", "ja"],
    "Kyoto": ["en", "ja"],
    "Seoul": ["en", "ko"],
    "Beijing": ["en", "zh"],
    "Shanghai": ["en", "zh"],
    "Hong Kong": ["en", "zh"],
    "Taipei": ["en", "zh"],
    "Paris": ["en", "fr"],
    "Montreal": ["en", "fr"],
    "Brussels": ["en", "fr", "nl"],
    "Berlin": ["en", "de"],
    "Vienna": ["en", "de"],
    "Zurich": ["en", "de"],
    "Madrid": ["en", "es"],
    "Barcelona": ["en", "es"],
    "Mexico City": ["en", "es"],
    "Bogota": ["en", "es"],
    "Buenos Aires": ["en", "es"],
    "Lima": ["en", "es"],
    "São Paulo": ["en", "pt"],
    "Lisbon": ["en", "pt"],
    "Rome": ["en", "it"],
    "Moscow": ["en", "ru"],
    "Stockholm": ["en", "sv"],
    "Oslo": ["en", "no"],
    "Amsterdam": ["en", "nl"],
}

_CITY_TIERS: dict[int, list[str]] = {
    1: TIER1_CITIES,
    2: TIER1_CITIES + TIER2_CITIES,
}

_FIELD_ALIASES: dict[str, str] = {
    "city": "city",
    "country": "country",
    "region": "region",
    "label": "label",
    "url": "url",
    "source": "source_directory",
    "source_directory": "source_directory",
    "source_ref": "source_refs",
    "source_refs": "source_refs",
    "notes": "notes",
}


class DuckDuckGoSearchBlocked(RuntimeError):
    """Raised when DuckDuckGo is unavailable due to anti-bot or upstream blocking."""


def _normalize_location_text(value: str) -> str:
    """Normalize text for case-insensitive blocked-location matching."""
    collapsed = re.sub(r"[\W_]+", " ", value.casefold())
    return " ".join(collapsed.split())


@dataclass(slots=True)
class BlockedLocationRules:
    """Field-aware blocked location matcher for SearchAgent filtering."""

    global_terms: set[str] = field(default_factory=set)
    field_terms: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_entries(cls, entries: Iterable[str]) -> "BlockedLocationRules":
        rules = cls()
        for raw_entry in entries:
            entry = raw_entry.strip()
            if not entry or entry.startswith("#"):
                continue
            key, value = cls._parse_entry(entry)
            normalized = _normalize_location_text(value)
            if not normalized:
                continue
            if key is None:
                rules.global_terms.add(normalized)
            else:
                rules.field_terms.setdefault(key, set()).add(normalized)
        return rules

    @staticmethod
    def _parse_entry(entry: str) -> tuple[str | None, str]:
        prefix, sep, remainder = entry.partition(":")
        if not sep:
            return None, entry
        field_name = _FIELD_ALIASES.get(prefix.strip().casefold())
        if field_name is None:
            return None, entry
        return field_name, remainder.strip()

    @property
    def enabled(self) -> bool:
        """Return True when at least one blocked term is configured."""
        return bool(self.global_terms or self.field_terms)

    @property
    def count(self) -> int:
        """Return the total number of configured blocked terms."""
        return len(self.global_terms) + sum(len(values) for values in self.field_terms.values())

    def should_block(
        self,
        *,
        city: str | None = None,
        region: str | None = None,
        country: str | None = None,
        label: str | None = None,
        url: str | None = None,
        source_directory: str | None = None,
        source_refs: Iterable[str] | None = None,
        notes: str | None = None,
    ) -> bool:
        """Return True when any blocked term matches the supplied metadata."""
        if not self.enabled:
            return False

        metadata: dict[str, list[str]] = {
            "city": [city or ""],
            "region": [region or ""],
            "country": [country or ""],
            "label": [label or ""],
            "url": [url or ""],
            "source_directory": [source_directory or ""],
            "source_refs": list(source_refs or []),
            "notes": [notes or ""],
        }

        normalized_metadata = {
            field_name: [
                normalized
                for value in values
                if (normalized := _normalize_location_text(value))
            ]
            for field_name, values in metadata.items()
        }

        haystacks = [
            normalized
            for values in normalized_metadata.values()
            for normalized in values
        ]

        for term in self.global_terms:
            if any(term in haystack for haystack in haystacks):
                return True

        for field_name, terms in self.field_terms.items():
            values = normalized_metadata.get(field_name, [])
            if any(term in value for term in terms for value in values):
                return True

        return False


def _domain_of(url: str) -> str:
    """Extract domain from URL, stripping a 'www.' prefix if present."""
    return urlparse(url).netloc.removeprefix("www.")


def _is_blocked(url: str) -> bool:
    """Check if URL belongs to a blocked domain."""
    domain = _domain_of(url)
    return any(domain == blocked or domain.endswith("." + blocked) for blocked in BLOCKED_DOMAINS)


def _unwrap_duckduckgo_href(href: str) -> str:
    """Return the destination URL from a DuckDuckGo result href."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        query = parse_qs(urlparse(href).query)
        uddg = query.get("uddg", [""])[0]
        return unquote(uddg) if uddg else ""
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    uddg = query.get("uddg", [""])[0]
    return unquote(uddg) if uddg else href


def _language_codes_for_city(city: str) -> list[str]:
    """Return a small, ordered language list for *city*."""
    return _CITY_LANGUAGE_HINTS.get(city, ["en"])


async def _duckduckgo_search(
    client: httpx.AsyncClient,
    query: str,
) -> list[str]:
    """
    Execute a DuckDuckGo HTML search and return result URLs.

    Uses DuckDuckGo's HTML endpoint (no API key required).

    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; WebcamDiscoveryBot/1.0)",
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://duckduckgo.com/",
            },
        )
        if resp.status_code in {401, 403, 429}:
            raise DuckDuckGoSearchBlocked(
                f"DuckDuckGo returned HTTP {resp.status_code} for query {query!r}"
            )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.select("a.result__a, a.result__url, .result__url"):
            href = _unwrap_duckduckgo_href(a.get("href", ""))
            if href.startswith("http") and not _is_blocked(href):
                if href not in seen:
                    seen.add(href)
                    urls.append(href)
        if not urls and ("anomaly" in resp.text.lower() or "captcha" in resp.text.lower()):
            raise DuckDuckGoSearchBlocked(
                f"DuckDuckGo returned an anti-bot page for query {query!r}"
            )
        return urls
    except DuckDuckGoSearchBlocked:
        raise
    except httpx.HTTPError as exc:
        logger.warning("DuckDuckGo search failed for '{}': {}", query, repr(exc))
        return []
    except Exception as exc:
        logger.warning("DuckDuckGo parse error for '{}': {}", query, repr(exc))
        return []


class SearchAgent:
    """Executes multi-language structured queries to discover cameras not in known directories."""

    # Maximum queries to run per city to avoid rate limiting while still hitting
    # known-source site queries and a small set of locale-aware variants.
    MAX_QUERIES_PER_CITY = 8
    # Concurrent search requests
    CONCURRENCY = 5
    MAX_RESULTS_PER_QUERY = 8
    RESULT_PAGE_CONCURRENCY = 10

    def __init__(
        self,
        *,
        blocked_locations: Iterable[str] | None = None,
        blocked_locations_file: Path | None = None,
        show_progress: bool = True,
    ) -> None:
        self._duckduckgo_available = True
        entries = list(blocked_locations or [])
        if blocked_locations_file is not None:
            entries.extend(blocked_locations_file.read_text(encoding="utf-8").splitlines())
        self._blocked_locations = BlockedLocationRules.from_entries(entries)
        self._show_progress = show_progress

    @staticmethod
    def _progress_message(progress: tqdm, city: str, hls_count: int) -> str:
        """Build a short tqdm postfix string with city, HLS count, and ETA."""
        remaining = progress.total - progress.n if progress.total is not None else 0
        rate = progress.format_dict.get("rate") or 0
        eta_seconds = int(remaining / rate) if rate else 0
        return f"city={city[:20]} hls={hls_count} eta={eta_seconds}s"

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """Collect the streaming search results into a list."""
        candidates = [candidate async for candidate in self.stream(tier=tier)]

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_candidates: list[CameraCandidate] = []
        for c in candidates:
            if c.url not in seen_urls:
                seen_urls.add(c.url)
                unique_candidates.append(c)

        cities = _CITY_TIERS.get(tier, TIER1_CITIES)
        logger.info("SearchAgent: tier={} → {} unique candidates from {} cities", tier, len(unique_candidates), len(cities))
        return unique_candidates

    async def stream(self, tier: int = 1) -> AsyncGenerator[CameraCandidate, None]:
        """
        Yield CameraCandidate objects incrementally as each city's searches complete.

        This is the streaming counterpart to ``run()``.  Results are emitted
        city-by-city, allowing the caller to begin validating early candidates
        while the remaining cities are still being searched.

        Args:
            tier: City tier to search (1 = Tier 1 cities only).

        Yields:
            CameraCandidate objects, deduplicated by URL across all emitted cities.
        """
        cities = _CITY_TIERS.get(tier, TIER1_CITIES)
        query_skill = QueryGenerationSkill()
        search_semaphore = asyncio.Semaphore(self.CONCURRENCY)
        page_semaphore = asyncio.Semaphore(self.RESULT_PAGE_CONCURRENCY)
        seen_urls: set[str] = set()
        progress = tqdm(
            total=len(cities),
            desc="SearchAgent cities",
            unit="city",
            dynamic_ncols=True,
            disable=not self._show_progress,
        )

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; WebcamDiscoveryBot/1.0)"},
            ) as client:
                for city in cities:
                    progress.set_postfix_str(self._progress_message(progress, city, len(seen_urls)))
                    if self._blocked_locations.should_block(city=city):
                        logger.info("SearchAgent: skipping blocked city '{}'", city)
                        progress.update(1)
                        continue
                    if not self._duckduckgo_available:
                        logger.warning(
                            "SearchAgent.stream: stopping early because DuckDuckGo is unavailable"
                        )
                        break
                    try:
                        city_candidates = await self._search_city(
                            client, city, query_skill, search_semaphore, page_semaphore
                        )
                    except Exception as exc:
                        logger.warning("SearchAgent.stream: error for city '{}': {}", city, exc)
                        progress.update(1)
                        continue

                    emitted_for_city = 0
                    for c in city_candidates:
                        if c.url not in seen_urls:
                            seen_urls.add(c.url)
                            emitted_for_city += 1
                            progress.set_postfix_str(
                                self._progress_message(progress, city, len(seen_urls))
                            )
                            yield c
                    progress.update(1)
                    logger.info(
                        "SearchAgent: city '{}' complete — {} new HLS stream(s), {} total",
                        city,
                        emitted_for_city,
                        len(seen_urls),
                    )
        finally:
            progress.close()

        logger.info(
            "SearchAgent.stream: finished — {} unique HLS candidates emitted from {} cities",
            len(seen_urls), len(cities),
        )

    async def _search_city(
        self,
        client: httpx.AsyncClient,
        city: str,
        query_skill: QueryGenerationSkill,
        search_semaphore: asyncio.Semaphore,
        page_semaphore: asyncio.Semaphore,
    ) -> list[CameraCandidate]:
        """Generate queries for one city, search DuckDuckGo, and extract direct HLS URLs."""
        output = query_skill.run(
            QueryGenerationInput(
                city=city,
                language_codes=_language_codes_for_city(city),
                known_domains=list(KNOWN_SOURCE_DOMAINS),
            )
        )
        queries = output.queries[: self.MAX_QUERIES_PER_CITY]

        extraction_skill = FeedExtractionSkill(client=client)
        page_candidates: list[CameraCandidate] = []
        direct_candidates: list[CameraCandidate] = []
        seen_pages: set[str] = set()

        for query in queries:
            if not self._duckduckgo_available:
                break
            async with search_semaphore:
                try:
                    urls = await _duckduckgo_search(client, query)
                except DuckDuckGoSearchBlocked as exc:
                    self._duckduckgo_available = False
                    logger.warning("SearchAgent: {}", exc)
                    break
                await asyncio.sleep(0.5)  # polite delay between requests

            for url in urls[: self.MAX_RESULTS_PER_QUERY]:
                if _is_blocked(url):
                    continue
                if _HLS_RE.search(url):
                    if direct_candidate := self._direct_hls_candidate(
                        url=url,
                        city=city,
                        query=query,
                    ):
                        direct_candidates.append(direct_candidate)
                    continue
                if url not in seen_pages:
                    seen_pages.add(url)
                    page_candidates.append(
                        CameraCandidate(
                            url=url,
                            city=city,
                            source_directory=_domain_of(url),
                            source_refs=[f"query:{query}"],
                            notes=f"search_result:{query[:80]}",
                        )
                    )

        extracted = await asyncio.gather(
            *[
                self._extract_result_page(
                    candidate,
                    extraction_skill=extraction_skill,
                    page_semaphore=page_semaphore,
                )
                for candidate in page_candidates
            ],
            return_exceptions=True,
        )

        city_candidates = list(direct_candidates)
        for result, candidate in zip(extracted, page_candidates):
            if isinstance(result, Exception):
                logger.debug(
                    "SearchAgent: failed to extract streams from {}: {}",
                    candidate.url,
                    result,
                )
                continue
            city_candidates.extend(result)

        logger.debug("SearchAgent: {} candidates for '{}'", len(city_candidates), city)
        return city_candidates

    async def _extract_result_page(
        self,
        candidate: CameraCandidate,
        extraction_skill: FeedExtractionSkill,
        page_semaphore: asyncio.Semaphore,
    ) -> list[CameraCandidate]:
        """
        Fetch a search-result page and return any direct HLS links it contains.
        """
        page_url = candidate.url
        async with page_semaphore:
            feed = await extraction_skill.run(FeedExtractionInput(page_url=page_url))

        results: list[CameraCandidate] = []
        seen_urls: set[str] = set()
        for stream_url in [feed.direct_stream_url, *feed.embedded_links]:
            if not stream_url or stream_url in seen_urls or _is_blocked(stream_url):
                continue
            if not _HLS_RE.search(stream_url):
                continue
            if self._blocked_locations.should_block(
                city=candidate.city,
                label=candidate.label,
                url=stream_url,
                source_directory=candidate.source_directory,
                source_refs=[page_url, *candidate.source_refs],
                notes=candidate.notes,
            ):
                logger.info(
                    "SearchAgent: blocked HLS stream {} from page {}",
                    stream_url,
                    page_url,
                )
                continue
            seen_urls.add(stream_url)
            refs = [page_url, *candidate.source_refs]
            results.append(
                candidate.model_copy(
                    update={
                        "url": stream_url,
                        "source_directory": _domain_of(page_url),
                        "source_refs": refs,
                        "notes": f"search_result_page:{page_url}",
                    }
                )
            )
            logger.info("SearchAgent: HLS stream {}", stream_url)

        return results

    def _direct_hls_candidate(self, *, url: str, city: str, query: str) -> CameraCandidate | None:
        """Build a direct HLS candidate, unless blocked by location rules."""
        candidate = CameraCandidate(
            url=url,
            city=city,
            source_directory=_domain_of(url),
            source_refs=[f"query:{query}"],
            notes=f"search_query:{query[:80]}",
        )
        if self._blocked_locations.should_block(
            city=city,
            url=url,
            source_directory=candidate.source_directory,
            source_refs=candidate.source_refs,
            notes=candidate.notes,
        ):
            logger.info("SearchAgent: blocked HLS stream {}", url)
            return None
        logger.info("SearchAgent: HLS stream {}", url)
        return candidate


def main() -> None:
    """CLI entry point for search agent."""
    from webcam_discovery.pipeline import configure_logging
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Discover cameras via structured search queries."
    )
    parser.add_argument("--tier", type=int, default=1, help="City tier to search (default: 1)")
    parser.add_argument(
        "--output", type=Path,
        default=settings.candidates_dir / "search_candidates.jsonl",
        help="Output path for search_candidates.jsonl",
    )
    parser.add_argument(
        "--blocked-location",
        action="append",
        default=[],
        help=(
            "Blocked location term. Repeat as needed. Supports raw terms "
            "or field-aware entries like city:Paris, country:France, source:example.com."
        ),
    )
    parser.add_argument(
        "--blocked-locations-file",
        type=Path,
        help="Optional file with one blocked location rule per line.",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the SearchAgent progress bar.",
    )
    args = parser.parse_args()

    candidates = asyncio.run(
        SearchAgent(
            blocked_locations=args.blocked_location,
            blocked_locations_file=args.blocked_locations_file,
            show_progress=not args.no_progress,
        ).run(tier=args.tier)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(c.model_dump_json() for c in candidates))
    logger.info("SearchAgent: {} candidates → {}", len(candidates), args.output)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
search_agent.py — Executes multi-language structured queries to discover cameras.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import argparse
import re
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

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
            logger.warning("DuckDuckGo returned an anti-bot page for query '{}'", query)
        return urls
    except httpx.HTTPError as exc:
        logger.warning("DuckDuckGo search failed for '{}': {}", query, exc)
        return []
    except Exception as exc:
        logger.warning("DuckDuckGo parse error for '{}': {}", query, exc)
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

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """
        Generate and execute search queries for each Tier-N city.

        Args:
            tier: City tier to search (1 = Tier 1 cities only).

        Returns:
            list[CameraCandidate] — camera candidates from search results.
        """
        cities = _CITY_TIERS.get(tier, TIER1_CITIES)
        query_skill = QueryGenerationSkill()
        candidates: list[CameraCandidate] = []

        search_semaphore = asyncio.Semaphore(self.CONCURRENCY)
        page_semaphore = asyncio.Semaphore(self.RESULT_PAGE_CONCURRENCY)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WebcamDiscoveryBot/1.0)"},
        ) as client:
            tasks = [
                self._search_city(client, city, query_skill, search_semaphore, page_semaphore)
                for city in cities
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for city, result in zip(cities, results):
            if isinstance(result, Exception):
                logger.warning("SearchAgent: error for city '{}': {}", city, result)
            else:
                candidates.extend(result)

        # Deduplicate by URL
        seen_urls: set[str] = set()
        unique_candidates: list[CameraCandidate] = []
        for c in candidates:
            if c.url not in seen_urls:
                seen_urls.add(c.url)
                unique_candidates.append(c)

        logger.info(
            "SearchAgent: tier={} → {} unique candidates from {} cities",
            tier, len(unique_candidates), len(cities),
        )
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

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WebcamDiscoveryBot/1.0)"},
        ) as client:
            for city in cities:
                try:
                    city_candidates = await self._search_city(
                        client, city, query_skill, search_semaphore, page_semaphore
                    )
                except Exception as exc:
                    logger.warning("SearchAgent.stream: error for city '{}': {}", city, exc)
                    continue

                for c in city_candidates:
                    if c.url not in seen_urls:
                        seen_urls.add(c.url)
                        yield c

        logger.info(
            "SearchAgent.stream: finished — {} unique candidates emitted from {} cities",
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
            async with search_semaphore:
                urls = await _duckduckgo_search(client, query)
                await asyncio.sleep(0.5)  # polite delay between requests

            for url in urls[: self.MAX_RESULTS_PER_QUERY]:
                if _is_blocked(url):
                    continue
                if _HLS_RE.search(url):
                    direct_candidates.append(
                        CameraCandidate(
                            url=url,
                            city=city,
                            source_directory=_domain_of(url),
                            source_refs=[f"query:{query}"],
                            notes=f"search_query:{query[:80]}",
                        )
                    )
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
                logger.warning(
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

        return results


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
    args = parser.parse_args()

    candidates = asyncio.run(SearchAgent().run(tier=args.tier))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(c.model_dump_json() for c in candidates))
    logger.info("SearchAgent: {} candidates → {}", len(candidates), args.output)


if __name__ == "__main__":
    main()

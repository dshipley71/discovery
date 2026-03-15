#!/usr/bin/env python3
"""
search_agent.py — Executes multi-language structured queries to discover cameras.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.search import QueryGenerationSkill, QueryGenerationInput
from webcam_discovery.agents.directory_crawler import SourcesRegistry


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

BLOCKED_DOMAINS: set[str] = {
    "shodan.io",
    "insecam.org",
    "www.insecam.org",
    "censys.io",
    "zoomeye.org",
    "fofa.info",
}

_CITY_TIERS: dict[int, list[str]] = {
    1: TIER1_CITIES,
    2: TIER1_CITIES + TIER2_CITIES,
}


def _domain_of(url: str) -> str:
    """Extract domain from URL, stripping a 'www.' prefix if present."""
    return urlparse(url).netloc.removeprefix("www.")


def _is_blocked(url: str, extra: frozenset[str] = frozenset()) -> bool:
    """Check if URL belongs to a blocked domain or a known non-HLS source domain."""
    domain = _domain_of(url)
    all_blocked = BLOCKED_DOMAINS | extra
    return domain in all_blocked or any(b in domain for b in all_blocked)


async def _duckduckgo_search(
    client: httpx.AsyncClient,
    query: str,
    extra_blocked: frozenset[str] = frozenset(),
) -> list[str]:
    """
    Execute a DuckDuckGo HTML search and return result URLs.

    Uses DuckDuckGo's HTML endpoint (no API key required).

    Args:
        extra_blocked: Additional domains to exclude from results (e.g. non-HLS sources).
    """
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        resp = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; WebcamDiscoveryBot/1.0)",
            "Accept": "text/html",
        })
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        urls: list[str] = []
        for a in soup.select("a.result__url, a.result__a, .result__url"):
            href = a.get("href", "")
            # DuckDuckGo wraps URLs in redirects
            match = re.search(r"uddg=([^&]+)", href)
            if match:
                from urllib.parse import unquote
                href = unquote(match.group(1))
            if href.startswith("http") and not _is_blocked(href, extra_blocked):
                urls.append(href)
        return urls
    except Exception as exc:
        logger.debug("DuckDuckGo search error for '{}': {}", query, exc)
        return []


class SearchAgent:
    """Executes multi-language structured queries to discover cameras not in known directories."""

    # Maximum queries to run per city to avoid rate limiting
    MAX_QUERIES_PER_CITY = 3
    # Concurrent search requests
    CONCURRENCY = 5

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """
        Generate and execute search queries for each Tier-N city.

        Sources listed in SOURCES.md whose feed types do not include HLS are
        excluded from results, in addition to the static BLOCKED_DOMAINS list.

        Args:
            tier: City tier to search (1 = Tier 1 cities only).

        Returns:
            list[CameraCandidate] — camera candidates from search results.
        """
        cities = _CITY_TIERS.get(tier, TIER1_CITIES)
        query_skill = QueryGenerationSkill()
        candidates: list[CameraCandidate] = []

        # Exclude known non-HLS source domains from search results
        non_hls = SourcesRegistry().non_hls_domains
        if non_hls:
            logger.info("SearchAgent: excluding {} non-HLS source domains", len(non_hls))

        semaphore = asyncio.Semaphore(self.CONCURRENCY)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
        ) as client:
            tasks = [
                self._search_city(client, city, query_skill, semaphore, non_hls)
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

    async def _search_city(
        self,
        client: httpx.AsyncClient,
        city: str,
        query_skill: QueryGenerationSkill,
        semaphore: asyncio.Semaphore,
        extra_blocked: frozenset[str] = frozenset(),
    ) -> list[CameraCandidate]:
        """Generate queries for one city and search DuckDuckGo."""
        output = query_skill.run(QueryGenerationInput(city=city, language_codes=["en"]))
        # Limit queries per city to avoid rate limiting
        queries = output.queries[: self.MAX_QUERIES_PER_CITY]

        city_candidates: list[CameraCandidate] = []
        for query in queries:
            async with semaphore:
                urls = await _duckduckgo_search(client, query, extra_blocked)
                await asyncio.sleep(0.5)  # polite delay between requests

            for url in urls:
                if _is_blocked(url, extra_blocked):
                    continue
                city_candidates.append(CameraCandidate(
                    url=url,
                    city=city,
                    source_directory="search:" + _domain_of(url),
                    source_refs=[f"query:{query}"],
                    notes=f"search_query:{query[:80]}",
                ))

        logger.debug("SearchAgent: {} candidates for '{}'", len(city_candidates), city)
        return city_candidates


def main() -> None:
    """CLI entry point for search agent."""
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

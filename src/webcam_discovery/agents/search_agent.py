#!/usr/bin/env python3
"""
search_agent.py — Executes multi-language structured queries to discover cameras.
Part of the Public Webcam Discovery System.

Fixes applied (2026-03-23)
--------------------------
1. Replaced bot User-Agent + HTML scraping with duckduckgo-search library
   (handles DDG anti-bot internally; no more TCP resets / blank error messages).
2. Removed .m3u8 from query strings — search finds watch pages, FeedExtractionSkill
   extracts the stream URL from those pages.
3. JS-gated domains (EarthCam, SkylineWebcams, etc.) excluded from SearchAgent query
   path; they are handled by DirectoryAgent + Playwright instead.
4. MAX_QUERIES_PER_CITY raised to 12; locale queries now run BEFORE site: queries so
   they are never truncated.
5. Global _duckduckgo_available kill switch replaced with per-city consecutive-block
   counter + 60 s cooldown.
6. CONCURRENCY reduced to 2; random pre-query jitter (1–3 s) added inside the
   semaphore to prevent burst-rate DDG blocks.
"""
from __future__ import annotations

import asyncio
import argparse
import random
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

# FIX 3 — Domains whose stream URLs require JavaScript execution to surface.
# FeedExtractionSkill uses httpx (static HTML only) and will never find a
# .m3u8 on these pages.  Route them through DirectoryAgent + Playwright instead.
_JS_GATED_DOMAINS: frozenset[str] = frozenset({
    "earthcam.com",
    "skylinewebcams.com",
    "insecam.org",
    "camstreamer.com",
    "roundshot.com",
    "windy.com",
    "opentopia.com",
})

# Domains that serve explorable HLS directories via static HTML — safe for
# FeedExtractionSkill to process.
SEARCH_SAFE_DOMAINS: tuple[str, ...] = tuple(
    d for d in KNOWN_SOURCE_DOMAINS if d not in _JS_GATED_DOMAINS
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

# FIX 5 — How many consecutive DDG blocks before we force a cooldown pause.
_MAX_CONSECUTIVE_BLOCKS = 3
_BLOCK_COOLDOWN_SECONDS = 60


class DuckDuckGoSearchBlocked(RuntimeError):
    """Raised when DuckDuckGo is unavailable due to anti-bot or upstream blocking."""


def _domain_of(url: str) -> str:
    """Extract domain from URL, stripping a 'www.' prefix if present."""
    return urlparse(url).netloc.removeprefix("www.")


def _is_blocked(url: str) -> bool:
    """Check if URL belongs to a blocked domain."""
    domain = _domain_of(url)
    return any(domain == blocked or domain.endswith("." + blocked) for blocked in BLOCKED_DOMAINS)


def _language_codes_for_city(city: str) -> list[str]:
    """Return a small, ordered language list for *city*."""
    return _CITY_LANGUAGE_HINTS.get(city, ["en"])


# ── FIX 1 — DDG search via library, not HTML scraping ────────────────────────

async def _duckduckgo_search(query: str) -> list[str]:
    """
    Execute a DuckDuckGo search and return result URLs.

    Uses the duckduckgo-search PyPI package (DDGS) which handles DDG's
    anti-bot measures internally.  Falls back gracefully on any error.

    NOTE: DDGS.text() is synchronous; run in an executor to avoid blocking
    the event loop.
    """
    try:
        from duckduckgo_search import DDGS  # type: ignore[import]
    except ImportError:
        raise RuntimeError(
            "duckduckgo-search is not installed.  "
            "Add it to pyproject.toml: duckduckgo-search>=6.0"
        )

    loop = asyncio.get_event_loop()
    try:
        results: list[dict] = await loop.run_in_executor(
            None,
            lambda: list(DDGS().text(query, max_results=10)),
        )
        urls: list[str] = []
        for r in results:
            href = r.get("href", "")
            if href.startswith("http") and not _is_blocked(href):
                urls.append(href)
        return urls
    except DuckDuckGoSearchBlocked:
        raise
    except Exception as exc:
        # Surface the actual exception message so the log is never blank.
        err_msg = repr(exc) or type(exc).__name__
        if "ratelimit" in err_msg.lower() or "202" in err_msg or "blocked" in err_msg.lower():
            raise DuckDuckGoSearchBlocked(f"DDG blocked query {query!r}: {err_msg}") from exc
        logger.warning("DuckDuckGo search failed for '{}': {}", query, err_msg)
        return []


class SearchAgent:
    """Executes multi-language structured queries to discover cameras not in known directories."""

    # FIX 4 — Raised from 8; locale queries no longer truncated.
    MAX_QUERIES_PER_CITY = 12
    # FIX 6 — Reduced from 5; prevents burst-rate blocks.
    CONCURRENCY = 2
    MAX_RESULTS_PER_QUERY = 8
    RESULT_PAGE_CONCURRENCY = 10

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """Collect the streaming search results into a list."""
        candidates = [candidate async for candidate in self.stream(tier=tier)]

        seen_urls: set[str] = set()
        unique_candidates: list[CameraCandidate] = []
        for c in candidates:
            if c.url not in seen_urls:
                seen_urls.add(c.url)
                unique_candidates.append(c)

        cities = _CITY_TIERS.get(tier, TIER1_CITIES)
        logger.info(
            "SearchAgent: tier={} → {} unique candidates from {} cities",
            tier, len(unique_candidates), len(cities),
        )
        return unique_candidates

    async def stream(self, tier: int = 1) -> AsyncGenerator[CameraCandidate, None]:
        """
        Yield CameraCandidate objects incrementally as each city's searches complete.

        FIX 5: replaces the global _duckduckgo_available kill switch with a
        consecutive-block counter.  After _MAX_CONSECUTIVE_BLOCKS failures in a
        row we pause for _BLOCK_COOLDOWN_SECONDS then resume rather than aborting
        the entire run.
        """
        cities = _CITY_TIERS.get(tier, TIER1_CITIES)
        query_skill = QueryGenerationSkill()
        search_semaphore = asyncio.Semaphore(self.CONCURRENCY)
        page_semaphore = asyncio.Semaphore(self.RESULT_PAGE_CONCURRENCY)
        seen_urls: set[str] = set()
        consecutive_blocks = 0  # FIX 5

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            for city in cities:
                # FIX 5 — Cooldown after repeated blocks, not a permanent shutdown.
                if consecutive_blocks >= _MAX_CONSECUTIVE_BLOCKS:
                    logger.warning(
                        "SearchAgent: {} consecutive DDG blocks — cooling down {}s before '{}'",
                        consecutive_blocks, _BLOCK_COOLDOWN_SECONDS, city,
                    )
                    await asyncio.sleep(_BLOCK_COOLDOWN_SECONDS)
                    consecutive_blocks = 0

                try:
                    city_candidates = await self._search_city(
                        client, city, query_skill, search_semaphore, page_semaphore
                    )
                    consecutive_blocks = 0  # reset on success
                except DuckDuckGoSearchBlocked as exc:
                    consecutive_blocks += 1
                    logger.warning(
                        "SearchAgent: DDG blocked for '{}' (consecutive={}): {}",
                        city, consecutive_blocks, exc,
                    )
                    await asyncio.sleep(random.uniform(5.0, 15.0))
                    continue
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
        """
        Generate queries for one city, search DuckDuckGo, and extract direct HLS URLs.

        FIX 3: passes SEARCH_SAFE_DOMAINS (JS-gated sources excluded) to
                QueryGenerationSkill so site: queries only target domains whose
                pages FeedExtractionSkill can actually parse.
        FIX 4: uses MAX_QUERIES_PER_CITY=12 so locale queries are not truncated.
        FIX 6: adds random pre-query jitter inside the semaphore.
        """
        output = query_skill.run(
            QueryGenerationInput(
                city=city,
                language_codes=_language_codes_for_city(city),
                # FIX 3 — only domains whose pages FeedExtractionSkill can parse
                known_domains=list(SEARCH_SAFE_DOMAINS),
            )
        )
        queries = output.queries[: self.MAX_QUERIES_PER_CITY]

        extraction_skill = FeedExtractionSkill(client=client)
        page_candidates: list[CameraCandidate] = []
        direct_candidates: list[CameraCandidate] = []
        seen_pages: set[str] = set()

        for query in queries:
            async with search_semaphore:
                # FIX 6 — jitter BEFORE the request, inside the semaphore,
                # so concurrent coroutines don't fire simultaneously.
                await asyncio.sleep(random.uniform(1.0, 3.0))
                urls = await _duckduckgo_search(query)
                # _duckduckgo_search raises DuckDuckGoSearchBlocked — propagate
                # to stream() which owns the consecutive-block counter.

            for url in urls[: self.MAX_RESULTS_PER_QUERY]:
                if _is_blocked(url):
                    continue
                if _HLS_RE.search(url):
                    # Rare — DDG occasionally indexes .m3u8 manifests directly
                    direct_candidates.append(
                        CameraCandidate(
                            url=url,
                            city=city,
                            source_directory=_domain_of(url),
                            source_refs=[f"query:{query}"],
                            notes=f"search_direct:{query[:80]}",
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
                    candidate.url, result,
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

        Only .m3u8 URLs pass the _HLS_RE filter — all other URLs are discarded.
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

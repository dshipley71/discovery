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
from pathlib import Path
from typing import Optional
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

    # Domains that must always be blocked regardless of SOURCES.md parse quality.
    _ALWAYS_BLOCKED: frozenset[str] = frozenset({
        "shodan.io",
        "censys.io",
        "insecam.org",
        "insecam.com",
        "opentopia.com",
        "camhacker.com",
        "zoomeye.org",
        "fofa.info",
        "binaryedge.io",
        "greynoise.io",
        "pictimo.com",
    })

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
        self._blocked_domains: set[str] = set(self._ALWAYS_BLOCKED)
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
        """Extract domain-like strings from bold source names in Section 2."""
        blocked: set[str] = set()
        for match in self._BOLD_CELL_RE.finditer(content):
            name = match.group(1).strip().lower()
            first_word = name.split()[0].rstrip(".,;")
            if self._DOMAIN_RE.match(first_word):
                blocked.add(first_word)
        return blocked


# ── Helpers ───────────────────────────────────────────────────────────────────

def _domain_of(url: str) -> str:
    """Extract netloc from URL, stripping a 'www.' prefix if present."""
    netloc = urlparse(url).netloc
    return netloc.removeprefix("www.")


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

    async def run(self, tier: int = 1) -> list[CameraCandidate]:
        """
        Traverse webcam directories up to the given tier and return candidates.

        Args:
            tier: Maximum tier to crawl (1 = Tier 1 only, 5 = all tiers 1–5).

        Returns:
            Deduplicated list of CameraCandidate objects with resolved feed URLs.
        """
        registry = SourcesRegistry()
        sources = registry.sources_for_tier(tier, hls_only=True)
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

        logger.info(
            "DirectoryAgent: tier={} → {} unique candidates",
            tier, len(unique),
        )
        return unique

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
        sem = asyncio.Semaphore(self.EXTRACT_CONCURRENCY)
        streams_by_domain: defaultdict[str, int] = defaultdict(int)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
            headers={"User-Agent": "WebcamDiscoveryBot/1.0"},
            limits=httpx.Limits(max_connections=self.EXTRACT_CONCURRENCY + 5),
        ) as shared_client:
            skill = FeedExtractionSkill(client=shared_client)

            async def _extract(candidate: CameraCandidate) -> list[CameraCandidate]:
                page_url = candidate.url
                async with sem:
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
                logger.info("  {}: {} HLS stream(s) found", domain, count)

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
    args = parser.parse_args()

    candidates = asyncio.run(DirectoryAgent().run(tier=args.tier))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(c.model_dump_json() for c in candidates),
        encoding="utf-8",
    )
    logger.info(
        "DirectoryAgent: wrote {} candidates → {}",
        len(candidates), args.output,
    )


if __name__ == "__main__":
    main()

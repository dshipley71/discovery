#!/usr/bin/env python3
"""
search.py — Search query generation, locale-aware URL traversal, and new source discovery.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from pydantic import BaseModel


# ── I/O Models ────────────────────────────────────────────────────────────────

class QueryGenerationInput(BaseModel):
    """Input for query generation skill."""

    city: str
    language_codes: list[str] = ["en"]
    known_domains: list[str] = []


class QueryGenerationOutput(BaseModel):
    """Output from query generation skill."""

    queries: list[str]


class LocaleNavigationInput(BaseModel):
    """Input for locale navigation skill."""

    source_url: str
    target_language: str


class LocaleNavigationOutput(BaseModel):
    """Output from locale navigation skill."""

    camera_links: list[str]


class SourceDiscoveryInput(BaseModel):
    """Input for source discovery skill."""

    search_results: list[str]
    known_domains: list[str] = []


class SourceDiscoveryOutput(BaseModel):
    """Output from source discovery skill."""

    candidate_sources: list[dict]


# ── Language-specific search terms ────────────────────────────────────────────

_LOCALE_TERMS: dict[str, list[str]] = {
    "ja": ["ライブカメラ 公開", "観光 ライブカメラ", "国道 カメラ", "リアルタイム カメラ"],
    "de": ["Webcam öffentlich kostenlos", "Livekamera", "Straßenkamera", "Verkehrskamera"],
    "fr": ["caméra en direct gratuit", "webcam en ligne", "caméra de surveillance publique"],
    "ko": ["실시간 카메라 공개", "CCTV 공개", "교통 카메라"],
    "pt": ["câmera ao vivo", "webcam pública", "câmera de tráfego"],
    "es": ["cámara en vivo público", "webcam pública", "cámara de tráfico"],
    "sv": ["live webkamera gratis", "trafikkamera", "webbkamera"],
    "no": ["live webkamera gratis", "trafikkamera", "veikamera"],
    "nl": ["webcam live gratis", "verkeerskamera", "live camera"],
    "zh": ["实时摄像头 公开", "交通摄像头", "旅游直播"],
    "ar": ["كاميرا مباشرة عامة", "كاميرا المرور"],
    "ru": ["веб-камера публичная", "прямой эфир камера", "трафик камера"],
    "it": ["webcam dal vivo", "telecamera pubblica", "camera traffico"],
}

_LOCALE_NAVIGATION_TERMS: dict[str, list[str]] = {
    "ja": ["ライブカメラ", "観光", "国道", "カメラ一覧"],
    "de": ["Webcam", "Livekamera", "Kamera", "Live"],
    "fr": ["webcam", "caméra", "direct", "live"],
    "ko": ["카메라", "실시간", "CCTV"],
    "pt": ["câmera", "webcam", "ao vivo"],
    "es": ["cámara", "webcam", "vivo", "directo"],
    "sv": ["kamera", "webcam", "live"],
    "no": ["kamera", "webcam", "live"],
    "nl": ["camera", "webcam", "live"],
    "zh": ["摄像头", "直播", "实时"],
    "ru": ["веб-камера", "прямой", "камера"],
    "it": ["webcam", "telecamera", "live"],
}


# ── QueryGenerationSkill ───────────────────────────────────────────────────────

class QueryGenerationSkill:
    """Generate high-yield search query variants for a given city or region."""

    def run(self, input: QueryGenerationInput) -> QueryGenerationOutput:
        """
        Generate English, locale-specific, and government/infrastructure queries.

        Args:
            input: QueryGenerationInput with city and language_codes.

        Returns:
            QueryGenerationOutput with queries list.
        """
        city = input.city
        queries: list[str] = []

        # Core English queries aligned with SOURCES.md patterns.
        queries.extend([
            f'"live webcam" "{city}" ".m3u8"',
            f'"public webcam" "{city}" "hls" -login -register -subscribe',
            f'inurl:webcam OR inurl:livecam "{city}" ".m3u8"',
            f'"{city}" "traffic camera" ".m3u8"',
            f'"{city}" municipality webcam hls',
        ])

        # Known-source site queries from SOURCES.md.
        for domain in input.known_domains:
            queries.append(f'site:{domain} "{city}" webcam')

        queries.extend([
            f'"{city}" tourism webcam ".m3u8"',
            f'"{city}" harbor webcam ".m3u8"',
            f'"{city}" airport webcam hls',
            f'"{city}" transport authority ".m3u8"',
            f'"{city}" open data webcam hls',
        ])

        # Locale-specific queries
        for lang in input.language_codes:
            if lang == "en":
                continue
            terms = _LOCALE_TERMS.get(lang, [])
            for term in terms:
                queries.append(f'"{city}" {term} m3u8')

        # Government and infrastructure queries
        queries.extend([
            f'"{city}" DOT traffic camera ".m3u8"',
            f'"{city}" 511 traffic feed ".m3u8"',
            f'"{city}" national weather service camera ".m3u8"',
        ])

        deduped_queries: list[str] = []
        seen_queries: set[str] = set()
        for query in queries:
            normalized = " ".join(query.split())
            if normalized not in seen_queries:
                seen_queries.add(normalized)
                deduped_queries.append(normalized)

        logger.debug("QueryGenerationSkill: {} queries for '{}'", len(deduped_queries), city)
        return QueryGenerationOutput(queries=deduped_queries)


# ── LocaleNavigationSkill ──────────────────────────────────────────────────────

class LocaleNavigationSkill:
    """Navigate non-English camera sites using language-specific terms."""

    async def run(self, input: LocaleNavigationInput) -> LocaleNavigationOutput:
        """
        Navigate a non-English camera site and extract camera links.

        Args:
            input: LocaleNavigationInput with source_url and target_language.

        Returns:
            LocaleNavigationOutput with camera_links list.
        """
        lang = input.target_language
        nav_terms = _LOCALE_NAVIGATION_TERMS.get(lang, ["webcam", "camera", "live"])
        camera_links: list[str] = []
        base_domain = urlparse(input.source_url).netloc

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0),
                follow_redirects=True,
                headers={"User-Agent": "WebcamDiscoveryBot/1.0"},
            ) as client:
                resp = await client.get(input.source_url)
                if resp.status_code != 200:
                    return LocaleNavigationOutput(camera_links=[])
                soup = BeautifulSoup(resp.text, "html.parser")

                for link in soup.find_all("a", href=True):
                    href = str(link["href"])
                    text = link.get_text(strip=True)
                    abs_href = urljoin(input.source_url, href)
                    link_domain = urlparse(abs_href).netloc

                    if link_domain != base_domain:
                        continue

                    # Check if the link text or URL path contains navigation terms
                    combined = (text + " " + href).lower()
                    if any(term.lower() in combined for term in nav_terms):
                        camera_links.append(abs_href)

        except httpx.TimeoutException:
            logger.warning("LocaleNavigationSkill timeout: {}", input.source_url)
        except Exception as exc:
            logger.warning("LocaleNavigationSkill error on {}: {}", input.source_url, exc)

        logger.debug(
            "LocaleNavigationSkill: {} links found on {} (lang={})",
            len(camera_links), input.source_url, lang,
        )
        return LocaleNavigationOutput(camera_links=camera_links)


# ── SourceDiscoverySkill ───────────────────────────────────────────────────────

class SourceDiscoverySkill:
    """Identify new webcam directories from search results for human review."""

    async def run(self, input: SourceDiscoveryInput) -> SourceDiscoveryOutput:
        """
        Extract domains from search results and check for cam directory structure.

        Args:
            input: SourceDiscoveryInput with search_results URLs and known_domains.

        Returns:
            SourceDiscoveryOutput with candidate_sources list (for human review only).
        """
        known = set(d.lower() for d in input.known_domains)
        domain_counts: dict[str, int] = {}

        for url in input.search_results:
            try:
                domain = urlparse(url).netloc.lower().removeprefix("www.")
                if domain and domain not in known:
                    domain_counts[domain] = domain_counts.get(domain, 0) + 1
            except Exception:
                continue

        # Only flag domains appearing multiple times as potential cam directories
        candidate_sources: list[dict] = []
        for domain, count in domain_counts.items():
            if count >= 2:
                candidate_sources.append({
                    "domain": domain,
                    "hit_count": count,
                    "review_required": True,
                    "note": "Flagged for human review — not auto-added to allow list",
                })

        logger.info(
            "SourceDiscoverySkill: {} candidate sources found (human review required)",
            len(candidate_sources),
        )
        return SourceDiscoveryOutput(candidate_sources=candidate_sources)


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    city = sys.argv[1] if len(sys.argv) > 1 else "Tokyo"
    skill = QueryGenerationSkill()
    result = skill.run(QueryGenerationInput(city=city, language_codes=["en", "ja"]))
    for q in result.queries:
        logger.info("  {}", q)

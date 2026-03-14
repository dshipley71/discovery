#!/usr/bin/env python3
"""
api_discovery.py — Key-free public API integrations for webcam discovery.
Part of the Public Webcam Discovery System.

Adds cameras from APIs that require no authentication.  Each URL returned is
passed through the standard validation pipeline, so _probe_html will extract
.m3u8 / .mjpeg stream URLs from embed pages automatically.

Supported APIs (no key required)
---------------------------------
Windy Webcams v2  — https://api.windy.com/api/webcams/v2/list
  orderby=popularity  Sorted by popularity worldwide.
  orderby=continent   One batch per continent for geographic breadth.
  Fields: url, location, player  (returns embed player URLs with lat/lon)
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from loguru import logger
from pydantic import BaseModel

from webcam_discovery.schemas import CameraCandidate


# ── I/O models ────────────────────────────────────────────────────────────────

class ApiDiscoveryResult(BaseModel):
    """Result from a single API-based webcam discovery call."""

    candidates: list[CameraCandidate]
    source: str
    total_fetched: int
    error: Optional[str] = None


# ── WindyApiSkill ─────────────────────────────────────────────────────────────

class WindyApiSkill:
    """
    Discover webcams via the Windy Webcams API (no API key required).

    The Windy API v2 provides public access to webcam metadata and player embed
    URLs.  Each candidate URL is the embed player page; the validation pipeline's
    _probe_html extracts the .m3u8 stream URL from the embed page HTML.

    Pagination
    ----------
    Results are fetched in batches of PAGE_SIZE with a 0.5 s polite delay.  A
    geographic sweep (continent buckets) runs after the global popularity list to
    improve worldwide coverage beyond what the global ranking alone provides.

    Failure handling
    ----------------
    403 → API now requires a key; returns partial results with error flag.
    Any HTTP error → logged, up to 3 consecutive failures before aborting.
    """

    BASE_URL  = "https://api.windy.com/api/webcams/v2/list"
    FIELDS    = "webcams:url,location,player"
    PAGE_SIZE = 100
    CONTINENTS = ["europe", "north-america", "asia", "africa", "south-america", "australia-oceania"]

    async def run(self, total_limit: int = 500) -> ApiDiscoveryResult:
        """
        Fetch webcam listings from the Windy public API.

        Args:
            total_limit: Maximum webcams to fetch via the popularity ranking.
                         A geographic sweep adds up to PAGE_SIZE per continent
                         on top of this limit.

        Returns:
            ApiDiscoveryResult with CameraCandidate objects.
        """
        all_candidates: list[CameraCandidate] = []
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WebcamDiscovery/1.0)"},
        ) as client:
            # ── Phase 1: global popularity list ───────────────────────────────
            phase1 = await self._fetch_list(
                client,
                order="popularity",
                limit=total_limit,
                seen_urls=seen_urls,
            )
            if phase1.error == "api_key_required":
                return ApiDiscoveryResult(
                    candidates=[],
                    source="api:windy.com",
                    total_fetched=0,
                    error="api_key_required",
                )
            all_candidates.extend(phase1.candidates)

            # ── Phase 2: per-continent sweep (geographic breadth) ─────────────
            for continent in self.CONTINENTS:
                phase2 = await self._fetch_list(
                    client,
                    order=f"continent={continent}/orderby=popularity",
                    limit=self.PAGE_SIZE,
                    seen_urls=seen_urls,
                )
                all_candidates.extend(phase2.candidates)
                if phase2.error:
                    break  # API error — stop trying continents
                await asyncio.sleep(0.3)

        logger.info(
            "WindyApiSkill: {} total candidates from Windy API",
            len(all_candidates),
        )
        return ApiDiscoveryResult(
            candidates=all_candidates,
            source="api:windy.com",
            total_fetched=len(all_candidates),
        )

    async def _fetch_list(
        self,
        client: httpx.AsyncClient,
        order: str,
        limit: int,
        seen_urls: set[str],
    ) -> ApiDiscoveryResult:
        """Paginate through one Windy API list query."""
        candidates: list[CameraCandidate] = []
        offset = 0
        failures = 0

        while offset < limit and failures < 3:
            batch = min(self.PAGE_SIZE, limit - offset)
            url = f"{self.BASE_URL}/{order}/limit={batch},offset={offset}?show={self.FIELDS}"

            try:
                resp = await client.get(url)
            except Exception as exc:
                logger.warning("WindyApiSkill: request error ({}): {}", url, exc)
                failures += 1
                await asyncio.sleep(2.0 * failures)
                continue

            if resp.status_code == 403:
                logger.warning("WindyApiSkill: 403 Forbidden — API may now require a key")
                return ApiDiscoveryResult(
                    candidates=candidates,
                    source="api:windy.com",
                    total_fetched=len(candidates),
                    error="api_key_required",
                )
            if resp.status_code != 200:
                logger.warning("WindyApiSkill: HTTP {} from {}", resp.status_code, url)
                failures += 1
                await asyncio.sleep(2.0 * failures)
                continue

            try:
                data = resp.json()
            except Exception:
                logger.warning("WindyApiSkill: JSON parse error from {}", url)
                failures += 1
                continue

            if data.get("status") != "OK":
                logger.warning("WindyApiSkill: API status={}", data.get("status"))
                break

            webcams = data.get("result", {}).get("webcams", [])
            if not webcams:
                break

            for cam in webcams:
                c = self._parse_webcam(cam)
                if c and c.url not in seen_urls:
                    seen_urls.add(c.url)
                    candidates.append(c)

            failures = 0
            offset += len(webcams)
            if len(webcams) < batch:
                break  # Last page

            await asyncio.sleep(0.5)

        return ApiDiscoveryResult(
            candidates=candidates,
            source="api:windy.com",
            total_fetched=len(candidates),
        )

    def _parse_webcam(self, cam: dict) -> Optional[CameraCandidate]:
        """
        Parse a Windy API webcam entry into a CameraCandidate.

        URL priority (most to least direct)
        ------------------------------------
        1. url.current.live    — live stream or embed URL returned by Windy
        2. player.day.embed    — HTML embed page (validation probes for .m3u8)
        3. player.embed        — alternate embed field
        4. url.current.desktop — full webcam landing page
        """
        try:
            location = cam.get("location", {})
            urls     = cam.get("url", {}).get("current", {})
            player   = cam.get("player", {})

            city    = location.get("city")    or "Unknown"
            country = location.get("country") or "Unknown"
            title   = cam.get("title")        or city

            candidate_url = (
                urls.get("live")
                or player.get("day", {}).get("embed")
                or player.get("embed")
                or urls.get("desktop")
            )
            if not candidate_url:
                return None

            return CameraCandidate(
                url=candidate_url,
                label=title,
                city=city,
                country=country,
                source_directory="api:windy.com",
                source_refs=[candidate_url],
            )
        except Exception as exc:
            logger.debug("WindyApiSkill: parse error: {}", exc)
            return None


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    async def _main() -> None:
        skill = WindyApiSkill()
        result = await skill.run(total_limit=50)
        logger.info(
            "WindyApiSkill: {} candidates (error={})", result.total_fetched, result.error
        )
        for c in result.candidates[:10]:
            print(json.dumps(c.model_dump(), indent=2))

    asyncio.run(_main())

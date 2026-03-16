#!/usr/bin/env python3
"""
validator.py — HTTP validation, feed classification, and legitimacy scoring.
Part of the Public Webcam Discovery System.

Performance model
-----------------
HTTP probing    — asyncio with Semaphore(settings.validation_concurrency).
                  FeedValidationSkill handles its own semaphore internally.
Geo-enrichment  — pure asyncio with upfront cache warming.
                  Before per-candidate resolution, unique city+country pairs and
                  unique country names are geocoded once sequentially (respecting
                  the Nominatim 1 req/s policy).  Per-candidate calls then run
                  concurrently via asyncio.gather: most are instant cache hits;
                  ip-api.com fallbacks execute in parallel since they share no lock.
                  The process-wide class-level cache (GeoEnrichmentSkill._geo_cache)
                  means repeated pipeline runs never re-geocode the same location.
Coordinates     — GeoEnrichmentSkill tries four strategies: city+country,
                  label text, IP geolocation of hostname, country center.
                  Cameras that survive all fallbacks with no coordinates are
                  included with latitude=None/longitude=None and
                  notes="location_unknown" for manual review.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from loguru import logger
from slugify import slugify
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate, CameraRecord, LegitimacyScore
from webcam_discovery.skills.validation import (
    FeedValidationSkill,
    RobotsPolicySkill,
    RobotsPolicyInput,
    FeedTypeClassificationSkill,
    FeedTypeInput,
    ValidationResult,
)
from webcam_discovery.skills.catalog import GeoEnrichmentSkill, GeoEnrichmentInput


# ── Helpers ───────────────────────────────────────────────────────────────────

_LEGIT_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _legit_ok(score: LegitimacyScore, minimum: str) -> bool:
    """Return True if score meets or exceeds the minimum threshold."""
    return _LEGIT_ORDER.get(score, 0) >= _LEGIT_ORDER.get(minimum, 0)


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _make_slug(city: str, label: str) -> str:
    """Generate a stable ID slug from city + label."""
    return slugify(f"{city} {label}", max_length=80, word_boundary=True, separator="-")


# ── ValidationAgent ───────────────────────────────────────────────────────────

class ValidationAgent:
    """
    Validates CameraCandidate objects via HTTP checks, classifies feed types,
    and optionally geo-enriches with lat/lon.

    Processing steps
    ----------------
    1. Group by domain; check robots.txt concurrently (one task per domain).
    2. Probe all allowed URLs via FeedValidationSkill (semaphore-limited async).
    3. Filter by settings.min_legitimacy.
    4. Geo-enrich via batch async: pre-warm cache for unique city+country pairs,
       then resolve all candidates concurrently (cache hits + parallel ip-api calls).
    5. Build CameraRecord; cameras where all geo fallbacks fail get latitude=None/longitude=None.
    """

    async def run(
        self,
        candidates: list[CameraCandidate],
        input_file: Optional[Path] = None,
    ) -> list[CameraRecord]:
        """
        Validate candidates and return CameraRecord objects.

        Args:
            candidates:  CameraCandidate list from discovery agents.
            input_file:  Optional JSONL path; loaded when candidates is empty.

        Returns:
            list[CameraRecord] ready for CatalogAgent.
        """
        if input_file and not candidates:
            candidates = [
                CameraCandidate(**json.loads(line))
                for line in input_file.read_text().splitlines()
                if line.strip()
            ]

        if not candidates:
            logger.warning("ValidationAgent: no candidates to validate")
            return []

        logger.info("ValidationAgent: {} candidates received", len(candidates))

        # ── Step 1: robots.txt (per-domain, cached, concurrent) ───────────────
        robots_skill = RobotsPolicySkill()
        domain_map: dict[str, list[CameraCandidate]] = {}
        for c in candidates:
            domain_map.setdefault(_domain_of(c.url), []).append(c)

        robots_tasks = [
            self._check_robots(robots_skill, domain, cands)
            for domain, cands in domain_map.items()
        ]
        allowed: list[CameraCandidate] = []
        for batch in await asyncio.gather(*robots_tasks):
            allowed.extend(batch)

        logger.info(
            "ValidationAgent: {}/{} candidates pass robots.txt (dropped {})",
            len(allowed), len(candidates), len(candidates) - len(allowed),
        )
        if not allowed:
            return []

        # ── Step 2: HTTP probe all allowed URLs ───────────────────────────────
        feed_skill = FeedValidationSkill()
        type_skill = FeedTypeClassificationSkill()
        geo_skill  = GeoEnrichmentSkill()

        logger.info(
            "ValidationAgent: probing {} URLs "
            "(concurrency={}, connect={}s, read={}s) …",
            len(allowed),
            settings.validation_concurrency,
            settings.validation_timeout_connect,
            settings.validation_timeout_read,
        )
        validation_results = await feed_skill.run([c.url for c in allowed])
        url_to_val = {r.url: r for r in validation_results}

        n_live    = sum(1 for r in validation_results if r.status == "live")
        n_timeout = sum(1 for r in validation_results if r.fail_reason == "timeout")
        n_dead    = sum(1 for r in validation_results if r.status == "dead")
        logger.info(
            "ValidationAgent: probe results — live={}, dead={}, timeout={}, other={}",
            n_live, n_dead, n_timeout,
            len(allowed) - n_live - n_dead - n_timeout,
        )

        # ── Step 3: filter by legitimacy ──────────────────────────────────────
        min_legit = settings.min_legitimacy
        to_enrich: list[tuple[CameraCandidate, object]] = []

        for candidate in allowed:
            v = url_to_val.get(candidate.url)
            if v is None:
                continue
            if v.status != "live":
                continue
            if not _legit_ok(v.legitimacy_score, min_legit):
                logger.debug(
                    "ValidationAgent: drop {} — legit={} < min={}",
                    candidate.url, v.legitimacy_score, min_legit,
                )
                continue
            to_enrich.append((candidate, v))

        logger.info(
            "ValidationAgent: {} candidates pass min_legitimacy='{}' filter",
            len(to_enrich), min_legit,
        )

        # ── Step 4: geo-enrich (batch async with cache warming) ───────────────
        geo_results = await self._batch_geo_enrich(
            geo_skill, [c for c, _ in to_enrich]
        )

        records: list[CameraRecord] = []

        # ── Step 5: build CameraRecord objects ────────────────────────────────
        for (candidate, v), geo in zip(to_enrich, geo_results):
            feed_type_result = type_skill.run(FeedTypeInput(
                url=candidate.url,
                content_type=v.content_type,
                playlist_type=v.playlist_type,
            ))

            city    = candidate.city    or "Unknown"
            country = candidate.country or "Unknown"
            label   = candidate.label   or city

            latitude:  Optional[float] = None
            longitude: Optional[float] = None
            continent  = "Unknown"
            region:  Optional[str] = None

            if isinstance(geo, Exception):
                logger.warning("ValidationAgent: geo error for '{}': {}", city, geo)
            elif geo is not None:
                latitude   = geo.latitude
                longitude  = geo.longitude
                continent  = geo.continent or "Unknown"
                region     = geo.region
                if geo.country:
                    country = geo.country

            record_id = _make_slug(city, label) or _make_slug("camera", candidate.url[-30:])

            notes = candidate.notes or ""
            if latitude is None:
                notes = f"{notes} location_unknown".strip()

            source_refs = list(candidate.source_refs) if candidate.source_refs else []

            try:
                record = CameraRecord(
                    id=record_id,
                    label=label,
                    city=city,
                    region=region,
                    country=country,
                    continent=continent,
                    latitude=latitude,
                    longitude=longitude,
                    url=candidate.url,
                    feed_type=feed_type_result.feed_type,
                    playlist_type=v.playlist_type,
                    variant_streams=v.variant_streams,
                    source_directory=candidate.source_directory,
                    source_refs=source_refs or [candidate.url],
                    legitimacy_score=v.legitimacy_score,
                    status=v.status,
                    last_verified=None,
                    notes=notes or None,
                )
                records.append(record)
                logger.debug(
                    "ValidationAgent: ✓ {} | feed={} | playlist={} | legit={} | coords={} | url={}",
                    label,
                    feed_type_result.feed_type,
                    v.playlist_type or "—",
                    v.legitimacy_score,
                    f"{latitude:.3f},{longitude:.3f}" if latitude is not None else "none",
                    candidate.url,
                )
            except Exception as exc:
                logger.warning(
                    "ValidationAgent: record build error for '{}': {}", candidate.url, exc
                )

        located   = sum(1 for r in records if r.latitude is not None)
        unlocated = len(records) - located
        logger.info(
            "ValidationAgent: {} validated records "
            "({} with coords, {} without — included with location_unknown note)",
            len(records), located, unlocated,
        )
        return records

    # ── Concurrency helpers ───────────────────────────────────────────────────

    async def _batch_geo_enrich(
        self,
        skill: GeoEnrichmentSkill,
        candidates: list[CameraCandidate],
    ) -> list[Optional[object]]:
        """
        Geo-enrich candidates in bulk, deduplicating Nominatim queries.

        Strategy
        --------
        Phase 1 — Pre-warm cache for unique city+country pairs.
                  Each unique pair triggers exactly one sequential Nominatim
                  call (respecting the 1 req/s policy).  All other candidates
                  sharing that pair will be instant cache hits.
        Phase 2 — Pre-warm cache for unique country names (country-center
                  fallback).  Only countries not already resolved by Phase 1
                  are geocoded here.
        Phase 3 — Run skill.run() for every candidate concurrently via
                  asyncio.gather.  Nearly all calls return from cache
                  immediately; ip-api.com hostname lookups execute in
                  parallel since they share no global lock.

        Returns results in the same order as `candidates`.
        """
        cache = GeoEnrichmentSkill._geo_cache

        # Phase 1: unique city+country → one Nominatim call each
        seen_city_country: dict[str, CameraCandidate] = {}
        for c in candidates:
            city = (c.city or "").strip()
            country = (c.country or "").strip()
            if city and city.lower() not in ("unknown", ""):
                key = f"city:{city}|{country}"
                if key not in cache and key not in seen_city_country:
                    seen_city_country[key] = c

        for key, c in tqdm(
            seen_city_country.items(),
            total=len(seen_city_country),
            desc="Geocoding city+country",
            unit="pair",
            ncols=90,
            disable=not seen_city_country,
        ):
            city = (c.city or "").strip()
            country = (c.country or "").strip()
            query = f"{city}, {country}" if country else city
            await skill._geocode_nominatim(query, cache_key=key)

        # Phase 2: unique countries for country-center fallback
        seen_countries: set[str] = set()
        for c in candidates:
            country = (c.country or "").strip()
            if country and country.lower() not in ("unknown", ""):
                ckey = f"country:{country}"
                if ckey not in cache:
                    seen_countries.add(country)

        for country in tqdm(
            seen_countries,
            total=len(seen_countries),
            desc="Geocoding countries  ",
            unit="country",
            ncols=90,
            disable=not seen_countries,
        ):
            await skill._geocode_nominatim(country, cache_key=f"country:{country}")

        # Phase 3: concurrent per-candidate resolution (mostly cache hits)
        async def _safe_run(candidate: CameraCandidate) -> Optional[object]:
            try:
                return await skill.run(GeoEnrichmentInput(
                    city=candidate.city,
                    country=candidate.country,
                    label=candidate.label,
                    url=candidate.url,
                ))
            except Exception as exc:
                logger.debug(
                    "ValidationAgent: geo failed for '{}': {}", candidate.url, exc
                )
                return None

        return list(await tqdm_asyncio.gather(
            *[_safe_run(c) for c in candidates],
            desc="Geo-enriching",
            unit="cam",
            ncols=90,
        ))

    async def _check_robots(
        self,
        skill: RobotsPolicySkill,
        domain: str,
        cands: list[CameraCandidate],
    ) -> list[CameraCandidate]:
        """Return the candidate list if robots.txt allows crawling this domain."""
        try:
            result = await skill.run(RobotsPolicyInput(domain=domain))
            if result.allowed:
                return cands
            logger.info(
                "ValidationAgent: robots.txt blocks {} ({} candidates dropped)",
                domain, len(cands),
            )
            return []
        except Exception as exc:
            logger.warning("ValidationAgent: robots check error for {}: {}", domain, exc)
            return cands  # default-allow on error


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for validator (wcd-validate)."""
    from webcam_discovery.pipeline import configure_logging
    configure_logging()
    parser = argparse.ArgumentParser(description="Validate camera candidates")
    parser.add_argument(
        "--input", type=Path, required=True,
        help="Path to candidates.jsonl from discovery agents",
    )
    parser.add_argument(
        "--output", type=Path,
        default=settings.candidates_dir / "validated.jsonl",
        help="Output path for validated.jsonl",
    )
    args = parser.parse_args()

    candidates = [
        CameraCandidate(**json.loads(line))
        for line in args.input.read_text().splitlines()
        if line.strip()
    ]
    records = asyncio.run(ValidationAgent().run(candidates=candidates))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(r.model_dump_json() for r in records),
        encoding="utf-8",
    )
    logger.info("ValidationAgent: {} records → {}", len(records), args.output)


if __name__ == "__main__":
    main()

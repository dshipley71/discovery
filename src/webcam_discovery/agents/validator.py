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
from webcam_discovery.skills.catalog import GeoEnrichmentSkill, GeoEnrichmentInput, _normalize_place_name


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
        # Build Referer map: url → source_directory so _probe_hls can send the
        # originating webcam site's URL as the Referer header.  Many CDNs gate
        # .m3u8 delivery to requests that look like they come from the source site.
        referers = {
            c.url: c.source_directory
            for c in allowed
            if c.source_directory
        }
        validation_results = await feed_skill.run(
            [c.url for c in allowed], referers=referers
        )
        url_to_val = {r.url: r for r in validation_results}

        n_live    = sum(1 for r in validation_results if r.status == "live")
        n_timeout = sum(1 for r in validation_results if r.fail_reason == "timeout")
        n_dead    = sum(1 for r in validation_results if r.status == "dead")
        n_unknown = sum(1 for r in validation_results if r.status == "unknown")
        logger.info(
            "ValidationAgent: probe results — live={}, dead={}, unknown={}, timeout={}, other={}",
            n_live, n_dead, n_unknown, n_timeout,
            len(allowed) - n_live - n_dead - n_unknown - n_timeout,
        )

        # Log fail_reason breakdown for dead + unknown results so operators can
        # distinguish token-expiry (http_403), offline (http_404), hotlink (http_403),
        # no-magic-bytes (no_m3u8_magic), and genuine timeouts.
        from collections import Counter
        fail_reasons = Counter(
            r.fail_reason
            for r in validation_results
            if r.fail_reason and r.status in ("dead", "unknown")
        )
        if fail_reasons:
            breakdown = "  ".join(f"{reason}={n}" for reason, n in fail_reasons.most_common())
            logger.info("ValidationAgent: failure reasons — {}", breakdown)

        # ── Step 3: filter by legitimacy ──────────────────────────────────────
        # status="unknown" with min_legitimacy="low" is kept: a 200 OK that
        # lacked #EXTM3U magic may still be a valid stream (e.g. no-magic CDN
        # delivery).  Operators can review these via notes="no_m3u8_magic".
        min_legit = settings.min_legitimacy
        to_enrich: list[tuple[CameraCandidate, object]] = []

        for candidate in allowed:
            v = url_to_val.get(candidate.url)
            if v is None:
                continue
            # Accept "live" always; accept "unknown" only when min_legitimacy="low"
            if v.status == "dead":
                continue
            if v.status == "unknown" and min_legit != "low":
                logger.debug(
                    "ValidationAgent: drop {} — status=unknown requires min_legitimacy=low",
                    candidate.url,
                )
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

    async def run_from_queue(
        self,
        queue: asyncio.Queue,
        batch_size: int = 100,
    ) -> list[CameraRecord]:
        """
        Validate candidates delivered via an ``asyncio.Queue``, processing them
        in batches as they arrive.

        Used by the streaming pipeline so that validation overlaps with discovery:
        candidates produced by DirectoryAgent / SearchAgent are validated
        immediately rather than waiting for all discovery to finish.

        The queue must be closed by the producer(s) sending a single ``None``
        sentinel value after all items have been put.

        Args:
            queue:      Shared ``asyncio.Queue[CameraCandidate | None]``.
                        ``None`` is the end-of-stream sentinel.
            batch_size: Number of candidates to accumulate before triggering a
                        validation pass.  Smaller values reduce latency at the
                        cost of more per-batch overhead (extra robots/HTTP round-
                        trips).  Default: 100.

        Returns:
            list[CameraRecord] — combined results from all batches.
        """
        pending: list[CameraCandidate] = []
        all_records: list[CameraRecord] = []
        seen_urls: set[str] = set()  # cross-agent deduplication

        async def flush() -> None:
            if not pending:
                return
            logger.info(
                "ValidationAgent.run_from_queue: flushing batch of {} candidates", len(pending)
            )
            batch_records = await self.run(candidates=list(pending))
            all_records.extend(batch_records)
            pending.clear()

        while True:
            item: CameraCandidate | None = await queue.get()
            if item is None:
                # End-of-stream sentinel — flush whatever remains and stop.
                await flush()
                break

            if item.url not in seen_urls:
                seen_urls.add(item.url)
                pending.append(item)

            if len(pending) >= batch_size:
                await flush()

        logger.info(
            "ValidationAgent.run_from_queue: complete — {} records from {} unique candidates",
            len(all_records), len(seen_urls),
        )
        return all_records

    # ── Concurrency helpers ───────────────────────────────────────────────────

    async def _batch_geo_enrich(
        self,
        skill: GeoEnrichmentSkill,
        candidates: list[CameraCandidate],
    ) -> list[Optional[object]]:
        """
        Geo-enrich candidates in bulk.

        LLM mode (use_llm_geodecode=True, default)
        -------------------------------------------
        Skips Nominatim pre-warm phases entirely.  Calls skill.run() for every
        candidate concurrently via asyncio.gather; GeoEnrichmentSkill._llm_lock
        serialises the underlying Ollama requests at 1 req/s to avoid 429s.
        Progress is shown as each LLM call completes.

        Nominatim mode (use_llm_geodecode=False)
        ----------------------------------------
        Phase 1 — Pre-warm cache for unique city+country pairs (sequential,
                  1 req/s Nominatim policy).
        Phase 2 — Pre-warm cache for unique country names (country-center
                  fallback).
        Phase 3 — Concurrent per-candidate resolution; nearly all calls are
                  instant cache hits.  ip-api.com fallbacks run in parallel.

        Returns results in the same order as `candidates`.
        """
        cache = GeoEnrichmentSkill._geo_cache

        if not skill._use_llm:
            # ── Nominatim Phase 1: unique city+country ─────────────────────────
            seen_city_country: dict[str, CameraCandidate] = {}
            for c in candidates:
                city    = (c.city    or "").strip()
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
                city    = (c.city    or "").strip()
                country = (c.country or "").strip()
                city_norm    = _normalize_place_name(city)    if city    else ""
                country_norm = _normalize_place_name(country) if country else ""
                query = f"{city_norm}, {country_norm}" if country_norm else city_norm
                await skill._geocode_nominatim(query, cache_key=key)

            # ── Nominatim Phase 2: unique countries ────────────────────────────
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

        # ── Phase 3 (both modes): per-candidate resolution ─────────────────────
        # LLM mode: calls are serialised inside _geocode_with_llm via _llm_lock
        #           (1 req/s); tqdm shows each call completing in sequence.
        # Nominatim mode: nearly all calls are instant cache hits from Phase 1/2.
        geo_desc = (
            f"LLM geocoding ({skill._LLM_INTERVAL:.1f}s/req)"
            if skill._use_llm
            else "Geo-enriching       "
        )

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
            desc=geo_desc,
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

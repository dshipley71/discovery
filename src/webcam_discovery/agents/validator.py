#!/usr/bin/env python3
"""
validator.py — HTTP validation, feed classification, and legitimacy scoring.
Part of the Public Webcam Discovery System.

Performance model
-----------------
HTTP probing    — asyncio with Semaphore(settings.validation_concurrency).
                  FeedValidationSkill handles its own semaphore internally.
Geo-enrichment  — concurrent.futures.ThreadPoolExecutor(settings.geo_thread_workers)
                  via asyncio.run_in_executor; geopy calls are synchronous and
                  rate-limited per-domain (1 req/s), so threads allow multiple
                  lookups to run in parallel without blocking the event loop.
Coordinates     — Optional; cameras without resolvable coordinates are included
                  with latitude=None/longitude=None and notes="location_unknown".
                  CatalogAgent may filter these from GeoJSON but they are
                  preserved in validated.jsonl for future enrichment.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from loguru import logger
from slugify import slugify

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate, CameraRecord, LegitimacyScore
from webcam_discovery.skills.validation import (
    FeedValidationSkill,
    RobotsPolicySkill,
    RobotsPolicyInput,
    FeedTypeClassificationSkill,
    FeedTypeInput,
)
from webcam_discovery.skills.catalog import GeoEnrichmentSkill, GeoEnrichmentInput


# ── Helpers ───────────────────────────────────────────────────────────────────

_LEGIT_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _legit_ok(score: LegitimacyScore, minimum: str) -> bool:
    """Return True if score meets or exceeds the minimum threshold."""
    return _LEGIT_ORDER.get(score, 0) >= _LEGIT_ORDER.get(minimum, 0)


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lstrip("www.")


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
    4. Classify feed type via FeedTypeClassificationSkill (synchronous).
    5. Geo-enrich in parallel via ThreadPoolExecutor (synchronous geopy in threads).
    6. Build CameraRecord; cameras without coordinates get latitude=None/longitude=None.
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

        # ── Step 4 & 5: classify + geo-enrich (threads for geo) ───────────────
        loop = asyncio.get_event_loop()
        records: list[CameraRecord] = []

        with ThreadPoolExecutor(
            max_workers=settings.geo_thread_workers,
            thread_name_prefix="geo-worker",
        ) as executor:
            geo_futures = [
                loop.run_in_executor(
                    executor,
                    self._geo_enrich_sync,
                    geo_skill,
                    candidate,
                )
                for candidate, _ in to_enrich
            ]
            geo_results = await asyncio.gather(*geo_futures, return_exceptions=True)

        # ── Step 6: build CameraRecord objects ────────────────────────────────
        for (candidate, v), geo in zip(to_enrich, geo_results):
            feed_type_result = type_skill.run(FeedTypeInput(
                url=candidate.url,
                content_type=v.content_type,
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
            if v.legitimacy_score == "medium" and v.fail_reason:
                notes = f"{notes} review:{v.fail_reason}".strip()
            if latitude is None:
                notes = f"{notes} location_unknown".strip()

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
                    source_directory=candidate.source_directory,
                    source_refs=candidate.source_refs or [candidate.url],
                    legitimacy_score=v.legitimacy_score,
                    status=v.status,
                    last_verified=None,
                    notes=notes or None,
                )
                records.append(record)
                logger.debug(
                    "ValidationAgent: ✓ {} | feed={} | legit={} | coords={}",
                    label,
                    feed_type_result.feed_type,
                    v.legitimacy_score,
                    f"{latitude:.3f},{longitude:.3f}" if latitude is not None else "none",
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

    @staticmethod
    def _geo_enrich_sync(
        skill: GeoEnrichmentSkill, candidate: CameraCandidate
    ):
        """
        Synchronous wrapper for GeoEnrichmentSkill, intended for use inside
        a ThreadPoolExecutor.  Runs a fresh event loop per thread so that
        the async geopy interface works without touching the main loop.

        Returns the GeoEnrichmentOutput or None if city is unknown / lookup fails.
        """
        city = candidate.city
        if not city or city.lower() in ("unknown", ""):
            return None

        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                skill.run(GeoEnrichmentInput(
                    city=city,
                    country=candidate.country,
                    label=candidate.label or city,
                ))
            )
        except Exception as exc:
            logger.debug(
                "ValidationAgent: geo lookup failed for city='{}': {}", city, exc
            )
            return None
        finally:
            loop.close()


# ── CLI entry point ───────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for validator (wcd-validate)."""
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

#!/usr/bin/env python3
"""
validator.py — HTTP validation, feed classification, and legitimacy scoring.
Part of the Public Webcam Discovery System.
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


# Legitimacy score ordering (higher index = higher legitimacy)
_LEGIT_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
_MIN_LEGIT_DEFAULT = "medium"


def _legit_meets_minimum(score: LegitimacyScore, minimum: str) -> bool:
    """Return True if score meets or exceeds the minimum threshold."""
    return _LEGIT_ORDER.get(score, 0) >= _LEGIT_ORDER.get(minimum, 1)


def _domain_of(url: str) -> str:
    """Extract domain from URL."""
    return urlparse(url).netloc.lstrip("www.")


def _make_slug(city: str, label: str) -> str:
    """Generate a stable ID slug from city and label."""
    raw = f"{city} {label}"
    return slugify(raw, max_length=80, word_boundary=True, separator="-")


class ValidationAgent:
    """
    Validates CameraCandidate objects via HTTP HEAD/GET checks.
    Classifies feed types and assigns legitimacy scores.
    Rejects low-scoring records; flags medium records for review.
    """

    async def run(
        self,
        candidates: list[CameraCandidate],
        input_file: Optional[Path] = None,
    ) -> list[CameraRecord]:
        """
        Validate candidates and return verified CameraRecord objects.

        Args:
            candidates:  List of CameraCandidate objects from discovery agents.
            input_file:  Optional JSONL file path (used when running as CLI).

        Returns:
            list[CameraRecord] — validated, scored records ready for CatalogAgent.
        """
        # Load from file if provided and candidates is empty
        if input_file and not candidates:
            candidates = [
                CameraCandidate(**json.loads(line))
                for line in input_file.read_text().splitlines()
                if line.strip()
            ]

        if not candidates:
            logger.warning("ValidationAgent: no candidates to validate")
            return []

        logger.info("ValidationAgent: validating {} candidates", len(candidates))

        robots_skill = RobotsPolicySkill()
        feed_skill = FeedValidationSkill()
        type_skill = FeedTypeClassificationSkill()
        geo_skill = GeoEnrichmentSkill()

        min_legit = settings.min_legitimacy

        # Group candidates by domain for robots.txt check
        domain_candidates: dict[str, list[CameraCandidate]] = {}
        for c in candidates:
            domain = _domain_of(c.url)
            domain_candidates.setdefault(domain, []).append(c)

        # Check robots.txt per domain
        allowed_candidates: list[CameraCandidate] = []
        for domain, domain_cands in domain_candidates.items():
            try:
                robots_result = await robots_skill.run(RobotsPolicyInput(domain=domain))
                if robots_result.allowed:
                    allowed_candidates.extend(domain_cands)
                else:
                    logger.info(
                        "ValidationAgent: robots.txt blocks {} — skipping {} candidates",
                        domain, len(domain_cands),
                    )
            except Exception as exc:
                logger.warning("ValidationAgent: robots check error for {}: {}", domain, exc)
                allowed_candidates.extend(domain_cands)  # default allow on error

        logger.info(
            "ValidationAgent: {} candidates pass robots check (dropped {})",
            len(allowed_candidates), len(candidates) - len(allowed_candidates),
        )

        if not allowed_candidates:
            return []

        # Validate all URLs
        urls = [c.url for c in allowed_candidates]
        validation_results = await feed_skill.run(urls)
        url_to_validation = {r.url: r for r in validation_results}

        # Build CameraRecord objects
        records: list[CameraRecord] = []
        for candidate in allowed_candidates:
            v = url_to_validation.get(candidate.url)
            if v is None:
                continue

            # Skip low legitimacy (unless minimum is "low")
            if not _legit_meets_minimum(v.legitimacy_score, min_legit):
                logger.debug(
                    "ValidationAgent: dropping '{}' — legitimacy={} < minimum={}",
                    candidate.url, v.legitimacy_score, min_legit,
                )
                continue

            # Classify feed type
            feed_type_result = type_skill.run(FeedTypeInput(
                url=candidate.url,
                content_type=v.content_type,
            ))

            # Determine city, country, label with fallbacks
            city = candidate.city or "Unknown"
            country = candidate.country or "Unknown"
            label = candidate.label or city

            # Geo-enrich if missing coordinates
            latitude: Optional[float] = None
            longitude: Optional[float] = None
            continent = "Unknown"
            region: Optional[str] = None

            if city and city != "Unknown":
                try:
                    geo = await geo_skill.run(GeoEnrichmentInput(
                        city=city,
                        country=candidate.country,
                        label=label,
                    ))
                    if geo.latitude is not None and geo.longitude is not None:
                        latitude = geo.latitude
                        longitude = geo.longitude
                        continent = geo.continent or "Unknown"
                        region = geo.region
                        if geo.country:
                            country = geo.country
                    else:
                        logger.debug("ValidationAgent: no coordinates for '{}' in '{}'", label, city)
                except Exception as exc:
                    logger.warning("ValidationAgent: geo error for '{}': {}", city, exc)

            # Skip records without coordinates
            if latitude is None or longitude is None:
                logger.debug(
                    "ValidationAgent: skipping '{}' — no coordinates (city='{}')",
                    candidate.url, city,
                )
                continue

            # Generate stable ID slug
            record_id = _make_slug(city, label)
            if not record_id:
                record_id = _make_slug("camera", candidate.url[-30:])

            # Notes for medium records
            notes = candidate.notes
            if v.legitimacy_score == "medium" and v.fail_reason:
                notes = (notes or "") + f" review:{v.fail_reason}"

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
                    notes=notes.strip() if notes else None,
                )
                records.append(record)
                logger.debug(
                    "ValidationAgent: accepted '{}' city='{}' status='{}' legit='{}'",
                    label, city, v.status, v.legitimacy_score,
                )
            except Exception as exc:
                logger.warning("ValidationAgent: record build error for '{}': {}", candidate.url, exc)

        logger.info(
            "ValidationAgent: {} records validated from {} candidates",
            len(records), len(allowed_candidates),
        )
        return records


def main() -> None:
    """CLI entry point for validator."""
    parser = argparse.ArgumentParser(description="Validate camera candidates")
    parser.add_argument("--input", type=Path, required=True,
                        help="Path to candidates.jsonl from discovery agents")
    parser.add_argument("--output", type=Path,
                        default=settings.candidates_dir / "validated.jsonl",
                        help="Output path for validated.jsonl")
    args = parser.parse_args()

    candidates = [
        CameraCandidate(**json.loads(line))
        for line in args.input.read_text().splitlines()
        if line.strip()
    ]
    records = asyncio.run(ValidationAgent().run(candidates=candidates))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(r.model_dump_json() for r in records))
    logger.info("Validated {} records → {}", len(records), args.output)


if __name__ == "__main__":
    main()

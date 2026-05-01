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

Fixes applied (2026-03-23)
--------------------------
unwrap_player_url() is now called on every candidate URL immediately after
candidates are loaded, before robots.txt checking, the hls_only filter, or
any HTTP probing.

Some webcam directories (worldcams.tv, etc.) embed the real .m3u8 URL inside
a player-wrapper URL, e.g.:

    https://worldcams.tv/player?url=https://cdn.example.com/stream.m3u8

These wrapper URLs pass the hls_only filter because ".m3u8" appears in the
string, but FeedValidationSkill probes the wrapper page, receives HTML (not
an HLS playlist), finds no #EXTM3U magic, and drops the stream as dead —
discarding a potentially live feed.

Unwrapping at ingestion resolves this for all entry paths: direct runs,
queue-based streaming, and maintenance re-checks.
"""
from __future__ import annotations

import asyncio
import argparse
import json
from collections import Counter
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
from webcam_discovery.skills.traversal import unwrap_player_url   # ← FIX: import unwrapper
from webcam_discovery.skills.hls_playlist_analysis import analyze_hls_playlist


# ── Helpers ───────────────────────────────────────────────────────────────────

_LEGIT_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _legit_ok(score: LegitimacyScore, minimum: str) -> bool:
    """Return True if score meets or exceeds the minimum threshold."""
    return _LEGIT_ORDER.get(score, 0) >= _LEGIT_ORDER.get(minimum, 0)


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


def _candidate_referer(candidate: CameraCandidate) -> Optional[str]:
    """
    Return the best available HTTP(S) referer URL for *candidate*.

    ``source_directory`` is sometimes a bare domain and sometimes a full page URL.
    Prefer a real URL from source_refs when available, otherwise fall back to
    source_directory only when it already looks like HTTP(S).
    """
    for ref in candidate.source_refs:
        if ref.startswith(("http://", "https://")):
            return ref
    if candidate.source_directory and candidate.source_directory.startswith(("http://", "https://")):
        return candidate.source_directory
    return None


def _make_slug(city: str, label: str) -> str:
    """Generate a stable ID slug from city + label."""
    return slugify(f"{city} {label}", max_length=80, word_boundary=True, separator="-")


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    """Append structured log rows to a JSONL file."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _unwrap_candidates(candidates: list[CameraCandidate]) -> list[CameraCandidate]:
    """
    Unwrap any player-wrapper URLs in the candidate list.

    For each candidate whose URL is a player wrapper (e.g.
    ``https://worldcams.tv/player?url=https://cdn.example.com/stream.m3u8``),
    the inner .m3u8 URL is extracted and stored as the candidate URL.  The
    original wrapper URL is preserved in source_refs so the source page remains
    traceable.

    Candidates whose URL is already a clean .m3u8 are returned unchanged.
    """
    unwrapped: list[CameraCandidate] = []
    n_unwrapped = 0

    for c in candidates:
        clean_url = unwrap_player_url(c.url)
        if clean_url != c.url:
            n_unwrapped += 1
            logger.debug(
                "ValidationAgent: unwrapped player URL '{}' → '{}'",
                c.url, clean_url,
            )
            # Preserve the original wrapper URL as a source reference
            existing_refs = list(c.source_refs) if c.source_refs else []
            if c.url not in existing_refs:
                existing_refs.insert(0, c.url)
            c = c.model_copy(update={"url": clean_url, "source_refs": existing_refs})
        unwrapped.append(c)

    if n_unwrapped:
        logger.info(
            "ValidationAgent: unwrapped {} player-wrapper URL(s) → direct .m3u8 stream(s)",
            n_unwrapped,
        )

    return unwrapped


# ── ValidationAgent ───────────────────────────────────────────────────────────

class ValidationAgent:
    """
    Validates CameraCandidate objects via HTTP checks, classifies feed types,
    and optionally geo-enriches with lat/lon.

    Processing steps
    ----------------
    0. Unwrap player-wrapper URLs (FIX 2026-03-23).
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

        # ── Step 0: unwrap player-wrapper URLs (FIX) ──────────────────────────
        # Must run before robots.txt, hls_only filter, and HTTP probing so that
        # wrapper URLs like https://worldcams.tv/player?url=https://.../stream.m3u8
        # are resolved to their inner .m3u8 before any validation takes place.
        candidates = _unwrap_candidates(candidates)

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

        # ── Step 1b: HLS-only filter ──────────────────────────────────────────
        # When hls_only=True (the default), discard any candidate whose URL is
        # not a direct .m3u8 stream.  Such URLs require user interaction (e.g.
        # clicking a play button on a web page) and are not automatically playable.
        # This runs before the HTTP probe to avoid wasting requests.
        # NOTE: after _unwrap_candidates() above, wrapper URLs are already
        # resolved — the .m3u8 check here applies to the clean inner URL.
        if settings.hls_only:
            hls_allowed = [
                c for c in allowed
                if ".m3u8" in c.url.lower()
                and c.url.lower().startswith(("http://", "https://"))
            ]
            dropped_non_hls = len(allowed) - len(hls_allowed)
            if dropped_non_hls:
                logger.info(
                    "ValidationAgent: hls_only=True — dropped {} non-HLS / invalid-protocol "
                    "candidates (only direct .m3u8 streams accepted)",
                    dropped_non_hls,
                )
            allowed = hls_allowed
            if not allowed:
                logger.warning("ValidationAgent: no valid HLS (.m3u8) candidates remain after hls_only filter")
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
        referers = {
            c.url: referer
            for c in allowed
            if (referer := _candidate_referer(c)) is not None
        }
        validation_results = await feed_skill.run(
            [c.url for c in allowed], referers=referers
        )
        url_to_val = {r.url: r for r in validation_results}
        validation_result_rows = [
            {
                "url": result.url,
                "status": result.status,
                "status_code": result.status_code,
                "content_type": result.content_type,
                "legitimacy_score": result.legitimacy_score,
                "fail_reason": result.fail_reason,
                "playlist_type": result.playlist_type,
                "variant_streams": result.variant_streams,
            }
            for result in validation_results
        ]
        _append_jsonl(settings.log_dir / "validation_results.jsonl", validation_result_rows)

        n_live    = sum(1 for r in validation_results if r.status == "live")
        n_timeout = sum(1 for r in validation_results if r.fail_reason == "timeout")
        n_dead    = sum(1 for r in validation_results if r.status == "dead")
        n_unknown = sum(1 for r in validation_results if r.status == "unknown")
        logger.info(
            "ValidationAgent: probe results — live={}, dead={}, unknown={}, timeout={}, other={}",
            n_live, n_dead, n_unknown, n_timeout,
            len(allowed) - n_live - n_dead - n_unknown - n_timeout,
        )

        fail_reasons = Counter(
            r.fail_reason
            for r in validation_results
            if r.fail_reason and r.status in ("dead", "unknown")
        )
        if fail_reasons:
            breakdown = "  ".join(f"{reason}={n}" for reason, n in fail_reasons.most_common())
            logger.info("ValidationAgent: failure reasons — {}", breakdown)

        # ── Step 2b: browser second-pass (optional) ───────────────────────────
        _browser_stream_map: dict[str, str] = {}

        if settings.use_browser_validation:
            browser_targets = [
                c.url for c in allowed
                if not c.url.lower().endswith(".m3u8")
                and ".m3u8" not in c.url.lower()
                and url_to_val.get(c.url) is not None
                and url_to_val[c.url].status != "live"
            ]
            if browser_targets:
                from webcam_discovery.skills.browser_validation import (
                    BrowserValidationSkill,
                )
                browser_output = await BrowserValidationSkill().run(browser_targets)

                if browser_output.stream_map:
                    new_stream_urls = list(browser_output.stream_map.values())
                    new_referers = {
                        stream_url: page_url
                        for page_url, stream_url in browser_output.stream_map.items()
                    }
                    new_results = await feed_skill.run(new_stream_urls, referers=new_referers)
                    new_url_to_val = {r.url: r for r in new_results}

                    upgraded = 0
                    for page_url, stream_url in browser_output.stream_map.items():
                        stream_result = new_url_to_val.get(stream_url)
                        if stream_result and stream_result.status == "live":
                            url_to_val[page_url] = stream_result
                            _browser_stream_map[page_url] = stream_url
                            upgraded += 1

                    for page_url in browser_output.offline_pages:
                        existing = url_to_val.get(page_url)
                        if existing is not None:
                            url_to_val[page_url] = existing.model_copy(
                                update={"status": "dead", "fail_reason": "browser_offline_marker"}
                            )

                    logger.info(
                        "ValidationAgent: browser pass upgraded {}/{} pages to live; "
                        "{} pages marked offline",
                        upgraded,
                        len(browser_output.stream_map),
                        len(browser_output.offline_pages),
                    )

        # ── Step 2c: ffprobe frame-level verification (primary status) ──────────
        if settings.use_ffprobe_validation:
            from webcam_discovery.skills.ffprobe_validation import FfprobeValidationSkill

            all_hls_urls = [
                c.url for c in allowed
                if url_to_val.get(c.url) is not None
            ]
            all_hls_urls += [
                stream_url for stream_url in _browser_stream_map.values()
                if ".m3u8" in stream_url.lower()
            ]
            all_hls_urls = list(dict.fromkeys(all_hls_urls))

            logger.info(
                "ValidationAgent: running ffprobe on {} HLS URLs "
                "(concurrency={}) …",
                len(all_hls_urls),
                settings.ffprobe_concurrency,
            )

            ffprobe_skill = FfprobeValidationSkill(
                concurrency=settings.ffprobe_concurrency
            )
            ffprobe_results = await ffprobe_skill.run(all_hls_urls)
            ffprobe_by_url = {r.url: r for r in ffprobe_results}
            ffprobe_log_rows = [
                {
                    "url": fp.url,
                    "stream_status": fp.stream_status,
                    "camera_status": fp.camera_status,
                    "frames_decoded": fp.frames_decoded,
                    "mean_brightness": fp.mean_brightness,
                    "entropy_avg": fp.entropy_avg,
                    "interframe_diff_max": fp.interframe_diff_max,
                    "detail": fp.detail,
                    "ffprobe_available": fp.ffprobe_available,
                }
                for fp in ffprobe_results
            ]
            _append_jsonl(settings.log_dir / "ffprobe_validation.jsonl", ffprobe_log_rows)

            for fp in ffprobe_results:
                if not fp.ffprobe_available:
                    logger.warning(
                        "ValidationAgent ffprobe: NOT AVAILABLE — "
                        "install ffmpeg to enable frame analysis (apt-get install -y ffmpeg)"
                    )
                    break
                logger.info(
                    "ValidationAgent ffprobe: {} | stream_status={} | "
                    "frames={} | brightness={} | entropy={} | diff_max={} | detail={}",
                    fp.url,
                    fp.stream_status or "skipped",
                    fp.frames_decoded,
                    f"{fp.mean_brightness:.1f}" if fp.mean_brightness is not None else "—",
                    f"{fp.entropy_avg:.2f}"     if fp.entropy_avg is not None     else "—",
                    f"{fp.interframe_diff_max:.2f}" if fp.interframe_diff_max is not None else "—",
                    fp.detail,
                )

            n_live = n_unknown = n_dead = n_skipped = 0
            for page_url, stream_url in list(_browser_stream_map.items()) + [
                (u, u) for u in all_hls_urls if u not in _browser_stream_map.values()
            ]:
                fp = ffprobe_by_url.get(stream_url)
                if fp is None:
                    continue
                val_key = page_url if page_url in url_to_val else stream_url
                existing = url_to_val.get(val_key)
                if existing is None:
                    continue

                if fp.stream_status is None:
                    n_skipped += 1
                    continue

                camera_status = fp.camera_status or "unknown"
                fail_reason   = fp.detail if camera_status != "live" else None
                url_to_val[val_key] = existing.model_copy(
                    update={"status": camera_status, "fail_reason": fail_reason}
                )
                if camera_status == "live":
                    n_live += 1
                elif camera_status == "unknown":
                    n_unknown += 1
                else:
                    n_dead += 1

            logger.info(
                "ValidationAgent: ffprobe results — "
                "live={} unknown={} dead={} skipped(ffprobe-unavailable)={}",
                n_live, n_unknown, n_dead, n_skipped,
            )

        playlist_results = []
        for candidate in allowed:
            v = url_to_val.get(candidate.url)
            if v is None or v.status != "live":
                continue
            effective_url = _browser_stream_map.get(candidate.url, candidate.url)
            pa = await analyze_hls_playlist(effective_url, delay_seconds=1.0, timeout=settings.request_timeout)
            playlist_results.append(pa.model_dump())
            if pa.classification != "live_playlist":
                url_to_val[candidate.url] = v.model_copy(update={"status": "unknown", "fail_reason": f"playlist:{pa.classification}"})
        _append_jsonl(settings.log_dir / "hls_playlist_analysis.jsonl", playlist_results)

        # ── Step 3: build record list ─────────────────────────────────────────
        to_enrich: list[tuple[CameraCandidate, object]] = []
        dropped_dead = 0

        for candidate in allowed:
            v = url_to_val.get(candidate.url)
            if v is None:
                continue

            if v.status == "dead":
                dropped_dead += 1
                logger.info(
                    "ValidationAgent: drop dead stream {} (fail_reason={})",
                    candidate.url,
                    v.fail_reason or "unknown",
                )
                continue

            if not settings.use_ffprobe_validation:
                min_legit = settings.min_legitimacy
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

        n_live    = sum(1 for _, v in to_enrich if v.status == "live")
        n_unknown = sum(1 for _, v in to_enrich if v.status == "unknown")
        n_dead    = sum(1 for _, v in to_enrich if v.status == "dead")
        logger.info(
            "ValidationAgent: {} HLS streams to catalog — live={} unknown={} dead={} "
            "dropped_dead={} (ffprobe_validation={})",
            len(to_enrich), n_live, n_unknown, n_dead, dropped_dead,
            settings.use_ffprobe_validation,
        )

        # ── Step 4: geo-enrich (batch async with cache warming) ───────────────
        geo_results = await self._batch_geo_enrich(
            geo_skill, [c for c, _ in to_enrich]
        )

        records: list[CameraRecord] = []

        # ── Step 5: build CameraRecord objects ────────────────────────────────
        for (candidate, v), geo in zip(to_enrich, geo_results):
            effective_url = _browser_stream_map.get(candidate.url, candidate.url)
            feed_type_result = type_skill.run(FeedTypeInput(
                url=effective_url,
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
                _append_jsonl(settings.log_dir / "geocoding_results.jsonl", [{"url": candidate.url, "latitude": geo.latitude, "longitude": geo.longitude, "country": geo.country, "region": geo.region, "continent": geo.continent}])
                latitude   = geo.latitude
                longitude  = geo.longitude
                continent  = geo.continent or "Unknown"
                region     = geo.region
                if geo.country:
                    country = geo.country

            record_id = _make_slug(city, label) or _make_slug("camera", effective_url[-30:])

            notes = candidate.notes or ""
            if latitude is None:
                notes = f"{notes} location_unknown".strip()

            source_refs = list(candidate.source_refs) if candidate.source_refs else []
            if effective_url != candidate.url and candidate.url not in source_refs:
                source_refs.insert(0, candidate.url)

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
                    url=effective_url,
                    feed_type=feed_type_result.feed_type,
                    playlist_type=v.playlist_type,
                    variant_streams=v.variant_streams,
                    source_directory=candidate.source_directory,
                    source_refs=source_refs or [effective_url],
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
                    effective_url,
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

        The queue must be closed by the producer(s) sending a single ``None``
        sentinel value after all items have been put.

        Args:
            queue:      Shared ``asyncio.Queue[CameraCandidate | None]``.
                        ``None`` is the end-of-stream sentinel.
            batch_size: Number of candidates to accumulate before triggering a
                        validation pass.  Default: 100.

        Returns:
            list[CameraRecord] — combined results from all batches.
        """
        pending: list[CameraCandidate] = []
        all_records: list[CameraRecord] = []
        seen_urls: set[str] = set()

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
                await flush()
                break

            # Unwrap before deduplication so wrapper and inner URLs don't both enter
            clean_url = unwrap_player_url(item.url)
            if clean_url not in seen_urls:
                seen_urls.add(clean_url)
                if clean_url != item.url:
                    existing_refs = list(item.source_refs) if item.source_refs else []
                    if item.url not in existing_refs:
                        existing_refs.insert(0, item.url)
                    item = item.model_copy(update={"url": clean_url, "source_refs": existing_refs})
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

        Nominatim mode (use_llm_geodecode=False)
        ----------------------------------------
        Phase 1 — Pre-warm cache for unique city+country pairs.
        Phase 2 — Pre-warm cache for unique country names.
        Phase 3 — Concurrent per-candidate resolution.

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

        # ── Phase 3: per-candidate resolution ─────────────────────────────────
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

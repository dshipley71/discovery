#!/usr/bin/env python3
"""
catalog.py — Record deduplication, coordinate enrichment, and GeoJSON export.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import socket

import httpx
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable
from loguru import logger
from pydantic import BaseModel
from rapidfuzz import fuzz

from webcam_discovery.schemas import CameraRecord


# ── Continent map ──────────────────────────────────────────────────────────────

CONTINENT_MAP: dict[str, str] = {
    # North America
    "United States": "North America",
    "Canada": "North America",
    "Mexico": "North America",
    "Cuba": "North America",
    "Jamaica": "North America",
    "Haiti": "North America",
    "Dominican Republic": "North America",
    "Puerto Rico": "North America",
    "Costa Rica": "North America",
    "Panama": "North America",
    "Guatemala": "North America",
    "Honduras": "North America",
    "El Salvador": "North America",
    "Nicaragua": "North America",
    "Belize": "North America",
    # South America
    "Brazil": "South America",
    "Argentina": "South America",
    "Colombia": "South America",
    "Chile": "South America",
    "Peru": "South America",
    "Venezuela": "South America",
    "Ecuador": "South America",
    "Bolivia": "South America",
    "Paraguay": "South America",
    "Uruguay": "South America",
    "Guyana": "South America",
    "Suriname": "South America",
    # Europe
    "United Kingdom": "Europe",
    "Germany": "Europe",
    "France": "Europe",
    "Spain": "Europe",
    "Italy": "Europe",
    "Netherlands": "Europe",
    "Belgium": "Europe",
    "Switzerland": "Europe",
    "Austria": "Europe",
    "Sweden": "Europe",
    "Norway": "Europe",
    "Denmark": "Europe",
    "Finland": "Europe",
    "Poland": "Europe",
    "Czech Republic": "Europe",
    "Hungary": "Europe",
    "Romania": "Europe",
    "Bulgaria": "Europe",
    "Greece": "Europe",
    "Portugal": "Europe",
    "Ireland": "Europe",
    "Croatia": "Europe",
    "Serbia": "Europe",
    "Slovakia": "Europe",
    "Slovenia": "Europe",
    "Lithuania": "Europe",
    "Latvia": "Europe",
    "Estonia": "Europe",
    "Luxembourg": "Europe",
    "Malta": "Europe",
    "Cyprus": "Europe",
    "Iceland": "Europe",
    "Russia": "Europe",
    "Ukraine": "Europe",
    "Belarus": "Europe",
    "Moldova": "Europe",
    "Albania": "Europe",
    "Bosnia and Herzegovina": "Europe",
    "North Macedonia": "Europe",
    "Montenegro": "Europe",
    "Kosovo": "Europe",
    "Andorra": "Europe",
    "Monaco": "Europe",
    "Liechtenstein": "Europe",
    "San Marino": "Europe",
    "Vatican City": "Europe",
    # Asia
    "Japan": "Asia",
    "China": "Asia",
    "South Korea": "Asia",
    "India": "Asia",
    "Singapore": "Asia",
    "Hong Kong": "Asia",
    "Taiwan": "Asia",
    "Thailand": "Asia",
    "Indonesia": "Asia",
    "Malaysia": "Asia",
    "Philippines": "Asia",
    "Vietnam": "Asia",
    "Cambodia": "Asia",
    "Myanmar": "Asia",
    "Bangladesh": "Asia",
    "Sri Lanka": "Asia",
    "Nepal": "Asia",
    "Pakistan": "Asia",
    "Afghanistan": "Asia",
    "Iran": "Asia",
    "Iraq": "Asia",
    "Saudi Arabia": "Asia",
    "United Arab Emirates": "Asia",
    "Qatar": "Asia",
    "Kuwait": "Asia",
    "Bahrain": "Asia",
    "Oman": "Asia",
    "Yemen": "Asia",
    "Jordan": "Asia",
    "Lebanon": "Asia",
    "Israel": "Asia",
    "Syria": "Asia",
    "Turkey": "Asia",
    "Kazakhstan": "Asia",
    "Uzbekistan": "Asia",
    "Turkmenistan": "Asia",
    "Kyrgyzstan": "Asia",
    "Tajikistan": "Asia",
    "Mongolia": "Asia",
    "North Korea": "Asia",
    "Laos": "Asia",
    "Brunei": "Asia",
    "Timor-Leste": "Asia",
    "Maldives": "Asia",
    "Bhutan": "Asia",
    # Africa
    "South Africa": "Africa",
    "Nigeria": "Africa",
    "Kenya": "Africa",
    "Ethiopia": "Africa",
    "Egypt": "Africa",
    "Morocco": "Africa",
    "Tunisia": "Africa",
    "Algeria": "Africa",
    "Libya": "Africa",
    "Sudan": "Africa",
    "Ghana": "Africa",
    "Tanzania": "Africa",
    "Uganda": "Africa",
    "Mozambique": "Africa",
    "Madagascar": "Africa",
    "Cameroon": "Africa",
    "Ivory Coast": "Africa",
    "Angola": "Africa",
    "Senegal": "Africa",
    "Zimbabwe": "Africa",
    "Zambia": "Africa",
    "Botswana": "Africa",
    "Namibia": "Africa",
    "Rwanda": "Africa",
    "Mauritius": "Africa",
    "Seychelles": "Africa",
    "Cape Verde": "Africa",
    "Johannesburg": "Africa",
    # Oceania
    "Australia": "Oceania",
    "New Zealand": "Oceania",
    "Papua New Guinea": "Oceania",
    "Fiji": "Oceania",
    "Solomon Islands": "Oceania",
    "Vanuatu": "Oceania",
    "Samoa": "Oceania",
    "Tonga": "Oceania",
    "Kiribati": "Oceania",
    "Micronesia": "Oceania",
    "Palau": "Oceania",
    "Marshall Islands": "Oceania",
    "Nauru": "Oceania",
    "Tuvalu": "Oceania",
}


def _country_to_continent(country: str) -> str:
    """Look up continent for a country name."""
    return CONTINENT_MAP.get(country, "Unknown")


# ── URL normalization ──────────────────────────────────────────────────────────

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "_ga", "_gl", "yclid", "twclid",
})


def _normalize_url(url: str) -> str:
    """Strip tracking params, normalize protocol and trailing slashes."""
    try:
        parsed = urlparse(url)
        # Normalize protocol
        scheme = "https"
        # Remove www.
        netloc = parsed.netloc.lstrip("www.")
        # Strip tracking query params
        params = parse_qs(parsed.query, keep_blank_values=True)
        clean_params = {k: v for k, v in params.items() if k.lower() not in _TRACKING_PARAMS}
        # Rebuild query string sorted for stability
        query = urlencode({k: v[0] for k, v in sorted(clean_params.items())}) if clean_params else ""
        # Strip trailing slash from path
        path = parsed.path.rstrip("/")
        normalized = urlunparse((scheme, netloc, path, parsed.params, query, ""))
        return normalized.lower()
    except Exception:
        return url.lower()


def _haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two coordinates in meters."""
    R = 6_371_000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── I/O Models ────────────────────────────────────────────────────────────────

class DeduplicationInput(BaseModel):
    """Input for deduplication skill."""

    candidate_record: CameraRecord
    existing_catalog: list[CameraRecord]


class DeduplicationOutput(BaseModel):
    """Output from deduplication skill."""

    is_duplicate: bool
    canonical_record: Optional[CameraRecord] = None
    merged_record: Optional[CameraRecord] = None


class GeoEnrichmentInput(BaseModel):
    """Input for geo enrichment skill."""

    city: Optional[str] = None
    country: Optional[str] = None
    label: Optional[str] = None
    url: Optional[str] = None  # camera URL; used as last-resort IP geolocation source


class GeoEnrichmentOutput(BaseModel):
    """Output from geo enrichment skill."""

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    country: Optional[str] = None
    region: Optional[str] = None
    continent: Optional[str] = None
    confidence: str = "low"


class GeoJSONExportInput(BaseModel):
    """Input for GeoJSON export skill."""

    cameras: list[CameraRecord]
    output_path: Path


class GeoJSONExportOutput(BaseModel):
    """Output from GeoJSON export skill."""

    exported: int
    skipped: int
    path: str


# ── DeduplicationSkill ─────────────────────────────────────────────────────────

class DeduplicationSkill:
    """Identify and merge duplicate camera records in the catalog."""

    FUZZY_THRESHOLD = 85
    PROXIMITY_METERS = 50

    def run(self, input: DeduplicationInput) -> DeduplicationOutput:
        """
        Check candidate_record against existing_catalog for duplicates.

        Priority order: URL normalization → coordinate proximity → fuzzy label match.

        Args:
            input: DeduplicationInput with candidate_record and existing_catalog.

        Returns:
            DeduplicationOutput indicating if duplicate and the merged record.
        """
        candidate = input.candidate_record
        candidate_url_norm = _normalize_url(candidate.url)

        for existing in input.existing_catalog:
            # 1. URL normalization match
            if _normalize_url(existing.url) == candidate_url_norm:
                merged = self._merge(existing, candidate)
                logger.debug("Dedup URL match: {} ≡ {}", candidate.url, existing.url)
                return DeduplicationOutput(
                    is_duplicate=True,
                    canonical_record=existing,
                    merged_record=merged,
                )

            # 2. Coordinate proximity match (within 50m, same city)
            if (
                existing.latitude is not None
                and existing.longitude is not None
                and candidate.latitude is not None
                and candidate.longitude is not None
                and existing.city.lower() == candidate.city.lower()
            ):
                dist = _haversine_distance_m(
                    existing.latitude, existing.longitude,
                    candidate.latitude, candidate.longitude,
                )
                if dist <= self.PROXIMITY_METERS:
                    merged = self._merge(existing, candidate)
                    logger.debug("Dedup proximity match: {} ≡ {} ({:.1f}m)", candidate.id, existing.id, dist)
                    return DeduplicationOutput(
                        is_duplicate=True,
                        canonical_record=existing,
                        merged_record=merged,
                    )

            # 3. Fuzzy label match (same city, >85% similarity)
            if existing.city.lower() == candidate.city.lower():
                similarity = fuzz.ratio(
                    existing.label.lower(), candidate.label.lower()
                )
                if similarity > self.FUZZY_THRESHOLD:
                    merged = self._merge(existing, candidate)
                    logger.debug(
                        "Dedup fuzzy match: '{}' ≈ '{}' ({:.0f}%)",
                        candidate.label, existing.label, similarity,
                    )
                    return DeduplicationOutput(
                        is_duplicate=True,
                        canonical_record=existing,
                        merged_record=merged,
                    )

        return DeduplicationOutput(is_duplicate=False)

    def _merge(self, canonical: CameraRecord, candidate: CameraRecord) -> CameraRecord:
        """Merge candidate into canonical, keeping canonical's core data."""
        merged_refs = list({*canonical.source_refs, candidate.url, *candidate.source_refs})
        # Use newer last_verified if available
        last_verified = canonical.last_verified
        if candidate.last_verified and (
            not last_verified or candidate.last_verified > last_verified
        ):
            last_verified = candidate.last_verified

        return canonical.model_copy(update={
            "source_refs": merged_refs,
            "last_verified": last_verified,
        })


# ── GeoEnrichmentSkill ─────────────────────────────────────────────────────────

class GeoEnrichmentSkill:
    """
    Attach geographic metadata to camera records lacking coordinates.

    Fallback chain (stops at first successful result):
    1. City + country geocoding via Nominatim
    2. Label text geocoding (may contain place names like "Eiffel Tower Paris")
    3. IP geolocation of the camera hostname via ip-api.com
    4. Country-center geocoding when only country is known
    """

    _IP_API_URL = "http://ip-api.com/json/{host}"
    _IP_API_FIELDS = "status,lat,lon,country,regionName,city"

    def __init__(self) -> None:
        """Initialize geocoder with a polite user-agent."""
        self._geocoder = Nominatim(user_agent="webcam_discovery_bot/1.0")
        self._cache: dict[str, Optional[GeoEnrichmentOutput]] = {}

    async def run(self, input: GeoEnrichmentInput) -> GeoEnrichmentOutput:
        """
        Resolve coordinates via a multi-strategy fallback chain.

        Args:
            input: GeoEnrichmentInput with any combination of city, country, label, url.

        Returns:
            GeoEnrichmentOutput with coordinates and geographic metadata, or empty
            GeoEnrichmentOutput if all strategies fail.
        """
        city = (input.city or "").strip()
        country = (input.country or "").strip()
        label = (input.label or "").strip()

        # Strategy 1: city + country
        if city and city.lower() not in ("unknown", ""):
            query = f"{city}, {country}" if country else city
            result = await self._geocode_nominatim(query, cache_key=f"city:{city}|{country}")
            if result.latitude is not None:
                return result

        # Strategy 2: label text (may encode a landmark or place name)
        if label and label.lower() not in ("unknown", ""):
            result = await self._geocode_nominatim(label, cache_key=f"label:{label}")
            if result.latitude is not None:
                return result

        # Strategy 3: IP geolocation from camera URL hostname
        if input.url:
            result = await self._ip_geolocate(input.url)
            if result.latitude is not None:
                return result

        # Strategy 4: country center
        if country and country.lower() not in ("unknown", ""):
            result = await self._geocode_nominatim(country, cache_key=f"country:{country}")
            if result.latitude is not None:
                return result

        return GeoEnrichmentOutput()

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _geocode_nominatim(
        self, query: str, cache_key: Optional[str] = None
    ) -> GeoEnrichmentOutput:
        """Geocode a free-text query via Nominatim; results are cached."""
        key = cache_key or query
        if key in self._cache:
            cached = self._cache[key]
            return cached if cached is not None else GeoEnrichmentOutput()

        try:
            loop = asyncio.get_event_loop()
            location = await loop.run_in_executor(
                None,
                lambda: self._geocoder.geocode(
                    query, exactly_one=True, addressdetails=True, language="en"
                ),
            )
        except (GeocoderTimedOut, GeocoderUnavailable) as exc:
            logger.warning("GeoEnrichmentSkill geocoder error for '{}': {}", query, exc)
            self._cache[key] = None
            return GeoEnrichmentOutput()
        except Exception as exc:
            logger.warning("GeoEnrichmentSkill unexpected error for '{}': {}", query, exc)
            self._cache[key] = None
            return GeoEnrichmentOutput()

        if location is None:
            logger.debug("GeoEnrichmentSkill: no Nominatim result for '{}'", query)
            self._cache[key] = None
            return GeoEnrichmentOutput()

        address = location.raw.get("address", {})
        found_country = address.get("country") or ""
        region = (
            address.get("state")
            or address.get("region")
            or address.get("county")
            or None
        )
        result = GeoEnrichmentOutput(
            latitude=location.latitude,
            longitude=location.longitude,
            country=found_country,
            region=region,
            continent=_country_to_continent(found_country),
            confidence="high" if found_country else "medium",
        )
        self._cache[key] = result
        logger.debug(
            "GeoEnrichmentSkill: Nominatim '{}' → ({:.4f}, {:.4f}) {}",
            query, result.latitude, result.longitude, found_country,
        )
        return result

    async def _ip_geolocate(self, url: str) -> GeoEnrichmentOutput:
        """
        Resolve camera hostname to approximate coordinates via ip-api.com.

        ip-api.com accepts both IP addresses and hostnames, returns lat/lon for
        the server's registered location — a good proxy for the camera location.
        Confidence is set to 'low' since ISP routing may not match camera placement.
        """
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if not host:
                return GeoEnrichmentOutput()

            cache_key = f"ip:{host}"
            if cache_key in self._cache:
                cached = self._cache[cache_key]
                return cached if cached is not None else GeoEnrichmentOutput()

            api_url = self._IP_API_URL.format(host=host)
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(api_url, params={"fields": self._IP_API_FIELDS})
                data = resp.json()

            if data.get("status") != "success":
                logger.debug("GeoEnrichmentSkill: ip-api no result for '{}'", host)
                self._cache[cache_key] = None
                return GeoEnrichmentOutput()

            country = data.get("country") or ""
            result = GeoEnrichmentOutput(
                latitude=data.get("lat"),
                longitude=data.get("lon"),
                country=country,
                region=data.get("regionName") or None,
                continent=_country_to_continent(country),
                confidence="low",
            )
            self._cache[cache_key] = result
            logger.debug(
                "GeoEnrichmentSkill: IP geo '{}' → ({:.4f}, {:.4f}) {} [{}]",
                host, result.latitude, result.longitude, country,
                data.get("city", ""),
            )
            return result

        except Exception as exc:
            logger.debug("GeoEnrichmentSkill: IP geo failed for '{}': {}", url, exc)
            return GeoEnrichmentOutput()


# ── GeoJSONExportSkill ─────────────────────────────────────────────────────────

class GeoJSONExportSkill:
    """Serialize validated CameraRecord objects directly to camera.geojson."""

    def run(self, input: GeoJSONExportInput) -> GeoJSONExportOutput:
        """
        Build GeoJSON FeatureCollection and write to output_path.

        Skips records missing lat/lon. Coordinates are [longitude, latitude] per RFC 7946.

        Args:
            input: GeoJSONExportInput with cameras list and output_path.

        Returns:
            GeoJSONExportOutput with counts and path.
        """
        features: list[dict] = []
        skipped = 0
        live_count = dead_count = unknown_count = 0

        for record in input.cameras:
            if record.latitude is None or record.longitude is None:
                logger.warning("GeoJSONExportSkill: skipping '{}' — missing coordinates", record.id)
                skipped += 1
                continue

            props = record.model_dump(exclude={"latitude", "longitude"})

            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [record.longitude, record.latitude],  # RFC 7946: lon, lat
                },
                "properties": props,
            })

            if record.status == "live":
                live_count += 1
            elif record.status == "dead":
                dead_count += 1
            else:
                unknown_count += 1

        geojson: dict = {
            "type": "FeatureCollection",
            "features": features,
            "metadata": {
                "total": len(features),
                "live": live_count,
                "dead": dead_count,
                "unknown": unknown_count,
                "unmapped": skipped,
                "generated": datetime.now(timezone.utc).isoformat(),
            },
        }

        output_path = input.output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(geojson, f, indent=2, default=str, ensure_ascii=False)

        logger.info(
            "GeoJSONExportSkill: {} features written to '{}', {} skipped",
            len(features), output_path, skipped,
        )
        return GeoJSONExportOutput(
            exported=len(features),
            skipped=skipped,
            path=str(output_path),
        )


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        city = sys.argv[1] if len(sys.argv) > 1 else "London"
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city=city))
        logger.info("{}", result.model_dump())

    asyncio.run(_main())

#!/usr/bin/env python3
"""
test_geo_enrichment.py — Unit tests for GeoEnrichmentSkill.
Nominatim geocoder is mocked; no live network calls.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from webcam_discovery.skills.catalog import GeoEnrichmentSkill, GeoEnrichmentInput


def _make_nominatim_location(lat: float, lon: float, country: str, state: str = "") -> MagicMock:
    """Build a mock Nominatim Location object."""
    loc = MagicMock()
    loc.latitude = lat
    loc.longitude = lon
    loc.raw = {
        "address": {
            "country": country,
            "state": state,
        }
    }
    return loc


@pytest.mark.asyncio
async def test_record_with_known_city_geocoded():
    """Valid city → coordinates populated via mocked Nominatim."""
    mock_location = _make_nominatim_location(
        lat=51.5074, lon=-0.1278, country="United Kingdom", state="England"
    )

    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        return_value=mock_location,
    ):
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city="London", country="United Kingdom"))

    assert result.latitude == pytest.approx(51.5074, abs=0.01)
    assert result.longitude == pytest.approx(-0.1278, abs=0.01)
    assert result.country == "United Kingdom"
    assert result.continent == "Europe"
    assert result.region == "England"


@pytest.mark.asyncio
async def test_unknown_city_skipped():
    """Nominatim returns None → empty result (no coordinates)."""
    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        return_value=None,
    ):
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city="XyzNonExistentCity99999"))

    assert result.latitude is None
    assert result.longitude is None
    assert result.country is None


@pytest.mark.asyncio
async def test_continent_mapping():
    """Country is mapped to correct continent via CONTINENT_MAP."""
    mock_location = _make_nominatim_location(
        lat=35.6762, lon=139.6503, country="Japan", state="Tokyo"
    )

    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        return_value=mock_location,
    ):
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city="Tokyo", country="Japan"))

    assert result.continent == "Asia"


@pytest.mark.asyncio
async def test_geocoder_timeout_returns_empty():
    """Geocoder timeout → empty result, no crash."""
    from geopy.exc import GeocoderTimedOut

    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        side_effect=GeocoderTimedOut("timed out"),
    ):
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city="Somewhere"))

    assert result.latitude is None
    assert result.longitude is None


@pytest.mark.asyncio
async def test_caching_same_city():
    """Same city queried twice — geocoder called only once (cached)."""
    mock_location = _make_nominatim_location(
        lat=48.8566, lon=2.3522, country="France", state="Île-de-France"
    )

    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        return_value=mock_location,
    ) as mock_geocode:
        skill = GeoEnrichmentSkill()
        result1 = await skill.run(GeoEnrichmentInput(city="Paris", country="France"))
        result2 = await skill.run(GeoEnrichmentInput(city="Paris", country="France"))

    # Geocoder should only be called once due to caching
    assert mock_geocode.call_count == 1
    assert result1.latitude == result2.latitude


@pytest.mark.asyncio
async def test_us_city_continent_north_america():
    """US city → continent=North America."""
    mock_location = _make_nominatim_location(
        lat=40.7128, lon=-74.0060, country="United States", state="New York"
    )

    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        return_value=mock_location,
    ):
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city="New York City", country="United States"))

    assert result.continent == "North America"


@pytest.mark.asyncio
async def test_australia_continent_oceania():
    """Australia → continent=Oceania."""
    mock_location = _make_nominatim_location(
        lat=-33.8688, lon=151.2093, country="Australia", state="New South Wales"
    )

    with patch(
        "webcam_discovery.skills.catalog.Nominatim.geocode",
        return_value=mock_location,
    ):
        skill = GeoEnrichmentSkill()
        result = await skill.run(GeoEnrichmentInput(city="Sydney", country="Australia"))

    assert result.continent == "Oceania"

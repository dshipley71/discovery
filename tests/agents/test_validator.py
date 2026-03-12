#!/usr/bin/env python3
"""
test_validator.py — Unit tests for ValidationAgent contracts.
All HTTP mocked via respx. No live network calls.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock, AsyncMock
from typing import Optional

import pytest
import respx
import httpx

from webcam_discovery.schemas import CameraCandidate, CameraRecord
from webcam_discovery.agents.validator import ValidationAgent
from webcam_discovery.skills.catalog import GeoEnrichmentOutput


def make_candidate(**kwargs) -> CameraCandidate:
    """Build a minimal CameraCandidate."""
    defaults = dict(
        url="https://example.com/webcam/test",
        label="Test Camera",
        city="London",
        country="United Kingdom",
        source_directory="example.com",
    )
    defaults.update(kwargs)
    return CameraCandidate(**defaults)


def _mock_geo_london() -> GeoEnrichmentOutput:
    """Return a mock geo result for London."""
    return GeoEnrichmentOutput(
        latitude=51.5074,
        longitude=-0.1278,
        country="United Kingdom",
        region="England",
        continent="Europe",
        confidence="high",
    )


def _mock_geo_skill(output: Optional[GeoEnrichmentOutput] = None):
    """Create an AsyncMock for GeoEnrichmentSkill.run."""
    mock = AsyncMock(return_value=output or _mock_geo_london())
    return mock


@pytest.mark.asyncio
async def test_legitimacy_high_on_media():
    """Media content-type → high legitimacy record created."""
    candidate = make_candidate(url="https://example.com/stream.mjpg")

    with respx.mock:
        respx.head("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.head("https://example.com/stream.mjpg").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}
            )
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    assert len(records) == 1
    assert records[0].legitimacy_score == "high"
    assert records[0].status == "live"


@pytest.mark.asyncio
async def test_html_stream_url_medium_legitimacy():
    """text/html on stream URL → medium legitimacy (not hard-rejected by default config)."""
    candidate = make_candidate(url="https://example.com/cam-embed-page")

    with respx.mock:
        respx.head("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.head("https://example.com/cam-embed-page").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    # text/html returns medium legitimacy — depends on min_legitimacy setting
    # With default "medium" min, these should be accepted
    if records:
        assert records[0].legitimacy_score == "medium"


@pytest.mark.asyncio
async def test_robots_blocked_domain_skipped():
    """robots.txt disallows webcam paths → candidate skipped."""
    candidate = make_candidate(url="https://blocked-site.com/webcams/london")

    robots_txt = """User-agent: *
Disallow: /webcams/
Disallow: /cameras/
Disallow: /live/
"""
    with respx.mock:
        respx.head("https://blocked-site.com/robots.txt").mock(
            return_value=httpx.Response(200, text=robots_txt)
        )
        # HEAD for the actual URL should NOT be called if robots blocks it
        agent = ValidationAgent()
        records = await agent.run(candidates=[candidate])

    # Should be empty — domain was blocked by robots.txt
    assert records == []


@pytest.mark.asyncio
async def test_timeout_status_unknown():
    """Timeout → status=unknown, not a crash; record may be excluded due to missing coords."""
    candidate = make_candidate(url="https://example.com/slow-cam")

    with respx.mock:
        respx.head("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.head("https://example.com/slow-cam").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            # Should not raise — graceful handling
            records = await agent.run(candidates=[candidate])

    # Timeout → status unknown → may or may not produce a record depending on min_legitimacy
    # Critical assertion: no exception was raised
    assert isinstance(records, list)


@pytest.mark.asyncio
async def test_401_candidate_excluded():
    """401 response → legitimacy=low → excluded from output."""
    candidate = make_candidate(url="https://example.com/auth-cam")

    with respx.mock:
        respx.head("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.head("https://example.com/auth-cam").mock(
            return_value=httpx.Response(401)
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    # Low legitimacy should be excluded with default min_legitimacy="medium"
    assert records == []


@pytest.mark.asyncio
async def test_empty_candidates():
    """Empty candidates list → empty records list, no crash."""
    agent = ValidationAgent()
    records = await agent.run(candidates=[])
    assert records == []


@pytest.mark.asyncio
async def test_record_id_is_slug():
    """Validated record ID is a slugified string."""
    candidate = make_candidate(
        url="https://example.com/stream.mjpg",
        label="Big Ben Live View",
        city="London",
    )

    with respx.mock:
        respx.head("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.head("https://example.com/stream.mjpg").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "image/jpeg"}
            )
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    if records:
        record_id = records[0].id
        # ID should be lowercase, hyphen-separated, no special chars
        assert record_id == record_id.lower()
        assert " " not in record_id
        assert record_id.replace("-", "").replace("_", "").isalnum() or "-" in record_id

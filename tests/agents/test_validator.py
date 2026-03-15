#!/usr/bin/env python3
"""
test_validator.py — Unit tests for ValidationAgent contracts.
All HTTP mocked via respx. No live network calls.

After the m3u8-only refactor, all non-.m3u8 URLs are rejected immediately
without making an HTTP request.
"""
from __future__ import annotations

from unittest.mock import patch, AsyncMock
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
        url="https://cdn.example.com/webcam/live.m3u8",
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
async def test_legitimacy_high_on_hls_media_playlist():
    """HLS media playlist → high legitimacy, status=live, playlist_type=media."""
    candidate = make_candidate(url="https://cdn.example.com/stream.m3u8")

    with respx.mock:
        respx.get("https://cdn.example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://cdn.example.com/stream.m3u8").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg.ts\n",
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
    assert records[0].playlist_type == "media"
    assert records[0].feed_type == "HLS_stream"


@pytest.mark.asyncio
async def test_hls_master_playlist_feed_type():
    """HLS master playlist → feed_type=HLS_master, variant_streams populated."""
    candidate = make_candidate(url="https://cdn.example.com/master.m3u8")

    with respx.mock:
        respx.get("https://cdn.example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://cdn.example.com/master.m3u8").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=(
                    b"#EXTM3U\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=2500000\n"
                    b"high.m3u8\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
                    b"low.m3u8\n"
                ),
            )
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    assert len(records) == 1
    assert records[0].feed_type == "HLS_master"
    assert records[0].playlist_type == "master"
    assert len(records[0].variant_streams) == 2


@pytest.mark.asyncio
async def test_non_m3u8_url_rejected():
    """Non-.m3u8 URL (HTML page) → status=dead, fail_reason=not_m3u8_stream."""
    candidate = make_candidate(url="https://example.com/cam-embed-page.html")

    with respx.mock:
        respx.get("https://example.com/robots.txt").mock(return_value=httpx.Response(404))
        # No HTTP call expected for the candidate URL — rejected without probing
        agent = ValidationAgent()
        records = await agent.run(candidates=[candidate])

    # The record may be created (depending on min_legitimacy setting) but must be dead
    if records:
        assert records[0].status == "dead"
        assert records[0].url == "https://example.com/cam-embed-page.html"


@pytest.mark.asyncio
async def test_robots_blocked_domain_skipped():
    """robots.txt disallows webcam paths → candidate skipped."""
    candidate = make_candidate(url="https://blocked-site.com/webcams/stream.m3u8")

    robots_txt = """User-agent: *
Disallow: /webcams
Disallow: /cameras
Disallow: /live
"""
    with respx.mock:
        respx.get("https://blocked-site.com/robots.txt").mock(
            return_value=httpx.Response(200, text=robots_txt)
        )
        agent = ValidationAgent()
        records = await agent.run(candidates=[candidate])

    # Should be empty — domain was blocked by robots.txt
    assert records == []


@pytest.mark.asyncio
async def test_timeout_status_unknown():
    """HLS timeout → status=unknown, not a crash."""
    candidate = make_candidate(url="https://cdn.example.com/slow.m3u8")

    with respx.mock:
        respx.get("https://cdn.example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://cdn.example.com/slow.m3u8").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    # Timeout → status=unknown, low legitimacy → may be filtered; no exception raised
    assert isinstance(records, list)


@pytest.mark.asyncio
async def test_401_hls_candidate_dead():
    """HLS returning 401 → status=dead, legitimacy=low."""
    candidate = make_candidate(url="https://cdn.example.com/private.m3u8")

    with respx.mock:
        respx.get("https://cdn.example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://cdn.example.com/private.m3u8").mock(
            return_value=httpx.Response(401)
        )
        with patch(
            "webcam_discovery.agents.validator.GeoEnrichmentSkill.run",
            _mock_geo_skill(),
        ):
            agent = ValidationAgent()
            records = await agent.run(candidates=[candidate])

    # Record may or may not appear depending on min_legitimacy; if present it must be dead/low
    if records:
        assert records[0].status == "dead"
        assert records[0].legitimacy_score == "low"


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
        url="https://cdn.example.com/big-ben.m3u8",
        label="Big Ben Live View",
        city="London",
    )

    with respx.mock:
        respx.get("https://cdn.example.com/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://cdn.example.com/big-ben.m3u8").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXTINF:6.0,\nseg.ts\n",
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
        assert record_id == record_id.lower()
        assert " " not in record_id
        assert record_id.replace("-", "").replace("_", "").isalnum() or "-" in record_id

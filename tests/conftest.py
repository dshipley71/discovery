#!/usr/bin/env python3
"""
conftest.py — Shared pytest fixtures for the webcam discovery test suite.
All HTTP is mocked here — no live network calls in tests/.
"""
from __future__ import annotations
import pytest
import respx
import httpx
from webcam_discovery.schemas import CameraCandidate, CameraRecord


@pytest.fixture
def sample_candidate() -> CameraCandidate:
    """Minimal valid CameraCandidate for unit tests."""
    return CameraCandidate(
        url="https://example.com/webcam/times-square",
        label="Times Square Test Cam",
        city="New York City",
        country="United States",
        source_directory="example.com",
    )


@pytest.fixture
def sample_record() -> CameraRecord:
    """Minimal valid CameraRecord for unit tests."""
    return CameraRecord(
        id="test-new-york-times-square",
        label="Times Square Test Cam",
        city="New York City",
        country="United States",
        continent="North America",
        latitude=40.7580,
        longitude=-73.9855,
        url="https://cdn.example.com/times-square/live.m3u8",
        feed_type="HLS_stream",
        playlist_type="media",
        legitimacy_score="high",
        status="live",
        last_verified="2025-03-10",
        source_directory="example.com",
    )


@pytest.fixture
def mock_live_stream():
    """Mock a live HLS stream response."""
    with respx.mock:
        respx.get("https://cdn.example.com/live.m3u8").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXTINF:6.0,\nseg.ts\n",
            )
        )
        yield


@pytest.fixture
def mock_dead_stream():
    """Mock a dead HLS stream URL."""
    with respx.mock:
        respx.get("https://cdn.example.com/dead.m3u8").mock(
            return_value=httpx.Response(404)
        )
        yield

#!/usr/bin/env python3
"""
test_feed_validation.py — Unit tests for FeedValidationSkill.
All HTTP mocked via respx. No live network calls.
"""
from __future__ import annotations

import pytest
import respx
import httpx

from webcam_discovery.skills.validation import FeedValidationSkill


@pytest.mark.asyncio
async def test_media_content_type_is_live():
    """HEAD returning multipart/x-mixed-replace → status=live, legitimacy=high."""
    url = "https://example.com/stream.mjpg"
    with respx.mock:
        respx.head(url).mock(
            return_value=httpx.Response(
                200, headers={"content-type": "multipart/x-mixed-replace; boundary=--frame"}
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "live"
    assert r.legitimacy_score == "high"
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_html_content_type_on_stream():
    """HEAD returning text/html → status=live, legitimacy=medium (not hard-rejected)."""
    url = "https://example.com/cam-page"
    with respx.mock:
        respx.head(url).mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "live"
    assert r.legitimacy_score == "medium"


@pytest.mark.asyncio
async def test_404_is_dead():
    """HEAD returning 404 → status=dead."""
    url = "https://example.com/dead.mjpg"
    with respx.mock:
        respx.head(url).mock(return_value=httpx.Response(404))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "dead"
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_timeout_is_unknown():
    """Timeout → status=unknown, not a crash."""
    url = "https://example.com/slow-stream"
    with respx.mock:
        respx.head(url).mock(side_effect=httpx.TimeoutException("timed out"))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "unknown"
    assert r.fail_reason is not None


@pytest.mark.asyncio
async def test_youtube_nocookie_exempt():
    """YouTube nocookie embed URL → status=live without content-type check (no HTTP call)."""
    url = "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&mute=1"
    # No HTTP mock needed — nocookie URLs are exempt from network checks
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "live"


@pytest.mark.asyncio
async def test_401_is_dead():
    """HEAD returning 401 → status=dead, legitimacy=low."""
    url = "https://example.com/auth-stream"
    with respx.mock:
        respx.head(url).mock(return_value=httpx.Response(401))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.legitimacy_score == "low"


@pytest.mark.asyncio
async def test_www_authenticate_header_low_legitimacy():
    """WWW-Authenticate header present → legitimacy=low, status=dead."""
    url = "https://example.com/protected-stream"
    with respx.mock:
        respx.head(url).mock(
            return_value=httpx.Response(
                200,
                headers={
                    "content-type": "video/mp4",
                    "www-authenticate": "Basic realm='cam'",
                },
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.legitimacy_score == "low"
    assert r.status == "dead"


@pytest.mark.asyncio
async def test_multiple_urls():
    """Multiple URLs validated concurrently — all results returned."""
    urls = [
        "https://example.com/stream1.mjpg",
        "https://example.com/stream2.mjpg",
        "https://example.com/stream3.mjpg",
    ]
    with respx.mock:
        respx.head(urls[0]).mock(
            return_value=httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace"})
        )
        respx.head(urls[1]).mock(return_value=httpx.Response(404))
        respx.head(urls[2]).mock(side_effect=httpx.TimeoutException("timeout"))

        skill = FeedValidationSkill()
        results = await skill.run(urls)

    assert len(results) == 3
    result_map = {r.url: r for r in results}
    assert result_map[urls[0]].status == "live"
    assert result_map[urls[1]].status == "dead"
    assert result_map[urls[2]].status == "unknown"


@pytest.mark.asyncio
async def test_hls_content_type_is_live_high():
    """HLS content-type → status=live, legitimacy=high."""
    url = "https://example.com/stream.m3u8"
    with respx.mock:
        respx.head(url).mock(
            return_value=httpx.Response(
                200, headers={"content-type": "application/vnd.apple.mpegurl"}
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"

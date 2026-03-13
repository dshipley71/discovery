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
async def test_html_without_live_player_is_rejected():
    """HTML page with no live-player patterns → status=dead, legitimacy=low."""
    url = "https://example.com/cam-page"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                text="<html><body><p>Welcome to our webcam page</p></body></html>",
                headers={"content-type": "text/html; charset=utf-8"},
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.legitimacy_score == "low"


@pytest.mark.asyncio
async def test_html_with_live_player_passes():
    """HTML page containing HLS.js player code → legitimacy=high, status=live."""
    url = "https://example.com/live-cam"
    html = (
        "<html><head><script src='hls.js'></script></head>"
        "<body><script>var hls=new Hls(); hls.loadSource('stream.m3u8');</script></body></html>"
    )
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200, text=html, headers={"content-type": "text/html"}
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"


@pytest.mark.asyncio
async def test_mp4_url_is_rejected():
    """MP4 URL → legitimacy=low, status=dead (static file, not live stream)."""
    url = "https://example.com/recording.mp4"
    with respx.mock:
        # No HTTP mock needed — MP4 is rejected before any request is made
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.legitimacy_score == "low"
    assert r.status == "dead"
    assert r.fail_reason == "not_live_stream"


@pytest.mark.asyncio
async def test_404_is_dead():
    """HEAD returning 404 → status=dead."""
    url = "https://example.com/dead.mjpg"
    with respx.mock:
        respx.head(url).mock(return_value=httpx.Response(
            404, headers={"content-type": "text/html"}
        ))
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
    """YouTube nocookie embed URL → status=live, legitimacy=high (no HTTP call)."""
    url = "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&mute=1"
    # No HTTP mock needed — YouTube URLs are exempt from network checks
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "live"
    assert r.legitimacy_score == "high"


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
    """WWW-Authenticate header on MJPEG HEAD → legitimacy=low, status=dead."""
    url = "https://example.com/protected-cam.mjpg"
    with respx.mock:
        respx.head(url).mock(
            return_value=httpx.Response(
                200,
                headers={
                    "content-type": "image/jpeg",
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
        respx.head(urls[1]).mock(return_value=httpx.Response(
            404, headers={"content-type": "text/html"}
        ))
        respx.head(urls[2]).mock(side_effect=httpx.TimeoutException("timeout"))

        skill = FeedValidationSkill()
        results = await skill.run(urls)

    assert len(results) == 3
    result_map = {r.url: r for r in results}
    assert result_map[urls[0]].status == "live"
    assert result_map[urls[1]].status == "dead"
    assert result_map[urls[2]].status == "unknown"


@pytest.mark.asyncio
async def test_hls_magic_bytes_is_live_high():
    """HLS playlist with #EXTM3U magic bytes → status=live, legitimacy=high."""
    url = "https://example.com/stream.m3u8"
    playlist = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                content=playlist,
                headers={"content-type": "application/vnd.apple.mpegurl"},
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"

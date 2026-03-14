#!/usr/bin/env python3
"""
test_feed_validation.py — Unit tests for FeedValidationSkill.
All HTTP mocked via respx. No live network calls.

Policy under test: only HLS (.m3u8) and MJPEG (.mjpeg) stream URLs are accepted
as active camera links. YouTube embeds, MP4 files, static images, and HTML pages
without an embedded stream URL are all rejected (status=dead).
"""
from __future__ import annotations

import pytest
import respx
import httpx

from webcam_discovery.skills.validation import FeedValidationSkill


# ── MJPEG probes ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mjpeg_multipart_is_live_high():
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
async def test_mjpeg_404_is_dead():
    """MJPEG HEAD returning 404 → status=dead."""
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
async def test_mjpeg_timeout_is_unknown():
    """MJPEG timeout → status=unknown, not a crash."""
    url = "https://example.com/slow.mjpeg"
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
async def test_mjpeg_401_is_dead():
    """MJPEG HEAD returning 401 → status=dead, legitimacy=low."""
    url = "https://example.com/auth-stream.mjpg"
    with respx.mock:
        respx.head(url).mock(return_value=httpx.Response(401))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.legitimacy_score == "low"


# ── HLS probes ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hls_extm3u_magic_is_live_high():
    """HLS GET returning #EXTM3U magic → status=live, legitimacy=high."""
    url = "https://example.com/stream.m3u8"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXT-X-VERSION:3\n",
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"


@pytest.mark.asyncio
async def test_hls_404_is_dead():
    """HLS playlist returning 404 → status=dead."""
    url = "https://example.com/gone.m3u8"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    assert results[0].status == "dead"


# ── YouTube / embed rejection ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_youtube_nocookie_embed_is_rejected():
    """YouTube nocookie embed URL → status=dead (not a direct stream)."""
    url = "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&mute=1"
    with respx.mock:  # No HTTP call expected; rejection is immediate
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "dead"
    assert r.fail_reason == "youtube_embed_not_stream"


@pytest.mark.asyncio
async def test_youtube_embed_is_rejected():
    """Regular YouTube embed URL → status=dead (not a direct stream)."""
    url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.fail_reason == "youtube_embed_not_stream"


# ── MP4 / static image rejection ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mp4_url_is_rejected():
    """MP4 URL → status=dead (static video recording, not a live stream)."""
    url = "https://example.com/recording.mp4"
    with respx.mock:  # No HTTP call expected; rejection is immediate
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.fail_reason == "mp4_static_video_not_stream"


@pytest.mark.asyncio
async def test_static_jpeg_is_rejected():
    """Static image URL → status=dead (not a live stream)."""
    url = "https://example.com/snapshot.jpg"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    assert results[0].status == "dead"
    assert results[0].fail_reason == "static_image_not_stream"


# ── HTML page probing ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_html_page_without_stream_is_dead():
    """HTML page with no embedded stream URL → status=dead."""
    url = "https://example.com/cam-page"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><body><h1>Camera Page</h1><p>No stream here</p></body></html>",
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "dead"
    assert r.fail_reason == "no_stream_url_in_html"


@pytest.mark.asyncio
async def test_html_page_with_hls_stream_is_live():
    """HTML page containing an .m3u8 URL → probes the stream → status=live if stream is up."""
    page_url   = "https://example.com/camera-view"
    stream_url = "https://cdn.example.com/live/stream.m3u8"

    with respx.mock:
        # GET the HTML page
        respx.get(page_url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=f'<html><script>hls.loadSource("{stream_url}")</script></html>',
            )
        )
        # GET the HLS playlist — returns valid #EXTM3U magic
        respx.get(stream_url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXT-X-VERSION:3\n",
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([page_url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"
    assert r.stream_url == stream_url   # resolved stream URL stored separately


@pytest.mark.asyncio
async def test_html_page_404_is_dead():
    """HTML page returning 404 → status=dead."""
    url = "https://example.com/missing-cam"
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(404))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    assert results[0].status == "dead"


# ── Auth-protected probes ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_url_pattern_is_dead():
    """URL path matching /login or /auth → dead immediately (no HTTP call)."""
    url = "https://example.com/login/stream.mjpg"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    assert results[0].status == "dead"
    assert results[0].fail_reason == "auth_url_pattern"


# ── Concurrency ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_urls_concurrent():
    """Multiple URLs validated concurrently — correct result per URL."""
    live_url    = "https://example.com/stream1.mjpg"
    dead_url    = "https://example.com/stream2.mjpg"
    timeout_url = "https://example.com/stream3.mjpeg"

    with respx.mock:
        respx.head(live_url).mock(
            return_value=httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace"})
        )
        respx.head(dead_url).mock(return_value=httpx.Response(404))
        respx.head(timeout_url).mock(side_effect=httpx.TimeoutException("timeout"))

        skill = FeedValidationSkill()
        results = await skill.run([live_url, dead_url, timeout_url])

    assert len(results) == 3
    result_map = {r.url: r for r in results}
    assert result_map[live_url].status == "live"
    assert result_map[dead_url].status == "dead"
    assert result_map[timeout_url].status == "unknown"

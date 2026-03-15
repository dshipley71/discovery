#!/usr/bin/env python3
"""
test_feed_validation.py — Unit tests for FeedValidationSkill.
All HTTP mocked via respx. No live network calls.

Policy under test: only HLS (.m3u8) stream URLs are accepted as active
camera links. All other URL types (HTML, MJPEG, MP4, YouTube, etc.) are
rejected immediately (status=dead, fail_reason='not_m3u8_stream').

Playlist classification:
  Master playlist (#EXT-X-STREAM-INF) → playlist_type='master', variant_streams populated.
  Media playlist  (#EXTINF / #EXT-X-TARGETDURATION) → playlist_type='media'.
"""
from __future__ import annotations

import pytest
import respx
import httpx

from webcam_discovery.skills.validation import FeedValidationSkill


# ── HLS probes ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hls_media_playlist_is_live():
    """HLS GET returning #EXTM3U + #EXTINF → status=live, playlist_type=media."""
    url = "https://example.com/stream.m3u8"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=(
                    b"#EXTM3U\n"
                    b"#EXT-X-VERSION:3\n"
                    b"#EXT-X-TARGETDURATION:6\n"
                    b"#EXTINF:6.0,\n"
                    b"segment0000.ts\n"
                ),
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"
    assert r.playlist_type == "media"
    assert r.variant_streams == []


@pytest.mark.asyncio
async def test_hls_master_playlist_extracts_variants():
    """HLS master playlist → playlist_type='master', variant_streams populated."""
    url = "https://cdn.example.com/live/index.m3u8"
    variant1 = "https://cdn.example.com/live/high.m3u8"
    variant2 = "https://cdn.example.com/live/low.m3u8"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=(
                    b"#EXTM3U\n"
                    b"#EXT-X-VERSION:3\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\n"
                    b"high.m3u8\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n"
                    b"low.m3u8\n"
                ),
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "high"
    assert r.playlist_type == "master"
    assert variant1 in r.variant_streams
    assert variant2 in r.variant_streams
    assert len(r.variant_streams) == 2


@pytest.mark.asyncio
async def test_hls_master_playlist_absolute_variant_urls():
    """Master playlist with absolute variant URLs are kept as-is."""
    url = "https://cdn.example.com/stream/master.m3u8"
    abs_variant = "https://other.cdn.com/stream/hd.m3u8"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/x-mpegurl"},
                content=(
                    b"#EXTM3U\n"
                    b"#EXT-X-STREAM-INF:BANDWIDTH=3000000\n"
                    b"https://other.cdn.com/stream/hd.m3u8\n"
                ),
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    r = results[0]
    assert r.playlist_type == "master"
    assert abs_variant in r.variant_streams


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
    assert results[0].fail_reason == "http_404"


@pytest.mark.asyncio
async def test_hls_no_magic_with_correct_content_type():
    """HLS URL returning mpegurl content-type but no #EXTM3U → medium/live."""
    url = "https://example.com/weird.m3u8"
    with respx.mock:
        respx.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"some data without the magic header",
            )
        )
        skill = FeedValidationSkill()
        results = await skill.run([url])

    r = results[0]
    assert r.status == "live"
    assert r.legitimacy_score == "medium"
    assert r.fail_reason == "no_m3u8_magic"


@pytest.mark.asyncio
async def test_hls_timeout_is_unknown():
    """HLS timeout → status=unknown, not a crash."""
    url = "https://example.com/slow.m3u8"
    with respx.mock:
        respx.get(url).mock(side_effect=httpx.TimeoutException("timed out"))
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.url == url
    assert r.status == "unknown"
    assert r.fail_reason == "timeout"


# ── Non-m3u8 URL rejection ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_html_page_is_rejected():
    """HTML page URL → rejected immediately, no HTTP call."""
    url = "https://example.com/cam-page.html"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.fail_reason == "not_m3u8_stream"


@pytest.mark.asyncio
async def test_mjpeg_url_is_rejected():
    """MJPEG URL → rejected immediately, no HTTP call."""
    url = "https://example.com/stream.mjpg"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.fail_reason == "not_m3u8_stream"


@pytest.mark.asyncio
async def test_mp4_url_is_rejected():
    """MP4 URL → rejected immediately, no HTTP call."""
    url = "https://example.com/recording.mp4"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.fail_reason == "not_m3u8_stream"


@pytest.mark.asyncio
async def test_youtube_embed_is_rejected():
    """YouTube embed URL → rejected immediately, no HTTP call."""
    url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert len(results) == 1
    r = results[0]
    assert r.status == "dead"
    assert r.fail_reason == "not_m3u8_stream"


@pytest.mark.asyncio
async def test_static_jpeg_is_rejected():
    """Static image URL → rejected immediately."""
    url = "https://example.com/snapshot.jpg"
    with respx.mock:
        skill = FeedValidationSkill()
        results = await skill.run([url])

    assert results[0].status == "dead"
    assert results[0].fail_reason == "not_m3u8_stream"


# ── Auth-protected probes ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_url_pattern_is_dead():
    """URL path matching /login or /auth → dead immediately (no HTTP call)."""
    url = "https://example.com/login/stream.m3u8"
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
    live_url    = "https://example.com/stream1.m3u8"
    dead_url    = "https://example.com/stream2.m3u8"
    timeout_url = "https://example.com/stream3.m3u8"
    non_hls_url = "https://example.com/page.html"

    with respx.mock:
        respx.get(live_url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/vnd.apple.mpegurl"},
                content=b"#EXTM3U\n#EXTINF:6.0,\nseg.ts\n",
            )
        )
        respx.get(dead_url).mock(return_value=httpx.Response(404))
        respx.get(timeout_url).mock(side_effect=httpx.TimeoutException("timeout"))
        # non_hls_url should be rejected without HTTP call

        skill = FeedValidationSkill()
        results = await skill.run([live_url, dead_url, timeout_url, non_hls_url])

    assert len(results) == 4
    result_map = {r.url: r for r in results}
    assert result_map[live_url].status == "live"
    assert result_map[dead_url].status == "dead"
    assert result_map[timeout_url].status == "unknown"
    assert result_map[non_hls_url].status == "dead"
    assert result_map[non_hls_url].fail_reason == "not_m3u8_stream"

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel

_MEDIA_SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")
_SEGMENT_RE = re.compile(r"^(?!#)([^\n\r]+)$", re.M)


class HLSPlaylistAnalysisResult(BaseModel):
    url: str
    classification: str
    has_endlist: bool = False
    is_vod_type: bool = False
    media_sequence_1: int | None = None
    media_sequence_2: int | None = None
    media_sequence_advanced: bool = False
    segments_changed: bool = False
    resolved_media_url: str | None = None
    error: str | None = None


async def analyze_hls_playlist(url: str, delay_seconds: float = 1.0, timeout: float = 15.0) -> HLSPlaylistAnalysisResult:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            first_url, first = await _fetch_media_playlist(client, url)
            await asyncio.sleep(delay_seconds)
            _, second = await _fetch_media_playlist(client, first_url)
    except Exception as exc:
        return HLSPlaylistAnalysisResult(url=url, classification="playlist_fetch_failed", error=str(exc))

    seq1 = _media_sequence(first)
    seq2 = _media_sequence(second)
    seg1 = _segments(first)
    seg2 = _segments(second)
    has_endlist = "#EXT-X-ENDLIST" in first or "#EXT-X-ENDLIST" in second
    is_vod_type = "#EXT-X-PLAYLIST-TYPE:VOD" in first.upper() or "#EXT-X-PLAYLIST-TYPE:VOD" in second.upper()
    advanced = seq1 is not None and seq2 is not None and seq2 > seq1
    changed = seg1 != seg2

    if has_endlist or is_vod_type:
        cls = "vod_playlist"
    elif advanced or changed:
        cls = "live_playlist"
    elif seq1 is not None and seq2 is not None and seq1 == seq2 and not changed:
        cls = "static_playlist"
    else:
        cls = "unknown_playlist"

    return HLSPlaylistAnalysisResult(
        url=url,
        classification=cls,
        has_endlist=has_endlist,
        is_vod_type=is_vod_type,
        media_sequence_1=seq1,
        media_sequence_2=seq2,
        media_sequence_advanced=advanced,
        segments_changed=changed,
        resolved_media_url=first_url,
    )


async def _fetch_media_playlist(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    r = await client.get(url)
    r.raise_for_status()
    text = r.text
    if "#EXT-X-STREAM-INF" in text:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                media = urljoin(str(r.url), line)
                rm = await client.get(media)
                rm.raise_for_status()
                return media, rm.text
    return str(r.url), text


def _media_sequence(text: str) -> int | None:
    m = _MEDIA_SEQ_RE.search(text)
    return int(m.group(1)) if m else None


def _segments(text: str) -> list[str]:
    return [s.strip() for s in _SEGMENT_RE.findall(text) if s.strip()]


async def inspect_playlist_growth(url: str, delay_seconds: float, timeout: float) -> dict:
    r = await analyze_hls_playlist(url, delay_seconds=delay_seconds, timeout=timeout)
    return {"playlist_media_sequence_delta": (r.media_sequence_2 - r.media_sequence_1) if (r.media_sequence_1 is not None and r.media_sequence_2 is not None) else None, "playlist_segment_growth": r.segments_changed}

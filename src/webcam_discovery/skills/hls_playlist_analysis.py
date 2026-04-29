from __future__ import annotations

import re

import httpx

_MEDIA_SEQ_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")


async def inspect_playlist_growth(url: str, delay_seconds: float, timeout: float) -> dict:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        first = await client.get(url)
        first.raise_for_status()
        text1 = first.text
        seq1 = _media_sequence(text1)

        await _sleep(delay_seconds)

        second = await client.get(url)
        second.raise_for_status()
        text2 = second.text
        seq2 = _media_sequence(text2)

    return {
        "playlist_media_sequence_delta": (seq2 - seq1) if (seq1 is not None and seq2 is not None) else None,
        "playlist_segment_growth": text1 != text2,
    }


def _media_sequence(text: str) -> int | None:
    m = _MEDIA_SEQ_RE.search(text)
    return int(m.group(1)) if m else None


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)

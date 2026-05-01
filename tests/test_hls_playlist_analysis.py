import asyncio
from webcam_discovery.skills import hls_playlist_analysis as mod


def test_vod_endlist(monkeypatch):
    async def fake(client, url):
        return url, "#EXTM3U\n#EXT-X-ENDLIST\nseg.ts"
    monkeypatch.setattr(mod, "_fetch_media_playlist", fake)
    r = asyncio.run(mod.analyze_hls_playlist("https://x"))
    assert r.classification == "vod_playlist"


def test_live_sequence_advances(monkeypatch):
    calls = {"n": 0}
    async def fake(client, url):
        calls["n"] += 1
        return url, f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:{calls['n']}\na.ts"
    monkeypatch.setattr(mod, "_fetch_media_playlist", fake)
    r = asyncio.run(mod.analyze_hls_playlist("https://x"))
    assert r.classification == "live_playlist"

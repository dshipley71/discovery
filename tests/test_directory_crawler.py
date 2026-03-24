import asyncio

import pytest

from webcam_discovery.agents.directory_crawler import (
    FORMAT_BUCKETS,
    DirectoryAgent,
    PER_HOST_EXTRACT_CONCURRENCY,
    SourcesRegistry,
    _classify_camera_format,
    _render_format_breakdown_html,
    _should_skip_feed_extraction,
)
from webcam_discovery.agents.search_agent import _is_blocked
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.traversal import FeedExtractionOutput


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/category/coast", True),
        ("https://example.com/resources/leaflets", True),
        ("https://example.com/brand-resources", True),
        ("https://example.com/live/streams/city-center", True),
        ("https://example.com/live/stream/city-center", False),
        ("https://example.com/webcams", False),
        ("https://example.com/cameras/harbor", True),
        ("https://example.com/camera/harbor", False),
    ],
)
def test_should_skip_feed_extraction(url: str, expected: bool) -> None:
    assert _should_skip_feed_extraction(url) is expected


def test_resolve_feed_urls_caps_per_host_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> tuple[int, list[CameraCandidate]]:
        agent = DirectoryAgent()
        active = 0
        max_active = 0

        async def fake_run(self, input):  # noqa: ANN001
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            slug = input.page_url.rstrip("/").split("/")[-1]
            return FeedExtractionOutput(
                direct_stream_url=f"https://streams.example/{slug}.m3u8"
            )

        monkeypatch.setattr(
            "webcam_discovery.agents.directory_crawler.FeedExtractionSkill.run",
            fake_run,
        )

        candidates = [
            CameraCandidate(
                url=f"https://slow.example/live/stream/{idx}-camera",
                label=f"Camera {idx}",
                source_directory="slow.example",
                source_refs=["https://slow.example/live"],
            )
            for idx in range(6)
        ]
        candidates.extend(
            [
                CameraCandidate(
                    url="https://slow.example/live/streams/city-center",
                    label="Collection page",
                    source_directory="slow.example",
                    source_refs=["https://slow.example/live"],
                ),
                CameraCandidate(
                    url="https://slow.example/resources/leaflets",
                    label="Marketing page",
                    source_directory="slow.example",
                    source_refs=["https://slow.example/live"],
                ),
            ]
        )

        resolved = await agent._resolve_feed_urls(candidates)
        return max_active, resolved

    max_active, resolved = asyncio.run(exercise())

    assert max_active <= PER_HOST_EXTRACT_CONCURRENCY
    assert len(resolved) == 6
    assert all(candidate.url.endswith(".m3u8") for candidate in resolved)
    assert all("/live/streams/" not in ref for c in resolved for ref in c.source_refs)


def test_sources_registry_parses_blocked_domains_from_urls(tmp_path) -> None:
    sources_path = tmp_path / "SOURCES.md"
    sources_path.write_text(
        """# SOURCES

## Section 2 — Blocked Sources

| Source | URL | Reason |
|--------|-----|--------|
| **Insecam** | https://www.insecam.org | Surveillance-oriented |
| **Shodan** | https://www.shodan.io | Device search engine |
| **example.net** | https://example.net/login | Auth gated |
""",
        encoding="utf-8",
    )

    registry = SourcesRegistry(sources_path=sources_path)

    assert registry.blocked_domains == frozenset({"insecam.org", "shodan.io", "example.net"})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.insecam.org/en/", True),
        ("https://search.censys.io/hosts", True),
        ("https://subdomain.zoomeye.org/path", True),
        ("https://worldcams.tv/city", False),
    ],
)
def test_search_agent_uses_sources_registry_blocklist(url: str, expected: bool) -> None:
    assert _is_blocked(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cams.example/live/main.m3u8", ".m3u8"),
        ("rtsp://cams.example/live", "RTSP"),
        ("https://cams.example/channel/stream.mpd", "DASH"),
        ("https://youtube.com/watch?v=abc", "YouTube-only source"),
        ("https://cams.example/mjpeg", "MJPEG"),
        ("https://cams.example/archive.mp4", "MP4-only"),
        ("https://cams.example/snapshot.jpg?refresh=1", "JPEG-refresh"),
        ("https://cams.example/camera/times-square", "Other/HTML/unknown"),
    ],
)
def test_classify_camera_format(url: str, expected: str) -> None:
    assert _classify_camera_format(url) == expected


def test_render_format_breakdown_html_counts_formats() -> None:
    candidates = [
        CameraCandidate(url="https://x.example/a.m3u8", city="Paris", country="France"),
        CameraCandidate(url="https://x.example/b.m3u8", city="Paris", country="France"),
        CameraCandidate(url="rtsp://x.example/cam", city="Paris", country="France"),
        CameraCandidate(url="https://x.example/rome", city="Rome", country="Italy"),
    ]
    formats = [".m3u8", ".m3u8", "RTSP", "MJPEG"]

    html = _render_format_breakdown_html(candidates, formats)

    assert "<table>" in html
    assert "Paris" in html
    assert "France" in html
    assert "Region" not in html
    assert all(bucket in html for bucket in FORMAT_BUCKETS)
    assert "<td class=\"num\">2</td>" in html
    assert "<td class=\"num\">1</td>" in html
    assert "<strong>Total</strong>" in html
    assert html.index("France") < html.index("Italy")

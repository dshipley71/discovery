import asyncio

import pytest

from webcam_discovery.agents.directory_crawler import (
    DirectoryAgent,
    PER_HOST_EXTRACT_CONCURRENCY,
    _should_skip_feed_extraction,
)
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

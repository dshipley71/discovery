from __future__ import annotations

from webcam_discovery.skills.feed_discovery import FeedDiscoverySkill, FeedDiscoveryResult


class FeedDiscoveryAgent:
    def __init__(self) -> None:
        self.skill = FeedDiscoverySkill()

    async def discover(self, page_urls: list[str], max_feed_endpoints: int, max_feed_records: int) -> FeedDiscoveryResult:
        return await self.skill.discover_from_pages(page_urls, max_endpoints=max_feed_endpoints, max_records=max_feed_records)

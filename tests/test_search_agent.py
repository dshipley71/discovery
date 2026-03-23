import asyncio

from webcam_discovery.agents.map_agent import MapAgent
from webcam_discovery.agents.search_agent import (
    BlockedLocationRules,
    DuckDuckGoSearchBlocked,
    SearchAgent,
)
from webcam_discovery.skills.search import QueryGenerationInput, QueryGenerationSkill
from webcam_discovery.skills.traversal import FeedExtractionOutput


def test_query_generation_includes_known_source_domains() -> None:
    result = QueryGenerationSkill().run(
        QueryGenerationInput(
            city="Tokyo",
            language_codes=["en", "ja"],
            known_domains=["worldcams.tv", "earthcam.com"],
        )
    )

    assert 'site:worldcams.tv "Tokyo" webcam' in result.queries
    assert 'site:earthcam.com "Tokyo" webcam' in result.queries
    assert any("ライブカメラ" in query for query in result.queries)


def test_search_agent_extracts_direct_hls_candidates(
    monkeypatch,
) -> None:
    async def exercise() -> list[str]:
        agent = SearchAgent()

        async def fake_search(query):  # noqa: ANN001
            if "Tokyo" not in query:
                return []
            return [
                "https://cams.example/tokyo-tower",
                "https://streams.example/direct/live.m3u8",
                "https://cams.example/tokyo-harbor",
            ]

        async def fake_extract(self, input):  # noqa: ANN001
            if input.page_url.endswith("tokyo-tower"):
                return FeedExtractionOutput(
                    direct_stream_url="https://cdn.example/tokyo-tower/index.m3u8"
                )
            if input.page_url.endswith("tokyo-harbor"):
                return FeedExtractionOutput(
                    embedded_links=["https://cdn.example/tokyo-harbor/master.m3u8"]
                )
            return FeedExtractionOutput()

        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._CITY_TIERS",
            {1: ["Tokyo"]},
        )
        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._duckduckgo_search",
            fake_search,
        )
        monkeypatch.setattr(
            "webcam_discovery.skills.traversal.FeedExtractionSkill.run",
            fake_extract,
        )

        return [candidate.url for candidate in await agent.run(tier=1)]

    urls = asyncio.run(exercise())

    assert set(urls) == {
        "https://cdn.example/tokyo-tower/index.m3u8",
        "https://streams.example/direct/live.m3u8",
        "https://cdn.example/tokyo-harbor/master.m3u8",
    }


def test_search_agent_stops_after_duckduckgo_block(
    monkeypatch,
) -> None:
    async def exercise() -> tuple[list[str], bool]:
        agent = SearchAgent()
        seen_queries: list[str] = []

        async def fake_search(query):  # noqa: ANN001
            seen_queries.append(query)
            raise DuckDuckGoSearchBlocked("anti-bot page")

        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._CITY_TIERS",
            {1: ["Tokyo", "Paris"]},
        )
        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._duckduckgo_search",
            fake_search,
        )

        results = await agent.run(tier=1)
        return seen_queries, results == []

    seen_queries, empty_results = asyncio.run(exercise())

    assert empty_results is True
    assert len(seen_queries) == 1


def test_blocked_location_rules_support_global_and_field_entries() -> None:
    rules = BlockedLocationRules.from_entries(
        [
            "Paris",
            "country:France",
            "source:blocked.example",
            "# comment",
        ]
    )

    assert rules.should_block(city="Paris") is True
    assert rules.should_block(country="France") is True
    assert rules.should_block(source_directory="blocked.example") is True
    assert rules.should_block(city="Tokyo", country="Japan") is False


def test_search_agent_filters_blocked_locations_from_cli_and_file(
    monkeypatch,
    tmp_path,
) -> None:
    blocked_file = tmp_path / "blocked_locations.txt"
    blocked_file.write_text("source:harbor.example\n", encoding="utf-8")

    async def exercise() -> list[str]:
        agent = SearchAgent(
            blocked_locations=["city:Tokyo"],
            blocked_locations_file=blocked_file,
            show_progress=False,
        )

        async def fake_search(query):  # noqa: ANN001
            if "Paris" in query:
                return [
                    "https://harbor.example/live/master.m3u8",
                    "https://allowed.example/live/master.m3u8",
                ]
            raise AssertionError(f"Unexpected query {query}")

        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._CITY_TIERS",
            {1: ["Tokyo", "Paris"]},
        )
        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._duckduckgo_search",
            fake_search,
        )

        return [candidate.url for candidate in await agent.run(tier=1)]

    urls = asyncio.run(exercise())

    assert urls == ["https://allowed.example/live/master.m3u8"]


def test_search_agent_reports_successful_hls_streams(
    monkeypatch,
) -> None:
    reported: list[str] = []

    async def exercise() -> list[str]:
        agent = SearchAgent(
            stream_reporter=reported.append,
            show_progress=False,
        )

        async def fake_search(query):  # noqa: ANN001
            if "Tokyo" not in query:
                return []
            return ["https://allowed.example/live/master.m3u8"]

        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._CITY_TIERS",
            {1: ["Tokyo"]},
        )
        monkeypatch.setattr(
            "webcam_discovery.agents.search_agent._duckduckgo_search",
            fake_search,
        )

        return [candidate.url for candidate in await agent.run(tier=1)]

    urls = asyncio.run(exercise())

    assert urls == ["https://allowed.example/live/master.m3u8"]
    assert reported == ["https://allowed.example/live/master.m3u8"]


def test_map_agent_copies_template_to_output(tmp_path) -> None:
    output_dir = tmp_path / "out"
    map_path = MapAgent(output_dir=output_dir).run()

    assert map_path == output_dir / "map.html"
    assert map_path.exists()
    assert "<!DOCTYPE html>" in map_path.read_text(encoding="utf-8")

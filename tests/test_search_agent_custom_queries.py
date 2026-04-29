import asyncio

from webcam_discovery.agents.search_agent import SearchAgent


def test_search_agent_uses_custom_queries(monkeypatch) -> None:
    seen_queries: list[str] = []

    async def fake_search(query):
        seen_queries.append(query)
        return [{"url": "https://allowed.example/live.m3u8", "title": "", "snippet": ""}]

    monkeypatch.setattr("webcam_discovery.agents.search_agent._duckduckgo_search", fake_search)

    async def run() -> list[str]:
        agent = SearchAgent(show_progress=False)
        candidates = [
            c.url
            async for c in agent.stream_queries(
                custom_queries=["PennDOT traffic cameras live"],
                raw_query="Get me traffic cams in Pennsylvania",
            )
        ]
        return candidates

    urls = asyncio.run(run())
    assert seen_queries == ["PennDOT traffic cameras live"]
    assert urls == ["https://allowed.example/live.m3u8"]


def test_search_agent_raw_query_fallback_has_no_unrelated_defaults(monkeypatch) -> None:
    seen_queries: list[str] = []

    async def fake_search(query):
        seen_queries.append(query)
        return []

    monkeypatch.setattr("webcam_discovery.agents.search_agent._duckduckgo_search", fake_search)

    async def run() -> None:
        agent = SearchAgent(show_progress=False)
        _ = [
            c
            async for c in agent.stream_queries(
                custom_queries=[],
                raw_query="Find public traffic cameras near Lancaster County Pennsylvania",
            )
        ]

    asyncio.run(run())
    joined = "\n".join(seen_queries)
    assert "Lancaster County Pennsylvania" in joined
    for forbidden in ["New York City", "London", "Tokyo", "Paris", "Sydney", "Dubai"]:
        assert forbidden not in joined

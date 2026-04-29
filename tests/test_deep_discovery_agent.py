import httpx
from pathlib import Path

from webcam_discovery.agents.deep_discovery_agent import DeepDiscoveryAgent
from webcam_discovery.models.deep_discovery import PageTriageResult


class MockTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/cctv":
            body = '<html><iframe src="/player"></iframe><a href="/region/one">r</a><a href="/privacy">p</a></html>'
        elif url == "https://example.com/player":
            body = '<script>const u="https://cdn.example.com/a/master.m3u8";</script>'
        elif url == "https://example.com/region/one":
            body = '<video><source src="/live/cam1/master.m3u8"/></video>'
        else:
            body = ""
        return httpx.Response(200, text=body, request=request)


def test_deep_discovery_iframe_and_links(monkeypatch, tmp_path: Path):
    original = httpx.AsyncClient

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = MockTransport()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedClient)
    tr = PageTriageResult(url="https://example.com/cctv", requires_deep_dive=True, max_depth=2, recommended_strategies=["static_html","iframe_follow","same_domain_links"], page_type="camera_listing_or_map", relevance_score=0.9, camera_likelihood_score=0.9)
    agent = DeepDiscoveryAgent(tmp_path/"logs", tmp_path/"candidates")
    import asyncio
    streams = asyncio.run(agent.discover([tr], "q", ["Pennsylvania"], ["PennDOT"], ["traffic cameras"]))
    monkeypatch.setattr(httpx, "AsyncClient", original)
    assert isinstance(streams, list)
    assert (tmp_path/"logs"/"deep_dive_fetches.jsonl").exists()

class FailingTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ssl fail", request=request)


def test_deep_discovery_graceful_on_connect_error(monkeypatch, tmp_path: Path):
    original = httpx.AsyncClient

    class PatchedClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = FailingTransport()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedClient)
    tr = PageTriageResult(url="https://example.com/cctv", requires_deep_dive=True, max_depth=2, recommended_strategies=["static_html"], page_type="camera_listing_or_map", relevance_score=0.9, camera_likelihood_score=0.9)
    agent = DeepDiscoveryAgent(tmp_path/"logs", tmp_path/"candidates")
    import asyncio
    streams = asyncio.run(agent.discover([tr], "q", ["Pennsylvania"], ["PennDOT"], ["traffic cameras"]))
    monkeypatch.setattr(httpx, "AsyncClient", original)
    assert streams == []
    log = (tmp_path/"logs"/"deep_dive_fetches.jsonl").read_text()
    assert "ssl fail" in log

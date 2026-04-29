from __future__ import annotations

from pydantic import BaseModel, Field


class PageCandidate(BaseModel):
    run_id: str | None = None
    user_query: str | None = None
    source_query: str | None = None
    url: str
    candidate_type: str = "search_result_page"
    title: str | None = None
    snippet: str | None = None
    discovered_by: str = "SearchAgent"
    target_locations: list[str] = Field(default_factory=list)
    camera_types: list[str] = Field(default_factory=list)


class StreamCandidate(BaseModel):
    run_id: str | None = None
    user_query: str | None = None
    source_query: str | None = None
    search_result_url: str | None = None
    root_url: str | None = None
    source_page: str | None = None
    parent_pages: list[str] = Field(default_factory=list)
    depth: int = 0
    discovery_strategy: str | None = None
    candidate_url: str
    candidate_type: str = "direct_hls_stream"
    target_locations: list[str] = Field(default_factory=list)
    camera_types: list[str] = Field(default_factory=list)
    page_type: str = "unknown"
    page_relevance_score: float = 0.0
    camera_likelihood_score: float = 0.0


class SearchDiscoveryResult(BaseModel):
    run_id: str | None = None
    page_candidates: list[PageCandidate] = Field(default_factory=list)
    stream_candidates: list[StreamCandidate] = Field(default_factory=list)
    search_results_count: int = 0
    direct_stream_count: int = 0


class PageTriageResult(BaseModel):
    run_id: str | None = None
    url: str
    source_query: str | None = None
    title: str | None = None
    snippet: str | None = None
    page_type: str = "unknown"
    relevance_score: float = 0.0
    camera_likelihood_score: float = 0.0
    requires_deep_dive: bool = False
    likely_requires_js: bool = False
    recommended_strategies: list[str] = Field(default_factory=list)
    max_depth: int = 0
    reason: str | None = None


class DeepDivePlan(BaseModel):
    run_id: str | None = None
    root_url: str
    source_query: str | None = None
    page_type: str = "unknown"
    relevance_score: float = 0.0
    camera_likelihood_score: float = 0.0
    target_locations: list[str] = Field(default_factory=list)
    camera_types: list[str] = Field(default_factory=list)
    strategies: list[str] = Field(default_factory=list)
    max_depth: int = 1
    max_links_per_page: int = 25
    max_pages_per_domain: int = 50
    max_js_assets: int = 20
    use_network_capture: bool = False
    network_capture_timeout_seconds: int = 8
    same_domain_only: bool = True
    stop_when_streams_found: bool = True
    reason: str | None = None


class CandidateRelevanceDecision(BaseModel):
    candidate_url: str
    accepted: bool
    relevance_score: float
    reason: str
    source_page: str | None = None
    source_query: str | None = None
    discovery_strategy: str | None = None

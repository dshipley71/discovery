from webcam_discovery.agents.search_result_triage_agent import SearchResultTriageAgent
from webcam_discovery.models.deep_discovery import PageCandidate


def test_triage_camera_listing_page():
    page = PageCandidate(url="https://www.511pa.com/cctv", title="511PA traffic cameras", snippet="live CCTV traffic cameras")
    r = SearchResultTriageAgent().triage([page], ["Pennsylvania"], ["PennDOT"], ["traffic cameras"])[0]
    assert r.requires_deep_dive is True
    assert r.page_type in {"camera_listing_or_map", "camera_region_page"}
    assert r.camera_likelihood_score >= 0.5
    assert "static_html" in r.recommended_strategies
    assert "same_domain_links" in r.recommended_strategies


def test_triage_low_relevance_page():
    page = PageCandidate(url="https://example.com/privacy", title="Privacy policy", snippet="terms and privacy")
    r = SearchResultTriageAgent().triage([page], ["Pennsylvania"], ["PennDOT"], ["traffic cameras"])[0]
    assert r.requires_deep_dive is False
    assert r.max_depth == 0
    assert r.camera_likelihood_score < 0.5

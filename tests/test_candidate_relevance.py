from webcam_discovery.models.deep_discovery import StreamCandidate
from webcam_discovery.skills.candidate_relevance import CandidateRelevanceFilter


def test_relevance_keeps_cdn_with_lineage():
    c = StreamCandidate(candidate_url="https://d123.cloudfront.net/live/master.m3u8", source_page="https://www.511pa.com/cctv", page_relevance_score=0.9)
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT"], ["traffic cameras"])[0][1]
    assert d.accepted is True


def test_relevance_rejects_test_domains_without_lineage():
    candidates = [StreamCandidate(candidate_url="https://test-streams.mux.dev/x.m3u8"), StreamCandidate(candidate_url="https://gist.github.com/a.m3u8")]
    decisions = CandidateRelevanceFilter().filter(candidates, ["Pennsylvania"], ["PennDOT"], ["traffic cameras"])
    assert all(not d.accepted for _, d in decisions)

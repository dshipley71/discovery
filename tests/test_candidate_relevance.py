from webcam_discovery.models.deep_discovery import StreamCandidate
from webcam_discovery.skills.candidate_relevance import CandidateRelevanceFilter


def test_rejects_maryland_stream_for_pennsylvania_query_only():
    c = StreamCandidate(candidate_url="https://chart.maryland.gov/sha/live.m3u8", source_query="Pennsylvania traffic cameras live")
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT", "511PA"], ["traffic cameras"])[0][1]
    assert d.accepted is False
    assert "query-only" in d.reason or "off_target" in d.reason


def test_rejects_michigan_page_for_pennsylvania():
    c = StreamCandidate(candidate_url="https://mdot.cam/live/stream.m3u8", source_page="https://www.michigan.gov/traffic/cameras", page_relevance_score=0.9)
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT", "511PA"], ["traffic cameras"])[0][1]
    assert d.accepted is False


def test_rejects_cdn_without_target_lineage():
    c = StreamCandidate(candidate_url="https://d123.cloudfront.net/live/master.m3u8")
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT"], ["traffic cameras"])[0][1]
    assert d.accepted is False


def test_accepts_target_relevant_source():
    c = StreamCandidate(candidate_url="https://d123.cloudfront.net/live/master.m3u8", source_page="https://www.511pa.com/cameras", page_relevance_score=0.9, camera_likelihood_score=0.9)
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT", "511PA"], ["traffic cameras"])[0][1]
    assert d.accepted is True

from webcam_discovery.models.deep_discovery import StreamCandidate
from webcam_discovery.skills.candidate_relevance import CandidateRelevanceFilter


def test_rejects_bad_patterns_and_query_only():
    c = StreamCandidate(candidate_url="https://video2archives.earthcam.com/archives/backup.mp4/playlist.m3u8", source_query="Pennsylvania cams")
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT"], ["traffic"])[0][1]
    assert d.accepted is False


def test_rejects_placeholder_ip_and_gist():
    candidates = [StreamCandidate(candidate_url="http://IP.AA.DD.RR:8080/x.m3u8", source_page="https://foo"), StreamCandidate(candidate_url="https://gist.github.com/x/iptv.m3u8", source_page="https://foo")]
    decisions = CandidateRelevanceFilter().filter(candidates, ["Pennsylvania"], ["PennDOT"], ["traffic"])
    assert all(not d.accepted for _, d in decisions)


def test_accepts_cdn_with_strong_lineage():
    c = StreamCandidate(candidate_url="https://d123.cloudfront.net/live/master.m3u8", source_page="https://511pa.com/cameras", page_relevance_score=0.9, camera_likelihood_score=0.9)
    d = CandidateRelevanceFilter().filter([c], ["Pennsylvania"], ["PennDOT", "511pa"], ["traffic"])[0][1]
    assert d.accepted is True

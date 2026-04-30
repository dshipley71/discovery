from __future__ import annotations

from urllib.parse import urlparse

from webcam_discovery.models.deep_discovery import CandidateRelevanceDecision, StreamCandidate

BLOCKED_TEST_DOMAINS = {"test-streams.mux.dev", "bitdash-a.akamaihd.net", "demo.unified-streaming.com", "gist.github.com", "github.com", "m3u8-player.com"}
BLOCKED_PATTERNS = ["vod", "archive", "archives", "backup.mp4", "newyears", "bigbuckbunny", "sample", "demo", "test-stream", "iptv", "ip.aa.dd.rr"]
OFF_TARGET_TOKENS = {"maryland", "michigan", "newyork", "new-york", "virginia", "ohio", "florida", "texas", "california"}


class CandidateRelevanceFilter:
    def filter(self, candidates: list[StreamCandidate], target_locations: list[str], agencies: list[str], camera_types: list[str]) -> list[tuple[StreamCandidate, CandidateRelevanceDecision]]:
        decisions = []
        target_terms = [x.lower() for x in (target_locations + agencies) if x]
        camera_terms = [x.lower() for x in (camera_types + ["camera", "webcam", "traffic", "hls", "m3u8"]) if x]

        for c in candidates:
            domain = (urlparse(c.candidate_url).netloc or "").lower()
            source_page = c.source_page if (c.source_page and c.source_page.startswith(("http://", "https://"))) else None
            source_blob = " ".join(filter(None, [source_page, c.root_url])).lower()
            candidate_blob = " ".join(filter(None, [c.candidate_url, source_page, c.root_url])).lower()
            blocked_pattern = next((p for p in BLOCKED_PATTERNS if p in candidate_blob), None)
            target_hits = [t for t in target_terms if t in source_blob or t in candidate_blob]
            camera_hits = [t for t in camera_terms if t in candidate_blob]
            source_query_only = bool(c.source_query and any(t in c.source_query.lower() for t in target_terms) and not target_hits)
            off_target_hits = [t for t in OFF_TARGET_TOKENS if t in candidate_blob and all(t not in tt for tt in target_terms)]
            strong_lineage = bool(source_page) and (c.page_relevance_score >= 0.6 or c.camera_likelihood_score >= 0.6)

            accepted = False
            reason = "rejected: insufficient target evidence"
            score = 0.1
            if domain in BLOCKED_TEST_DOMAINS:
                reason = "rejected: generic demo/test domain"
            elif blocked_pattern:
                reason = f"rejected: blocked pattern '{blocked_pattern}'"
            elif source_query_only:
                reason = "rejected: query-only target evidence"
            elif off_target_hits and not target_hits:
                reason = "rejected: off_target_region_or_agency"
            elif not target_hits:
                reason = "rejected: no source-page target evidence"
            elif not camera_hits:
                reason = "rejected: missing camera/hls evidence"
            elif domain.endswith(("cloudfront.net", "akamaihd.net", "fastly.net")) and not strong_lineage:
                reason = "rejected: untrusted CDN without strong lineage"
            else:
                accepted = True
                score = 0.9
                reason = "accepted: source lineage + target evidence"

            decisions.append((c, CandidateRelevanceDecision(candidate_url=c.candidate_url, accepted=accepted, relevance_score=score, reason=reason, source_page=source_page, source_query=c.source_query, discovery_strategy=c.discovery_strategy)))
        return decisions

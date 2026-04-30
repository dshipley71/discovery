from __future__ import annotations

import re
from urllib.parse import urlparse

from webcam_discovery.models.deep_discovery import CandidateRelevanceDecision, StreamCandidate

BLOCKED_TEST_DOMAINS = {"test-streams.mux.dev", "bitdash-a.akamaihd.net", "demo.unified-streaming.com", "gist.github.com", "github.com", "m3u8-player.com"}
BLOCKED_PATTERNS = ["vod", "archive", "archives", "backup.mp4", "newyears", "bigbuckbunny", "sample", "demo", "test-stream", "iptv", "ip.aa.dd.rr"]


class CandidateRelevanceFilter:
    def filter(self, candidates: list[StreamCandidate], target_locations: list[str], agencies: list[str], camera_types: list[str]) -> list[tuple[StreamCandidate, CandidateRelevanceDecision]]:
        decisions = []
        terms = [x.lower() for x in target_locations + agencies + camera_types if x]
        for c in candidates:
            domain = (urlparse(c.candidate_url).netloc or "").lower()
            source_blob = " ".join(filter(None, [c.source_page, c.root_url])).lower()
            candidate_blob = " ".join(filter(None, [c.candidate_url, c.source_page, c.root_url])).lower()
            blocked_pattern = next((p for p in BLOCKED_PATTERNS if p in candidate_blob), None)
            term_in_source = any(t in source_blob for t in terms)
            source_query_only = bool(c.source_query and any(t in c.source_query.lower() for t in terms) and not term_in_source)
            strong_lineage = c.page_relevance_score >= 0.7 or c.camera_likelihood_score >= 0.7 or (c.source_page is not None and term_in_source)

            accepted = False
            reason = "rejected: insufficient target evidence"
            score = 0.1
            if domain in BLOCKED_TEST_DOMAINS:
                reason = "rejected: generic demo/test domain"
            elif blocked_pattern:
                reason = f"rejected: blocked pattern '{blocked_pattern}'"
            elif source_query_only:
                reason = "rejected: query-only target evidence"
            elif not term_in_source and not strong_lineage:
                reason = "rejected: no source-page target evidence"
            elif domain.endswith(("cloudfront.net", "akamaihd.net", "fastly.net")) and not strong_lineage:
                reason = "rejected: untrusted CDN without strong lineage"
            else:
                accepted = True
                score = 0.9
                reason = "accepted: source lineage + target evidence"
            decisions.append((c, CandidateRelevanceDecision(candidate_url=c.candidate_url, accepted=accepted, relevance_score=score, reason=reason, source_page=c.source_page, source_query=c.source_query, discovery_strategy=c.discovery_strategy)))
        return decisions

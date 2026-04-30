from __future__ import annotations

from urllib.parse import urlparse

from webcam_discovery.models.deep_discovery import CandidateRelevanceDecision, StreamCandidate

BLOCKED_TEST_DOMAINS = {"test-streams.mux.dev", "bitdash-a.akamaihd.net", "demo.unified-streaming.com", "gist.github.com", "github.com", "m3u8-player.com"}


class CandidateRelevanceFilter:
    def filter(self, candidates: list[StreamCandidate], target_locations: list[str], agencies: list[str], camera_types: list[str]) -> list[tuple[StreamCandidate, CandidateRelevanceDecision]]:
        decisions = []
        terms = [x.lower() for x in target_locations + agencies + camera_types if x]
        for c in candidates:
            domain = (urlparse(c.candidate_url).netloc or "").lower()
            blob = " ".join(filter(None, [c.candidate_url, c.source_page, c.root_url, c.user_query])).lower()
            source_blob = " ".join(filter(None, [c.source_page, c.root_url])).lower()
            strong_lineage = c.page_relevance_score >= 0.65 or c.camera_likelihood_score >= 0.65 or bool(c.source_page)
            term_in_source = any(t in source_blob for t in terms)
            term_in_query_only = (c.source_query and any(t in c.source_query.lower() for t in terms)) and not term_in_source
            is_test = domain in BLOCKED_TEST_DOMAINS
            accepted = False
            reason = "rejected: insufficient target evidence"
            score = 0.1
            if is_test and not strong_lineage:
                reason = "rejected: generic/demo test stream"
            elif term_in_query_only and not strong_lineage:
                reason = "rejected: source_query-only match"
            elif strong_lineage and (term_in_source or any(t in blob for t in terms) or domain.endswith("cloudfront.net") or domain.endswith("akamaihd.net")):
                accepted = True
                score = 0.85
                reason = "accepted: strong lineage and target evidence"
            decisions.append((c, CandidateRelevanceDecision(candidate_url=c.candidate_url, accepted=accepted, relevance_score=score, reason=reason, source_page=c.source_page, source_query=c.source_query, discovery_strategy=c.discovery_strategy)))
        return decisions

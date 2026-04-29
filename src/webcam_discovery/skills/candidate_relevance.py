from __future__ import annotations

from urllib.parse import urlparse

from webcam_discovery.models.deep_discovery import CandidateRelevanceDecision, StreamCandidate

BLOCKED_TEST_DOMAINS = {"test-streams.mux.dev", "bitdash-a.akamaihd.net", "demo.unified-streaming.com", "gist.github.com", "github.com"}


class CandidateRelevanceFilter:
    def filter(self, candidates: list[StreamCandidate], target_locations: list[str], agencies: list[str], camera_types: list[str]) -> list[tuple[StreamCandidate, CandidateRelevanceDecision]]:
        decisions = []
        terms = [x.lower() for x in target_locations + agencies + camera_types]
        for c in candidates:
            domain = (urlparse(c.candidate_url).netloc or "").lower()
            blob = " ".join(filter(None, [c.candidate_url, c.source_page, c.source_query, c.root_url])).lower()
            lineage = c.page_relevance_score >= 0.5 or c.camera_likelihood_score >= 0.5 or bool(c.source_page)
            term_hit = any(t and t in blob for t in terms)
            is_test = domain in BLOCKED_TEST_DOMAINS
            accepted = (lineage or term_hit) and not (is_test and not lineage)
            score = 0.9 if accepted else 0.1
            reason = "accepted: lineage/target match" if accepted else "rejected: generic/test or no lineage"
            decisions.append((c, CandidateRelevanceDecision(candidate_url=c.candidate_url, accepted=accepted, relevance_score=score, reason=reason, source_page=c.source_page, source_query=c.source_query, discovery_strategy=c.discovery_strategy)))
        return decisions

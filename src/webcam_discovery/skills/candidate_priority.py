from __future__ import annotations

from urllib.parse import urlparse

from webcam_discovery.models.deep_discovery import CandidatePriorityDecision, StreamCandidate

BLOCKED_TEST_DOMAINS = {"test-streams.mux.dev", "bitdash-a.akamaihd.net", "demo.unified-streaming.com", "gist.github.com", "github.com", "m3u8-player.com"}


class CandidatePriorityScorer:
    def score(self, candidates: list[StreamCandidate], target_locations: list[str], agencies: list[str], camera_types: list[str]) -> list[tuple[StreamCandidate, CandidatePriorityDecision]]:
        decisions = []
        target_terms = [x.lower() for x in (target_locations + agencies) if x]
        camera_terms = [x.lower() for x in (camera_types + ["camera", "webcam", "traffic", "hls", "m3u8"]) if x]

        for c in candidates:
            blob = " ".join(filter(None, [c.candidate_url, c.source_page, c.root_url, c.source_query])).lower()
            domain = (urlparse(c.candidate_url).netloc or "").lower()
            target_hits = [t for t in target_terms if t in blob]
            camera_hits = [t for t in camera_terms if t in blob]
            is_malformed = not c.candidate_url.startswith(("http://", "https://"))
            non_hls = ".m3u8" not in c.candidate_url.lower()
            blocked = domain in BLOCKED_TEST_DOMAINS

            if is_malformed:
                decisions.append((c, CandidatePriorityDecision(candidate_url=c.candidate_url, priority="low", priority_score=0.0, priority_reason="malformed_url", sent_to_validation=False, evidence={"url_evidence": [c.candidate_url], "feed_metadata_evidence": [], "source_page_evidence": [], "query_evidence": [], "visual_evidence": []})))
                continue
            if blocked:
                decisions.append((c, CandidatePriorityDecision(candidate_url=c.candidate_url, priority="low", priority_score=0.0, priority_reason="policy_blocked_test_domain", sent_to_validation=False, evidence={"url_evidence": [domain], "feed_metadata_evidence": [], "source_page_evidence": [], "query_evidence": [], "visual_evidence": []})))
                continue

            score = 0.2
            score += min(0.5, 0.2 * len(target_hits))
            score += min(0.2, 0.1 * len(camera_hits))
            if not non_hls:
                score += 0.2
            score += min(0.1, c.page_relevance_score * 0.1)

            priority = "high" if score >= 0.7 else "medium" if score >= 0.45 else "low"
            reason = "prioritized_for_validation"
            evidence = {
                "url_evidence": [c.candidate_url],
                "feed_metadata_evidence": [],
                "source_page_evidence": [c.source_page] if c.source_page else [],
                "query_evidence": [c.source_query] if c.source_query else [],
                "visual_evidence": [],
            }
            decisions.append((c, CandidatePriorityDecision(candidate_url=c.candidate_url, priority=priority, priority_score=round(score, 4), priority_reason=reason, sent_to_validation=True, evidence=evidence)))

        return decisions

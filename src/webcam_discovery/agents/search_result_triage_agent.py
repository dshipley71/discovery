from __future__ import annotations

from webcam_discovery.models.deep_discovery import PageCandidate, PageTriageResult

POSITIVE = ["camera", "cctv", "traffic", "webcam", "stream", "live", "map", "region", "511", "dot", "turnpike"]
NEGATIVE = ["privacy", "terms", "careers", "login", "signup", "press", "blog", "news", "pdf", "job"]


class SearchResultTriageAgent:
    def triage(self, pages: list[PageCandidate], target_locations: list[str], agencies: list[str], camera_types: list[str]) -> list[PageTriageResult]:
        out: list[PageTriageResult] = []
        location_terms = [x.lower() for x in (target_locations + agencies + camera_types)]
        for p in pages:
            blob = " ".join(filter(None, [p.url, p.title, p.snippet, p.source_query])).lower()
            pos = sum(1 for s in POSITIVE if s in blob)
            neg = sum(1 for s in NEGATIVE if s in blob)
            loc = sum(1 for s in location_terms if s and s in blob)
            camera_score = min(1.0, 0.15 * pos - 0.2 * neg + 0.15 * (loc > 0))
            relevance = min(1.0, 0.2 * pos - 0.2 * neg + 0.2 * min(loc, 2))
            page_type = "generic_search_result"
            if "m3u8" in blob:
                page_type = "direct_hls_stream"
            elif "region" in blob:
                page_type = "camera_region_page"
            elif any(t in blob for t in ["cctv", "cameralisting", "camera listing", "/cctv", "webmap"]):
                page_type = "camera_listing_or_map"
            elif any(t in blob for t in ["player", "viewer"]):
                page_type = "player_page"
            requires = relevance >= 0.4 and camera_score >= 0.35 and page_type != "direct_hls_stream"
            likely_js = page_type in {"camera_listing_or_map", "camera_region_page", "player_page"}
            strategies = ["static_html"] if requires else []
            if requires:
                strategies += ["iframe_follow", "same_domain_links"]
            if likely_js:
                strategies += ["javascript_asset_scan", "network_capture"]
            max_depth = 3 if page_type == "camera_listing_or_map" else (2 if requires else 0)
            out.append(PageTriageResult(
                run_id=p.run_id, url=p.url, source_query=p.source_query, title=p.title, snippet=p.snippet,
                page_type=page_type, relevance_score=relevance, camera_likelihood_score=camera_score,
                requires_deep_dive=requires, likely_requires_js=likely_js, recommended_strategies=strategies,
                max_depth=max_depth, reason=f"signals pos={pos} neg={neg} loc={loc}"
            ))
        return out

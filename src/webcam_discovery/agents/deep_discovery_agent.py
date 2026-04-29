from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from webcam_discovery.models.deep_discovery import DeepDivePlan, PageTriageResult, StreamCandidate
from webcam_discovery.skills.javascript_asset_scan import scan_javascript_asset

HLS_RE = re.compile(r"https?://[^'\"\\s]+\\.m3u8[^'\"\\s]*|/[^'\"\\s]+\\.m3u8[^'\"\\s]*", re.IGNORECASE)
POS_LINK = ["camera", "cctv", "traffic", "region", "map", "stream", "viewer", "player", "cameralisting"]
NEG_LINK = ["privacy", "terms", "careers", "login", "signup"]


class DeepDiscoveryAgent:
    def __init__(self, log_dir: Path, candidates_dir: Path, max_links_per_page: int = 25, max_js_assets_per_page: int = 20):
        self.log_dir = log_dir
        self.candidates_dir = candidates_dir
        self.max_links_per_page = max_links_per_page
        self.max_js_assets_per_page = max_js_assets_per_page

    def _append(self, path: Path, row: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.open("a", encoding="utf-8").write(json.dumps(row) + "\n")

    async def discover(self, triage_results: list[PageTriageResult], user_query: str, target_locations: list[str], agencies: list[str], camera_types: list[str], max_deep_depth: int = 3, max_deep_pages: int = 100, max_network_capture_pages: int = 10, network_capture_timeout: int = 8) -> list[StreamCandidate]:
        streams: list[StreamCandidate] = []
        async with httpx.AsyncClient(timeout=15) as client:
            for tr in triage_results:
                self._append(self.log_dir / "page_triage.jsonl", tr.model_dump())
                if not tr.requires_deep_dive:
                    continue
                plan = DeepDivePlan(root_url=tr.url, source_query=tr.source_query, page_type=tr.page_type, relevance_score=tr.relevance_score, camera_likelihood_score=tr.camera_likelihood_score, target_locations=target_locations, camera_types=camera_types, strategies=tr.recommended_strategies, max_depth=min(max_deep_depth, tr.max_depth), max_links_per_page=self.max_links_per_page, max_pages_per_domain=max_deep_pages, max_js_assets=self.max_js_assets_per_page, use_network_capture=("network_capture" in tr.recommended_strategies), network_capture_timeout_seconds=network_capture_timeout, reason=tr.reason)
                self._append(self.log_dir / "deep_dive_plan.jsonl", plan.model_dump())
                found = await self._crawl(client, plan, tr, user_query)
                streams.extend(found)
        return streams

    async def _crawl(self, client, plan, tr, user_query):
        q = [(plan.root_url, 0, [plan.root_url])]
        visited = set()
        root_domain = urlparse(plan.root_url).netloc
        out = []
        while q:
            url, depth, parents = q.pop(0)
            if url in visited or depth > plan.max_depth:
                continue
            visited.add(url)
            r = await client.get(url)
            text = r.text
            self._append(self.log_dir / "deep_dive_fetches.jsonl", {"root_url": plan.root_url, "url": url, "depth": depth, "status_code": r.status_code, "content_type": r.headers.get("content-type", ""), "strategy": "same_domain_links"})
            for m in HLS_RE.findall(text):
                sc = StreamCandidate(user_query=user_query, source_query=plan.source_query, search_result_url=plan.root_url, root_url=plan.root_url, source_page=url, parent_pages=parents, depth=depth, discovery_strategy="static_html", candidate_url=urljoin(url, m), target_locations=plan.target_locations, camera_types=plan.camera_types, page_type=plan.page_type, page_relevance_score=plan.relevance_score, camera_likelihood_score=plan.camera_likelihood_score)
                out.append(sc)
                self._append(self.candidates_dir / "extracted_stream_candidates.jsonl", sc.model_dump())
            soup = BeautifulSoup(text, "html.parser")
            for iframe in soup.select("iframe[src]"):
                iframe_url = urljoin(url, iframe["src"])
                if urlparse(iframe_url).netloc == root_domain:
                    q.append((iframe_url, depth + 1, parents + [iframe_url]))
            for script in soup.select("script[src]")[: plan.max_js_assets]:
                js_url = urljoin(url, script["src"])
                js_resp = await client.get(js_url)
                matches = scan_javascript_asset(js_resp.text, url, js_url)
                self._append(self.log_dir / "js_asset_scan.jsonl", {"page_url": url, "js_url": js_url, "status_code": js_resp.status_code, "matches": matches})
                for m in matches:
                    if m["kind"] == "hls_url":
                        sc = StreamCandidate(user_query=user_query, source_query=plan.source_query, search_result_url=plan.root_url, root_url=plan.root_url, source_page=url, parent_pages=parents, depth=depth, discovery_strategy="javascript_asset_scan", candidate_url=m["value"], target_locations=plan.target_locations, camera_types=plan.camera_types, page_type=plan.page_type, page_relevance_score=plan.relevance_score, camera_likelihood_score=plan.camera_likelihood_score)
                        out.append(sc)
            for a in soup.select("a[href]")[: plan.max_links_per_page]:
                href = urljoin(url, a["href"])
                if urlparse(href).scheme not in {"http", "https"}:
                    continue
                if urlparse(href).netloc != root_domain:
                    continue
                low = href.lower()
                if any(x in low for x in NEG_LINK):
                    continue
                if any(x in low for x in POS_LINK):
                    q.append((href, depth + 1, parents + [href]))
        self._append(self.log_dir / "network_capture.jsonl", {"root_url": plan.root_url, "status": "skipped_or_not_enabled"})
        return out

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.endpoint_patterns import discover_endpoint_urls
from webcam_discovery.skills.feed_parsers import extract_camera_records


@dataclass
class FeedDiscoveryResult:
    endpoints_discovered: int = 0
    endpoints_parsed: int = 0
    records_extracted: int = 0
    candidates: list[CameraCandidate] | None = None


class FeedDiscoverySkill:
    async def discover_from_pages(self, page_urls: list[str], max_endpoints: int = 100, max_records: int = 3000) -> FeedDiscoveryResult:
        result = FeedDiscoveryResult(candidates=[])
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            endpoint_urls: list[str] = []
            for u in page_urls:
                try:
                    r = await client.get(u)
                except Exception:
                    continue
                endpoint_urls.extend(discover_endpoint_urls(r.text))
            endpoint_urls = list(dict.fromkeys(endpoint_urls))[:max_endpoints]
            result.endpoints_discovered = len(endpoint_urls)
            for e in endpoint_urls:
                try:
                    r = await client.get(e)
                    payload = r.json()
                except Exception:
                    continue
                result.endpoints_parsed += 1
                for rec in extract_camera_records(payload, base_url=e):
                    cand = CameraCandidate(url=rec["stream_url"], label=rec.get("label"), city=rec.get("city"), country=rec.get("country"), notes=json.dumps({"latitude": rec.get("latitude"), "longitude": rec.get("longitude"), "viewer_url": rec.get("viewer_url")}), source_refs=[e])
                    result.candidates.append(cand)
                    result.records_extracted += 1
                    if result.records_extracted >= max_records:
                        break
                if result.records_extracted >= max_records:
                    break
        return result

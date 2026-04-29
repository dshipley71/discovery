from __future__ import annotations

import re
from urllib.parse import urljoin

HLS_RE = re.compile(r"https?://[^'\"\\s]+\\.m3u8[^'\"\\s]*|/[^'\"\\s]+\\.m3u8[^'\"\\s]*", re.IGNORECASE)
API_RE = re.compile(r"['\"](/api/[^'\"\\s]+|[^'\"\\s]*(?:GetCameras|GetCamera|CameraListing)[^'\"\\s]*)['\"]", re.IGNORECASE)


def scan_javascript_asset(js_text: str, page_url: str, js_url: str) -> list[dict]:
    matches: list[dict] = []
    for m in HLS_RE.findall(js_text):
        matches.append({"kind": "hls_url", "value": urljoin(js_url, m)})
    for m in API_RE.findall(js_text):
        matches.append({"kind": "api_endpoint", "value": urljoin(page_url, m)})
    return matches

from __future__ import annotations

import re

JSON_FEED_RE = re.compile(r"https?://[^\s'\"]+\.(?:json|geojson)", re.I)
ARCGIS_RE = re.compile(r"https?://[^\s'\"]+/(?:FeatureServer|MapServer)/\d+", re.I)
CALTRANS_RE = re.compile(r"cwwp2\.dot\.ca\.gov", re.I)


def discover_endpoint_urls(text: str) -> list[str]:
    urls = set(JSON_FEED_RE.findall(text) + ARCGIS_RE.findall(text))
    if CALTRANS_RE.search(text):
        for d in range(1, 13):
            urls.add(f"https://cwwp2.dot.ca.gov/data/d{d}/cctv/cctvStatusD{d:02d}.json")
    return sorted(urls)

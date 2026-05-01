from __future__ import annotations

from typing import Any
from urllib.parse import urljoin


def walk_urls(payload: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def rec(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                rec(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                rec(v, f"{path}[{i}]")
        elif isinstance(node, str) and (node.startswith("http://") or node.startswith("https://") or ".m3u8" in node):
            out.append((path, node))

    rec(payload)
    return out


def extract_camera_records(payload: Any, base_url: str = "") -> list[dict]:
    records: list[dict] = []
    if isinstance(payload, list):
        iterable = payload
    elif isinstance(payload, dict):
        iterable = payload.get("features") or payload.get("data") or payload.get("cameras") or []
    else:
        iterable = []

    for item in iterable:
        obj = item.get("properties", item) if isinstance(item, dict) else {}
        urls = walk_urls(obj)
        stream = next((u for _, u in urls if ".m3u8" in u), None)
        if not stream:
            continue
        lon = lat = None
        if isinstance(item, dict) and isinstance(item.get("geometry"), dict):
            coords = item["geometry"].get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                lon, lat = coords[0], coords[1]
        lat = obj.get("latitude", obj.get("lat", lat))
        lon = obj.get("longitude", obj.get("lon", lon))
        records.append({
            "stream_url": urljoin(base_url, stream),
            "viewer_url": obj.get("viewer_url") or obj.get("url"),
            "label": obj.get("name") or obj.get("camera_name") or obj.get("title") or "camera",
            "city": obj.get("city"),
            "country": obj.get("country"),
            "latitude": lat,
            "longitude": lon,
            "metadata": obj,
        })
    return records

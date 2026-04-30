from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class TargetIndicator(BaseModel):
    id: str
    raw_text: str
    target_type: str
    normalized_name: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bbox: list[float] | None = None
    source_url: str | None = None
    domain: str | None = None
    confidence: float = 0.0
    reason: str | None = None


class TargetResolutionResult(BaseModel):
    user_query: str
    targets: list[TargetIndicator] = Field(default_factory=list)
    insufficient_target: bool = False
    message: str | None = None


URL_RE = re.compile(r"https?://[^\s]+", re.I)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
COORD_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")
GEO_PHRASE_RE = re.compile(r"\b(?:in|from|near|around)\s+([A-Za-z][A-Za-z\s,\-]{2,80})", re.I)


class TargetResolutionSkill:
    def resolve(self, user_query: str, planner_locations: list[str] | None = None) -> TargetResolutionResult:
        targets: list[TargetIndicator] = []
        q = user_query.strip()

        for u in URL_RE.findall(q):
            p = urlparse(u)
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=u, target_type="url", normalized_name=p.netloc, source_url=u, domain=p.netloc.lower(), confidence=0.95, reason="detected URL"))

        for ip in IP_RE.findall(q):
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=ip, target_type="ip", normalized_name=ip, confidence=0.7, reason="detected IPv4"))

        for m in COORD_RE.findall(q):
            lat, lon = float(m[0]), float(m[1])
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=f"{m[0]},{m[1]}", target_type="coordinates", latitude=lat, longitude=lon, normalized_name=f"{lat},{lon}", confidence=0.9, reason="detected coordinates"))

        for phrase in GEO_PHRASE_RE.findall(q):
            txt = phrase.strip(" .")
            if len(txt) > 2:
                targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=txt, target_type="place", normalized_name=txt, confidence=0.65, reason="detected geospatial phrase"))

        for loc in planner_locations or []:
            if loc and not any(t.normalized_name and t.normalized_name.casefold() == loc.casefold() for t in targets):
                targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=loc, target_type="place", normalized_name=loc, confidence=0.75, reason="planner target"))

        if not targets:
            return TargetResolutionResult(user_query=user_query, insufficient_target=True, message="I do not have enough location or source information to search for public HLS cameras. Please provide a place, landmark, region, country, coordinates, IP address, hostname, agency, or website to inspect.")

        return TargetResolutionResult(user_query=user_query, targets=targets)

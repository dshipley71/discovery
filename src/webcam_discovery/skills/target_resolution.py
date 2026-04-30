from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class TargetIndicator(BaseModel):
    id: str
    raw_text: str
    target_type: str
    normalized_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    source_url: str | None = None
    domain: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    confidence: float = 0.0
    reason: str | None = None


class TargetResolutionResult(BaseModel):
    user_query: str
    targets: list[TargetIndicator] = Field(default_factory=list)
    insufficient_target: bool = False
    message: str | None = None


URL_RE = re.compile(r"https?://[^\s]+", re.I)
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
COORD_RE = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")
PLACE_SPLIT_RE = re.compile(r"\b(?:from|in|near|around)\b", re.I)
INSUFFICIENT = {"find public hls cameras", "get all webcams", "find live traffic cameras", "find m3u8 feeds", "search for webcams"}
ALIAS_MAP = {"pennsylvania": ["PA"], "united kingdom": ["UK", "Britain"]}


class TargetResolutionSkill:
    def resolve(self, user_query: str, planner_locations: list[str] | None = None) -> TargetResolutionResult:
        q = user_query.strip()
        if q.casefold() in INSUFFICIENT:
            return TargetResolutionResult(user_query=q, insufficient_target=True, message=self._insufficient_message())

        targets: list[TargetIndicator] = []
        for u in URL_RE.findall(q):
            p = urlparse(u)
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=u, target_type="url", normalized_name=p.netloc, source_url=u, domain=p.netloc.lower(), confidence=0.95, reason="detected URL"))
        for h in HOST_RE.findall(q):
            if h.lower().startswith("http"):
                continue
            if any(t.domain == h.lower() for t in targets):
                continue
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=h, target_type="hostname", normalized_name=h.lower(), domain=h.lower(), confidence=0.8, reason="detected hostname"))
        for ip in IP_RE.findall(q):
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=ip, target_type="ip", normalized_name=ip, confidence=0.8, reason="detected IPv4"))
        for m in COORD_RE.findall(q):
            targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=f"{m[0]},{m[1]}", target_type="coordinates", normalized_name=f"{m[0]},{m[1]}", latitude=float(m[0]), longitude=float(m[1]), confidence=0.9, reason="detected coordinates"))

        place_part = PLACE_SPLIT_RE.split(q)
        if len(place_part) > 1:
            tail = place_part[-1]
            for token in re.split(r"\band\b|;", tail, flags=re.I):
                t = token.strip(" .,")
                if len(t) >= 3 and not URL_RE.search(t) and not COORD_RE.search(t):
                    n = t
                    aliases = ALIAS_MAP.get(n.casefold(), [])
                    targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=t, target_type="place", normalized_name=n, aliases=aliases, confidence=0.7, reason="detected geospatial phrase"))

        for loc in planner_locations or []:
            if loc and not any((ti.normalized_name or "").casefold() == loc.casefold() for ti in targets):
                targets.append(TargetIndicator(id=f"target-{len(targets)+1}", raw_text=loc, target_type="place", normalized_name=loc, aliases=ALIAS_MAP.get(loc.casefold(), []), confidence=0.75, reason="planner target"))

        dedup: list[TargetIndicator] = []
        seen = set()
        for t in targets:
            k = (t.target_type, (t.normalized_name or t.raw_text).casefold())
            if k not in seen:
                seen.add(k)
                dedup.append(t)
        if not dedup:
            return TargetResolutionResult(user_query=q, insufficient_target=True, message=self._insufficient_message())
        return TargetResolutionResult(user_query=q, targets=dedup)

    def _insufficient_message(self) -> str:
        return "I do not have enough location or source information to search for public HLS cameras. Please provide a place, landmark, region, country, coordinates, IP address, hostname, agency, or website to inspect."

from __future__ import annotations

from pydantic import BaseModel, Field


class LocationSearchPlan(BaseModel):
    original_locations: list[str] = Field(default_factory=list)
    expanded_locations: list[str] = Field(default_factory=list)
    agencies: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


_LOCATION_KB: dict[str, dict[str, list[str]]] = {}


class LocationExpansionSkill:
    def expand(
        self,
        target_locations: list[str],
        camera_types: list[str] | None = None,
        raw_query: str | None = None,
        max_queries: int = 25,
    ) -> LocationSearchPlan:
        locations = [l.strip() for l in target_locations if l and l.strip()]
        expanded_locations: list[str] = []
        agencies: list[str] = []
        domains: list[str] = []

        for loc in locations:
            key = loc.casefold()
            profile = _LOCATION_KB.get(key)
            if profile:
                expanded_locations.extend(profile.get("locations", []))
                agencies.extend(profile.get("agencies", []))
                domains.extend(profile.get("domains", []))
            else:
                expanded_locations.append(loc)
                agencies.append(f"{loc} transportation")

        c_types = [c.casefold() for c in (camera_types or [])]
        traffic_focus = any(any(k in c for k in ["traffic", "road", "dot", "highway", "511"]) for c in c_types)

        queries: list[str] = []
        for agency in agencies:
            if traffic_focus:
                queries.extend([f"{agency} traffic cameras live", f"{agency} traffic camera map"])

        for domain in domains:
            queries.extend([f"site:{domain} m3u8", f"site:{domain} HLS camera"])
            if traffic_focus:
                queries.extend([f"site:{domain} traffic cameras", f"site:{domain} live traffic camera"])

        for loc in expanded_locations or locations:
            if traffic_focus:
                queries.extend(
                    [
                        f"{loc} traffic camera live public",
                        f"{loc} live traffic cameras",
                        f"{loc} DOT traffic cameras",
                    ]
                )
            queries.extend([f"{loc} public webcam m3u8", f"{loc} live camera HLS", f"{loc} public camera feed", f"{loc} live webcam stream"])

        if not queries and raw_query:
            return LocationSearchPlan(original_locations=locations, expanded_locations=[], agencies=[], domains=[], search_queries=[])

        deduped: list[str] = []
        seen: set[str] = set()
        for q in queries:
            if q not in seen:
                seen.add(q)
                deduped.append(q)

        return LocationSearchPlan(
            original_locations=locations,
            expanded_locations=_dedupe_preserve(expanded_locations),
            agencies=_dedupe_preserve(agencies),
            domains=_dedupe_preserve(domains),
            search_queries=deduped[:max_queries],
        )


def _dedupe_preserve(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out

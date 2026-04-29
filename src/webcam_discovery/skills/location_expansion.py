from __future__ import annotations

from pydantic import BaseModel, Field


class LocationSearchPlan(BaseModel):
    original_locations: list[str] = Field(default_factory=list)
    expanded_locations: list[str] = Field(default_factory=list)
    agencies: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


_LOCATION_KB: dict[str, dict[str, list[str]]] = {
    "pennsylvania": {
        "agencies": [
            "PennDOT",
            "511PA",
            "Pennsylvania Department of Transportation",
        ],
        "locations": [
            "Pennsylvania",
            "Philadelphia",
            "Pittsburgh",
            "Harrisburg",
            "Erie",
            "Allentown",
            "Scranton",
        ],
        "domains": ["511pa.com", "pa.gov", "penndot.pa.gov"],
    }
}


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
                agencies.extend(
                    [
                        f"{loc} Department of Transportation",
                        f"{loc} DOT",
                        f"{loc} 511",
                    ]
                )

        c_types = [c.casefold() for c in (camera_types or [])]
        traffic_focus = (not c_types) or any("traffic" in c for c in c_types)

        queries: list[str] = []
        for agency in agencies:
            queries.extend(
                [
                    f"{agency} traffic cameras live",
                    f"{agency} traffic camera map",
                ]
            )

        for domain in domains:
            queries.extend(
                [
                    f"site:{domain} traffic cameras",
                    f"site:{domain} live traffic camera",
                    f"site:{domain} m3u8",
                    f"site:{domain} HLS traffic camera",
                ]
            )

        for loc in expanded_locations or locations:
            if traffic_focus:
                queries.extend(
                    [
                        f"{loc} traffic camera live public",
                        f"{loc} live traffic cameras",
                        f"{loc} DOT traffic cameras",
                    ]
                )
            queries.extend([
                f"{loc} live camera m3u8",
                f"{loc} HLS camera",
            ])

        if not queries and raw_query:
            rq = raw_query.strip()
            queries = [
                f"{rq} traffic camera live public",
                f"{rq} live camera m3u8",
                f"{rq} HLS camera",
                f"{rq} public webcam stream",
            ]

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

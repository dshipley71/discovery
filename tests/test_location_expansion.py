from webcam_discovery.skills.location_expansion import LocationExpansionSkill


def test_pennsylvania_expansion() -> None:
    plan = LocationExpansionSkill().expand(
        target_locations=["Pennsylvania"],
        camera_types=["traffic cameras"],
        raw_query="Get me all live traffic cameras from Pennsylvania",
    )

    assert "PennDOT" in plan.agencies
    assert "511PA" in plan.agencies
    assert "Philadelphia" in plan.expanded_locations
    assert "Pittsburgh" in plan.expanded_locations
    assert "Harrisburg" in plan.expanded_locations
    assert "511pa.com" in plan.domains
    assert "pa.gov" in plan.domains
    joined = "\n".join(plan.search_queries)
    assert "PennDOT traffic cameras" in joined
    assert "site:511pa.com" in joined
    assert "Pennsylvania traffic camera" in joined


def test_unknown_location_fallback() -> None:
    plan = LocationExpansionSkill().expand(
        target_locations=["Exampleland"],
        camera_types=["traffic cameras"],
        raw_query="",
    )
    joined = "\n".join(plan.search_queries)
    assert "Exampleland traffic camera" in joined
    assert "Exampleland DOT traffic cameras" in joined
    assert "Exampleland Department of Transportation traffic cameras" in joined


def test_raw_query_fallback_without_hardcoded_locations() -> None:
    query = "Find public traffic cameras near Lancaster County Pennsylvania"
    plan = LocationExpansionSkill().expand(target_locations=[], camera_types=[], raw_query=query)
    joined = "\n".join(plan.search_queries)
    assert query in joined
    for forbidden in [
        "New York City", "London", "Tokyo", "Paris", "Sydney", "Dubai",
        "Russia", "China", "Ukraine", "Africa", "Israel",
    ]:
        assert forbidden not in joined

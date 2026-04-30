from webcam_discovery.skills.location_expansion import LocationExpansionSkill


def test_unknown_location_general_queries() -> None:
    plan = LocationExpansionSkill().expand(target_locations=["Exampleland"], camera_types=[], raw_query="")
    joined = "\n".join(plan.search_queries)
    assert "Exampleland public webcam m3u8" in joined
    assert "Exampleland live camera HLS" in joined


def test_traffic_query_includes_traffic_terms() -> None:
    plan = LocationExpansionSkill().expand(target_locations=["Exampleland"], camera_types=["traffic cameras"], raw_query="")
    joined = "\n".join(plan.search_queries)
    assert "traffic camera" in joined.lower()


def test_raw_query_without_targets_no_default_locations() -> None:
    query = "Find public HLS cameras"
    plan = LocationExpansionSkill().expand(target_locations=[], camera_types=[], raw_query=query)
    assert plan.search_queries == []

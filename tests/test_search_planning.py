from webcam_discovery.skills.location_expansion import LocationExpansionSkill


def test_general_camera_queries_default_non_traffic():
    plan = LocationExpansionSkill().expand(["Paris"], [], "Get cameras from Paris", max_queries=20)
    joined = "\n".join(plan.search_queries)
    assert "Paris public webcam m3u8" in joined
    assert "Paris live camera HLS" in joined
    assert "Paris public camera feed" in joined


def test_traffic_only_when_requested():
    traffic = LocationExpansionSkill().expand(["Pennsylvania"], ["traffic cameras"], "Get traffic cameras", max_queries=30)
    beach = LocationExpansionSkill().expand(["Sydney, Australia"], ["beach cameras"], "Get beach cameras", max_queries=30)
    assert any("traffic" in q.lower() for q in traffic.search_queries)
    assert sum("traffic" in q.lower() for q in beach.search_queries) <= 1

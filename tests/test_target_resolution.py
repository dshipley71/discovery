from webcam_discovery.skills.target_resolution import TargetResolutionSkill


def test_insufficient_target_query_rejected():
    r = TargetResolutionSkill().resolve("Find public HLS cameras", planner_locations=[])
    assert r.insufficient_target is True
    assert "enough location" in (r.message or "")


def test_multi_location_targets():
    r = TargetResolutionSkill().resolve("Get public HLS cameras from London, England and Sydney, Australia", planner_locations=[])
    names = [t.normalized_name for t in r.targets if t.normalized_name]
    assert any("London" in n for n in names)
    assert any("Sydney" in n for n in names)

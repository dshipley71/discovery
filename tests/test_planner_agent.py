from webcam_discovery.agents.planner_agent import PlannerAgent


def test_normalize_plan_dict_coerces_scalar_to_list() -> None:
    raw = {
        "original_query": "Get me traffic cameras from Pennsylvania",
        "parsed_intent": {
            "geography": "Pennsylvania",
            "agencies": "PennDOT",
            "camera_types": "traffic cameras",
        },
        "target_locations": "Pennsylvania",
        "camera_types": "traffic cameras",
        "discovery_methods": "known_sources",
        "source_preferences": "511pa.com",
        "validation_enabled": True,
        "visual_stream_analysis_enabled": True,
        "video_summary_enabled": False,
        "output_artifacts": "camera.geojson",
        "public_source_only": True,
        "skip_restricted_sources": True,
        "reasoning_summary": "Use known statewide traffic portals first.",
    }

    normalized = PlannerAgent._normalize_plan_dict(raw)

    assert normalized["parsed_intent"]["geography"] == ["Pennsylvania"]
    assert normalized["parsed_intent"]["agencies"] == ["PennDOT"]
    assert normalized["parsed_intent"]["camera_types"] == ["traffic cameras"]
    assert normalized["discovery_methods"] == ["known_sources"]
    assert normalized["output_artifacts"] == ["camera.geojson"]

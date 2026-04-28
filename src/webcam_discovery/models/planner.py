from __future__ import annotations

from pydantic import BaseModel, Field


class PlannerIntent(BaseModel):
    geography: list[str] = Field(default_factory=list)
    agencies: list[str] = Field(default_factory=list)
    camera_types: list[str] = Field(default_factory=list)


class PlannerPlan(BaseModel):
    original_query: str
    parsed_intent: PlannerIntent
    target_locations: list[str] = Field(default_factory=list)
    camera_types: list[str] = Field(default_factory=list)
    discovery_methods: list[str] = Field(default_factory=list)
    source_preferences: list[str] = Field(default_factory=list)
    validation_enabled: bool = True
    visual_stream_analysis_enabled: bool = False
    video_summary_enabled: bool = False
    output_artifacts: list[str] = Field(default_factory=lambda: [
        "camera.geojson", "cameras.md", "map.html", "logs"
    ])
    public_source_only: bool = True
    skip_restricted_sources: bool = True
    reasoning_summary: str

#!/usr/bin/env python3
"""
schemas.py — Shared Pydantic models for the webcam discovery pipeline.
Single source of truth for CameraCandidate and CameraRecord.
All agents and skills import from here — never redefine these models elsewhere.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ── Feed types ────────────────────────────────────────────────────────────────

FeedType = Literal[
    "HLS_master",   # Master playlist: contains #EXT-X-STREAM-INF variant references
    "HLS_stream",   # Media playlist: contains #EXTINF segments (direct live stream)
    "unknown",
]

LegitimacyScore = Literal["high", "medium", "low"]
CameraStatus    = Literal["live", "dead", "unknown", "restricted", "timeout", "offline_http", "decode_failed"]


# ── Inter-agent models ────────────────────────────────────────────────────────

class CameraCandidate(BaseModel):
    """
    Raw camera candidate produced by DirectoryAgent and SearchAgent.
    Not yet validated or geo-enriched. Passed to ValidationAgent.
    """
    url:              str
    label:            Optional[str]  = None
    city:             Optional[str]  = None
    state_region:     Optional[str]  = None   # state / province / region extracted from URL path
    country:          Optional[str]  = None
    source_directory: Optional[str]  = None
    source_refs:      list[str]      = Field(default_factory=list)
    notes:            Optional[str]  = None
    latitude:         Optional[float] = None
    longitude:        Optional[float] = None
    viewer_url:       Optional[str] = None
    feed_endpoint:    Optional[str] = None
    source_page:      Optional[str] = None
    source_record_id: Optional[str] = None
    raw_metadata:     dict[str, Any] = Field(default_factory=dict)
    source_domain:    Optional[str] = None
    source_query:     Optional[str] = None
    target_locations: list[str] = Field(default_factory=list)
    url_metadata_hints: dict[str, Any] = Field(default_factory=dict)
    location_text_candidates: list[dict[str, Any]] = Field(default_factory=list)
    stream_substatus: Optional[str] = None
    stream_confidence: Optional[float] = None
    stream_reasons: list[str] = Field(default_factory=list)
    visual_metrics: dict[str, Any] = Field(default_factory=dict)


class ScopeEnforcementResult(BaseModel):
    has_sufficient_scope: bool
    scope_type: str | None = None
    scope_label: str | None = None
    scope_summary: str | None = None
    normalized_targets: list[str] = Field(default_factory=list)
    target_aliases: list[str] = Field(default_factory=list)
    included_locations: list[str] = Field(default_factory=list)
    excluded_locations: list[str] = Field(default_factory=list)
    included_sources: list[str] = Field(default_factory=list)
    excluded_sources: list[str] = Field(default_factory=list)
    agency_or_owners: list[str] = Field(default_factory=list)
    coordinates: list[dict[str, float]] = Field(default_factory=list)
    # Optional bbox metadata is an audit hint only unless verified by a
    # geocoder/source metadata. LLM-created bboxes must not be used as
    # authoritative rejection criteria.
    bbox: dict[str, Any] | None = None
    bbox_source: str | None = None
    bbox_verified: bool = False
    bbox_confidence: float | None = None
    bbox_warning: str | None = None
    hostnames: list[str] = Field(default_factory=list)
    ip_addresses: list[str] = Field(default_factory=list)
    camera_types: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    insufficient_scope_reason: str | None = None
    user_message: str | None = None
    raw_llm_response: dict[str, Any] | str | None = None


class ScopeDecision(BaseModel):
    decision: Literal["accept", "reject", "review"]
    confidence: float = 0.0
    reason: str
    matched_scope_terms: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    raw_llm_response: dict[str, Any] | str | None = None


class CameraRecord(BaseModel):
    """
    Validated, geo-enriched camera record.
    Produced by ValidationAgent; catalogued by CatalogAgent.
    All fields required before export to camera.geojson.
    url always points to the direct .m3u8 stream.
    """
    id:               str
    label:            str
    city:             str
    region:           Optional[str]  = None
    country:          str
    continent:        str
    latitude:         Optional[float] = None
    longitude:        Optional[float] = None
    url:              str                              # direct stream URL (.m3u8)
    feed_type:        FeedType        = "unknown"
    playlist_type:    Optional[Literal["master", "media"]] = None
    variant_streams:  list[str]       = Field(default_factory=list)            # variant URLs from master playlist
    source_directory: Optional[str]  = None
    source_refs:      list[str]      = Field(default_factory=list)
    legitimacy_score: LegitimacyScore = "medium"
    last_verified:    Optional[str]  = None           # ISO date string
    status:           CameraStatus   = "unknown"
    notes:            Optional[str]  = None
    stream_substatus: Optional[str] = None
    stream_confidence: Optional[float] = None
    stream_reasons: list[str] = Field(default_factory=list)
    visual_metrics: dict[str, Any] = Field(default_factory=dict)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    location_text_candidates: list[dict[str, Any]] = Field(default_factory=list)
    hls_status: Optional[str] = None
    validation_confidence: Optional[float] = None
    validation_reason: Optional[str] = None
    geocode_source: Optional[str] = None
    geocode_confidence: Optional[float] = None
    geocode_precision: Optional[str] = None
    geocode_reason: Optional[str] = None

    @field_validator("latitude")
    @classmethod
    def lat_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not -90 <= v <= 90:
            raise ValueError(f"latitude {v} out of range [-90, 90]")
        return v

    @field_validator("longitude")
    @classmethod
    def lon_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not -180 <= v <= 180:
            raise ValueError(f"longitude {v} out of range [-180, 180]")
        return v

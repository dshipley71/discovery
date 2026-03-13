#!/usr/bin/env python3
"""
schemas.py — Shared Pydantic models for the webcam discovery pipeline.
Single source of truth for CameraCandidate and CameraRecord.
All agents and skills import from here — never redefine these models elsewhere.
Part of the Public Webcam Discovery System.
"""
from __future__ import annotations
from datetime import date
from typing import Literal, Optional
from pydantic import BaseModel, HttpUrl, field_validator


# ── Feed types ────────────────────────────────────────────────────────────────

FeedType = Literal[
    "youtube_live",
    "static_refresh",
    "MJPEG",
    "HLS",
    "iframe",
    "js_player",
    "unknown",
]

LegitimacyScore = Literal["high", "medium", "low"]
CameraStatus    = Literal["live", "dead", "unknown"]


# ── Inter-agent models ────────────────────────────────────────────────────────

class CameraCandidate(BaseModel):
    """
    Raw camera candidate produced by DirectoryAgent and SearchAgent.
    Not yet validated or geo-enriched. Passed to ValidationAgent.
    """
    url:              str
    label:            Optional[str]  = None
    city:             Optional[str]  = None
    country:          Optional[str]  = None
    source_directory: Optional[str]  = None
    source_refs:      list[str]      = []
    notes:            Optional[str]  = None


class CameraRecord(BaseModel):
    """
    Validated, geo-enriched camera record.
    Produced by ValidationAgent; catalogued by CatalogAgent.
    All fields required before export to camera.geojson.
    """
    id:                 str
    label:              str
    city:               str
    region:             Optional[str]  = None
    country:            str
    continent:          str
    latitude:           Optional[float] = None
    longitude:          Optional[float] = None
    url:                str
    stream_url:         Optional[str]  = None
    direct_stream_url:  Optional[str]  = None
    video_id:           Optional[str]  = None
    feed_type:          FeedType       = "unknown"
    source_directory:   Optional[str]  = None
    source_refs:        list[str]      = []
    legitimacy_score:   LegitimacyScore = "medium"
    requires_js:        bool           = False
    geo_restricted:     bool           = False
    last_verified:      Optional[str]  = None   # ISO date string
    status:             CameraStatus   = "unknown"
    notes:              Optional[str]  = None

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

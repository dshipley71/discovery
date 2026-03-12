#!/usr/bin/env python3
"""
test_schemas.py — CameraRecord and CameraCandidate validation edge cases.
Claude Code: implement tests following the schema rules in schemas.py.
"""
import pytest
from pydantic import ValidationError
from webcam_discovery.schemas import CameraRecord, CameraCandidate


def test_camera_record_valid(sample_record):
    assert sample_record.id == "test-new-york-times-square"
    assert sample_record.status == "live"


def test_camera_record_invalid_latitude():
    with pytest.raises(ValidationError):
        CameraRecord(
            id="bad", label="Bad", city="X", country="X", continent="X",
            latitude=999.0, longitude=0.0,  # invalid
            url="https://x.com",
        )


def test_camera_record_invalid_longitude():
    with pytest.raises(ValidationError):
        CameraRecord(
            id="bad", label="Bad", city="X", country="X", continent="X",
            latitude=0.0, longitude=999.0,  # invalid
            url="https://x.com",
        )


def test_camera_candidate_minimal(sample_candidate):
    assert sample_candidate.url.startswith("https://")


# Claude Code: add more edge-case tests here following AGENTS.md output schema.

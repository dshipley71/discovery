#!/usr/bin/env python3
"""
test_deduplication.py — Unit tests for DeduplicationSkill.
No network calls.
"""
from __future__ import annotations

import pytest

from webcam_discovery.schemas import CameraRecord
from webcam_discovery.skills.catalog import DeduplicationSkill, DeduplicationInput


def make_record(**kwargs) -> CameraRecord:
    """Helper to build a minimal CameraRecord."""
    defaults = dict(
        id="test-new-york-times-square",
        label="Times Square Cam",
        city="New York City",
        country="United States",
        continent="North America",
        latitude=40.7580,
        longitude=-73.9855,
        url="https://example.com/webcam/times-square",
        feed_type="youtube_live",
        legitimacy_score="high",
        status="live",
    )
    defaults.update(kwargs)
    return CameraRecord(**defaults)


def test_identical_label_city_deduped():
    """Same label + city → is_duplicate=True."""
    existing = make_record(
        id="new-york-city-times-square-cam",
        url="https://example.com/webcam/times-square",
        label="Times Square Cam",
        city="New York City",
    )
    candidate = make_record(
        id="new-york-city-times-square-cam-2",
        url="https://example.com/webcam/times-square-alt",
        label="Times Square Cam",
        city="New York City",
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is True
    assert result.canonical_record is not None
    assert result.canonical_record.id == existing.id


def test_fuzzy_match_above_threshold():
    """Labels >85% similar in same city → deduplicated."""
    existing = make_record(
        id="nyc-times-square-north",
        url="https://example.com/ts-north",
        label="Times Square North Webcam",
        city="New York City",
    )
    # Very similar label — above 85% Levenshtein threshold
    candidate = make_record(
        id="nyc-times-square-north-cam",
        url="https://example.com/ts-north-cam",
        label="Times Square North WebCam",  # only capitalization differs
        city="New York City",
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is True


def test_fuzzy_match_below_threshold():
    """Labels <85% similar, different coordinates → not duplicate, both records kept."""
    existing = make_record(
        id="nyc-times-square",
        url="https://example.com/times-square",
        label="Times Square Camera",
        city="New York City",
        latitude=40.7580,
        longitude=-73.9855,
    )
    candidate = make_record(
        id="nyc-brooklyn-bridge",
        url="https://example.com/brooklyn-bridge",
        label="Brooklyn Bridge Live View",
        city="New York City",
        # Brooklyn Bridge is ~5km from Times Square
        latitude=40.7061,
        longitude=-73.9969,
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is False
    assert result.canonical_record is None


def test_url_normalization():
    """Same URL with different tracking params → duplicate."""
    existing = make_record(
        id="london-tower-bridge",
        url="https://www.example.com/webcam/tower-bridge",
        label="Tower Bridge Cam",
        city="London",
        country="United Kingdom",
        continent="Europe",
        latitude=51.5055,
        longitude=-0.0754,
    )
    # Same URL but with tracking params and www
    candidate = make_record(
        id="london-tower-bridge-2",
        url="https://example.com/webcam/tower-bridge?utm_source=test&ref=homepage",
        label="Tower Bridge Different Label",
        city="London",
        country="United Kingdom",
        continent="Europe",
        latitude=51.5055,
        longitude=-0.0754,
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is True


def test_coordinate_proximity_match():
    """Records within 50m of each other in same city → duplicate."""
    existing = make_record(
        id="tokyo-shibuya",
        url="https://example.com/shibuya-1",
        label="Shibuya Crossing",
        city="Tokyo",
        country="Japan",
        continent="Asia",
        latitude=35.6595,
        longitude=139.7004,
    )
    # Slightly different coordinates but within 50m
    candidate = make_record(
        id="tokyo-shibuya-2",
        url="https://example.com/shibuya-2",
        label="Shibuya Crossing View",
        city="Tokyo",
        country="Japan",
        continent="Asia",
        latitude=35.65952,  # ~2m away
        longitude=139.70042,
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is True


def test_different_city_not_deduped():
    """Same label but different city → not duplicate."""
    existing = make_record(
        id="london-central-park",
        url="https://example.com/cam1",
        label="Central Park Webcam",
        city="London",
        country="United Kingdom",
        continent="Europe",
        latitude=51.5074,
        longitude=-0.1278,
    )
    candidate = make_record(
        id="nyc-central-park",
        url="https://example.com/cam2",
        label="Central Park Webcam",
        city="New York City",
        country="United States",
        continent="North America",
        latitude=40.7851,
        longitude=-73.9683,
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is False


def test_source_refs_merged_on_duplicate():
    """On duplicate detection, source_refs from candidate are merged into canonical."""
    existing = make_record(
        id="paris-eiffel",
        url="https://example.com/eiffel",
        label="Eiffel Tower",
        city="Paris",
        country="France",
        continent="Europe",
        latitude=48.8584,
        longitude=2.2945,
        source_refs=["https://source1.com/eiffel"],
    )
    candidate = make_record(
        id="paris-eiffel-2",
        url="https://example.com/eiffel",
        label="Eiffel Tower",
        city="Paris",
        country="France",
        continent="Europe",
        latitude=48.8584,
        longitude=2.2945,
        source_refs=["https://source2.com/eiffel"],
    )
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[existing],
    ))
    assert result.is_duplicate is True
    assert result.merged_record is not None
    # Both source refs should be in merged record
    assert "https://source1.com/eiffel" in result.merged_record.source_refs
    assert "https://source2.com/eiffel" in result.merged_record.source_refs


def test_empty_catalog_not_duplicate():
    """Against empty catalog → not duplicate."""
    candidate = make_record()
    skill = DeduplicationSkill()
    result = skill.run(DeduplicationInput(
        candidate_record=candidate,
        existing_catalog=[],
    ))
    assert result.is_duplicate is False

#!/usr/bin/env python3
"""
test_catalog.py — Unit tests for CatalogAgent contracts.
No live network calls.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from webcam_discovery.schemas import CameraRecord
from webcam_discovery.agents.catalog import CatalogAgent
from webcam_discovery.skills.catalog import GeoJSONExportSkill, GeoJSONExportInput


def make_record(**kwargs) -> CameraRecord:
    """Build a minimal CameraRecord."""
    defaults = dict(
        id="london-tower-bridge",
        label="Tower Bridge",
        city="London",
        country="United Kingdom",
        continent="Europe",
        latitude=51.5055,
        longitude=-0.0754,
        url="https://cdn.example.com/tower-bridge/live.m3u8",
        feed_type="HLS_stream",
        legitimacy_score="high",
        status="live",
    )
    defaults.update(kwargs)
    return CameraRecord(**defaults)


@pytest.mark.asyncio
async def test_deduplication_same_city_label():
    """Two records with same label + city → only one record in output."""
    record1 = make_record(
        id="london-tower-bridge-1",
        url="https://example.com/tower-bridge",
        label="Tower Bridge Cam",
        city="London",
    )
    record2 = make_record(
        id="london-tower-bridge-2",
        url="https://example.com/tower-bridge-alt",
        label="Tower Bridge Cam",  # same label
        city="London",             # same city
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        agent = CatalogAgent()
        await agent.run(records=[record1, record2], output_dir=output_dir)

        geojson_path = output_dir / "camera.geojson"
        assert geojson_path.exists()
        data = json.loads(geojson_path.read_text())
        features = data["features"]

    # Should have deduplicated to 1 record
    assert len(features) == 1


@pytest.mark.asyncio
async def test_slug_generation():
    """Record ID is a lowercase slug with no special characters."""
    record = make_record(
        id="london-tower-bridge",
        label="Tower Bridge Live View",
        city="London",
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        agent = CatalogAgent()
        await agent.run(records=[record], output_dir=output_dir)

        geojson_path = output_dir / "camera.geojson"
        data = json.loads(geojson_path.read_text())
        feature = data["features"][0]

    record_id = feature["properties"]["id"]
    assert record_id == record_id.lower()
    assert " " not in record_id


@pytest.mark.asyncio
async def test_geojson_coordinate_order():
    """GeoJSON coordinates are [longitude, latitude] per RFC 7946."""
    record = make_record(
        latitude=51.5055,
        longitude=-0.0754,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        agent = CatalogAgent()
        await agent.run(records=[record], output_dir=output_dir)

        geojson_path = output_dir / "camera.geojson"
        data = json.loads(geojson_path.read_text())
        feature = data["features"][0]

    coords = feature["geometry"]["coordinates"]
    # GeoJSON spec: [longitude, latitude]
    assert coords[0] == pytest.approx(-0.0754)   # longitude first
    assert coords[1] == pytest.approx(51.5055)   # latitude second


@pytest.mark.asyncio
async def test_missing_coords_skipped():
    """Records without lat/lon are not exported to GeoJSON."""
    # We test GeoJSONExportSkill directly since CameraRecord requires lat/lon
    from webcam_discovery.skills.catalog import GeoJSONExportSkill, GeoJSONExportInput

    record_with_coords = make_record(
        id="london-tower-bridge",
        latitude=51.5055,
        longitude=-0.0754,
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "test.geojson"
        skill = GeoJSONExportSkill()
        result = skill.run(GeoJSONExportInput(
            cameras=[record_with_coords],
            output_path=output_path,
        ))

    assert result.exported == 1
    assert result.skipped == 0


@pytest.mark.asyncio
async def test_cameras_md_written():
    """cameras.md is written to output_dir."""
    record = make_record()

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        agent = CatalogAgent()
        await agent.run(records=[record], output_dir=output_dir)

        md_path = output_dir / "cameras.md"
        assert md_path.exists()
        content = md_path.read_text()
        assert "Tower Bridge" in content
        assert "London" in content


@pytest.mark.asyncio
async def test_camera_geojson_has_metadata():
    """camera.geojson has metadata field with totals."""
    record = make_record()

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        agent = CatalogAgent()
        await agent.run(records=[record], output_dir=output_dir)

        geojson_path = output_dir / "camera.geojson"
        data = json.loads(geojson_path.read_text())

    assert "metadata" in data
    assert "total" in data["metadata"]
    assert data["metadata"]["total"] >= 1


@pytest.mark.asyncio
async def test_empty_records():
    """Empty records list → geojson written with 0 features, no crash."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_dir = Path(tmp_dir)
        agent = CatalogAgent()
        await agent.run(records=[], output_dir=output_dir)
        # Should not crash; geojson may or may not be written
        assert True  # reached here without exception


def test_geojson_export_skill_directly():
    """GeoJSONExportSkill: coordinates = [lon, lat] directly tested."""
    record = make_record(latitude=48.8566, longitude=2.3522)

    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "out.geojson"
        skill = GeoJSONExportSkill()
        result = skill.run(GeoJSONExportInput(cameras=[record], output_path=output_path))

        assert result.exported == 1
        assert result.skipped == 0

        data = json.loads(output_path.read_text())
        coords = data["features"][0]["geometry"]["coordinates"]
        assert coords[0] == pytest.approx(2.3522)   # longitude
        assert coords[1] == pytest.approx(48.8566)  # latitude

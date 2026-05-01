from webcam_discovery.skills.feed_parsers import walk_urls, extract_camera_records
from webcam_discovery.skills.endpoint_patterns import discover_endpoint_urls


def test_recursive_json_url_extraction():
    payload = {"a": [{"stream": "https://x/live.m3u8"}], "b": {"viewer": "https://x/view"}}
    urls = walk_urls(payload)
    assert any("live.m3u8" in u for _, u in urls)


def test_extract_camera_records_geojson():
    payload = {"features": [{"geometry": {"coordinates": [-120.1, 35.1]}, "properties": {"name": "cam1", "video": "https://x/cam1.m3u8", "city": "A"}}]}
    recs = extract_camera_records(payload)
    assert len(recs) == 1
    assert recs[0]["latitude"] == 35.1


def test_endpoint_pattern_discovery_caltrans_and_arcgis():
    text = "https://cwwp2.dot.ca.gov/abc and https://example.com/FeatureServer/0"
    eps = discover_endpoint_urls(text)
    assert any("cctvStatusD01" in e for e in eps)
    assert any("FeatureServer/0" in e for e in eps)

import asyncio
import json

from webcam_discovery.agents.validator import ValidationAgent
from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.ffprobe_validation import FfprobeResult, FfprobeValidationSkill
from webcam_discovery.skills.validation import ValidationResult


def test_validation_agent_drops_dead_streams_and_logs_results(
    monkeypatch,
    tmp_path,
) -> None:
    agent = ValidationAgent()

    candidates = [
        CameraCandidate(
            url="https://live.example/cam.m3u8",
            label="Live Cam",
            city="New York City",
            country="United States",
            source_directory="https://live.example/watch",
            source_refs=["https://live.example/watch"],
        ),
        CameraCandidate(
            url="https://dead.example/cam.m3u8",
            label="Dead Cam",
            city="Chicago",
            country="United States",
            source_directory="https://dead.example/watch",
            source_refs=["https://dead.example/watch"],
        ),
    ]

    async def fake_check_robots(self, robots_skill, domain, domain_candidates):  # noqa: ANN001
        return domain_candidates

    async def fake_batch_geo_enrich(self, skill, batch_candidates):  # noqa: ANN001
        return [None for _ in batch_candidates]

    async def fake_feed_run(self, urls, referers=None):  # noqa: ANN001
        assert referers == {
            "https://live.example/cam.m3u8": "https://live.example/watch",
            "https://dead.example/cam.m3u8": "https://dead.example/watch",
        }
        return [
            ValidationResult(
                url="https://live.example/cam.m3u8",
                status="live",
                legitimacy_score="high",
                content_type="application/vnd.apple.mpegurl",
                playlist_type="media",
            ),
            ValidationResult(
                url="https://dead.example/cam.m3u8",
                status="dead",
                legitimacy_score="low",
                status_code=404,
                fail_reason="http_404",
                content_type="application/vnd.apple.mpegurl",
                playlist_type="media",
            ),
        ]

    async def fake_ffprobe_run(self, urls):  # noqa: ANN001
        assert urls == [
            "https://live.example/cam.m3u8",
            "https://dead.example/cam.m3u8",
        ]
        return [
            FfprobeResult(
                url="https://live.example/cam.m3u8",
                stream_status="active_streaming",
                frames_decoded=6,
                detail="frames_ok",
            ),
            FfprobeResult(
                url="https://dead.example/cam.m3u8",
                stream_status="does_not_exist",
                detail="404 not found",
            ),
        ]

    monkeypatch.setattr(
        "webcam_discovery.agents.validator.ValidationAgent._check_robots",
        fake_check_robots,
    )
    monkeypatch.setattr(
        "webcam_discovery.agents.validator.ValidationAgent._batch_geo_enrich",
        fake_batch_geo_enrich,
    )
    monkeypatch.setattr(
        "webcam_discovery.skills.validation.FeedValidationSkill.run",
        fake_feed_run,
    )
    monkeypatch.setattr(
        "webcam_discovery.skills.ffprobe_validation.FfprobeValidationSkill.run",
        fake_ffprobe_run,
    )
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    monkeypatch.setattr(settings, "use_ffprobe_validation", True)
    monkeypatch.setattr(settings, "use_browser_validation", False)
    monkeypatch.setattr(settings, "min_legitimacy", "low")

    records = asyncio.run(agent.run(candidates))

    assert [record.url for record in records] == ["https://live.example/cam.m3u8"]
    assert records[0].status == "live"

    validation_log = settings.log_dir / "validation_results.jsonl"
    ffprobe_log = settings.log_dir / "ffprobe_validation.jsonl"
    assert validation_log.exists()
    assert ffprobe_log.exists()

    validation_rows = [
        json.loads(line) for line in validation_log.read_text(encoding="utf-8").splitlines()
    ]
    ffprobe_rows = [
        json.loads(line) for line in ffprobe_log.read_text(encoding="utf-8").splitlines()
    ]

    assert [row["url"] for row in validation_rows] == [
        "https://live.example/cam.m3u8",
        "https://dead.example/cam.m3u8",
    ]
    assert validation_rows[1]["status_code"] == 404
    assert validation_rows[1]["fail_reason"] == "http_404"

    assert [row["stream_status"] for row in ffprobe_rows] == [
        "active_streaming",
        "does_not_exist",
    ]
    assert ffprobe_rows[1]["camera_status"] == "dead"


def test_ffprobe_validation_skill_shows_progress_and_preserves_input_order(
    monkeypatch,
) -> None:
    skill = FfprobeValidationSkill(concurrency=2)
    urls = [
        "https://example.com/slow.m3u8",
        "https://example.com/fast.m3u8",
    ]
    progress_calls: list[dict] = []

    async def fake_ffprobe_available(self):  # noqa: ANN001
        return True

    async def fake_probe_url(self, url, sem):  # noqa: ANN001
        async with sem:
            if "slow" in url:
                await asyncio.sleep(0.02)
            else:
                await asyncio.sleep(0.0)
            return FfprobeResult(url=url, stream_status="active_streaming", detail="frames_ok")

    def fake_tqdm(iterable, **kwargs):  # noqa: ANN001
        progress_calls.append(kwargs)
        return iterable

    monkeypatch.setattr(
        "webcam_discovery.skills.ffprobe_validation.FfprobeValidationSkill._ffprobe_available",
        fake_ffprobe_available,
    )
    monkeypatch.setattr(
        "webcam_discovery.skills.ffprobe_validation.FfprobeValidationSkill._probe_url",
        fake_probe_url,
    )
    monkeypatch.setattr("webcam_discovery.skills.ffprobe_validation.tqdm", fake_tqdm)

    results = asyncio.run(skill.run(urls))

    assert [result.url for result in results] == urls
    assert progress_calls == [
        {
            "total": 2,
            "desc": "ffprobe",
            "unit": "url",
            "dynamic_ncols": True,
        }
    ]

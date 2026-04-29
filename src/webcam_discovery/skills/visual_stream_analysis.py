from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from webcam_discovery.config import settings
from webcam_discovery.models.stream_analysis import StreamAnalysisResult
from webcam_discovery.skills.hls_playlist_analysis import inspect_playlist_growth


class VisualStreamAnalysis:
    async def analyze(self, url: str) -> StreamAnalysisResult:
        try:
            import numpy as np
        except Exception as exc:
            raise RuntimeError(
                "Visual analysis enabled but numpy is not installed. Install dependencies and retry."
            ) from exc

        frames = await asyncio.wait_for(self._sample_frames(url), timeout=settings.visual_timeout_seconds)
        if not frames:
            return StreamAnalysisResult(
                url=url,
                stream_status="unknown",
                stream_substatus="decode_failed",
                stream_confidence=0.2,
                stream_reasons=["No frames decoded from stream"],
                visual_metrics={"frames_decoded": 0},
            )

        arr = np.stack(frames)
        diffs = np.mean(np.abs(np.diff(arr.astype(np.float32), axis=0)), axis=(1, 2, 3)) if len(frames) > 1 else np.array([0.0])
        mean_brightness = float(arr.mean())
        blank_ratio = float((arr < 8).mean())
        similarity = 1.0 - float(diffs.mean() / 255.0)
        recurrence = float((diffs < 1.2).mean()) if len(diffs) else 1.0

        playlist = await inspect_playlist_growth(
            url,
            delay_seconds=settings.visual_playlist_growth_check_seconds,
            timeout=settings.request_timeout,
        )

        status = "live"
        substatus = "active_live_dynamic"
        reasons = ["Frames decoded successfully"]
        confidence = 0.7

        if blank_ratio > 0.98:
            status, substatus, confidence = "unknown", "decode_failed", 0.4
            reasons.append("Frames are mostly blank")
        elif similarity > 0.995:
            status, substatus, confidence = "live", "active_live_static_view", 0.65
            reasons.append("Very low adjacent-frame motion")
        if recurrence > 0.95 and playlist.get("playlist_segment_growth"):
            status, substatus, confidence = "unknown", "active_prerecorded_loop_short", 0.6
            reasons.append("High recurrence suggests looped visual content")

        metrics = {
            "frames_decoded": len(frames),
            "blank_frame_ratio": blank_ratio,
            "mean_brightness": mean_brightness,
            "adjacent_frame_similarity": similarity,
            "recurrence_score": recurrence,
            **playlist,
            "classification_confidence": confidence,
        }

        return StreamAnalysisResult(
            url=url,
            stream_status=status,
            stream_substatus=substatus,
            stream_confidence=confidence,
            stream_reasons=reasons,
            visual_metrics=metrics,
        )

    async def _sample_frames(self, url: str) -> list:
        try:
            import numpy as np
        except Exception:
            return []
        with tempfile.TemporaryDirectory() as tmpdir:
            out_pattern = str(Path(tmpdir) / "frame-%03d.jpg")
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", url,
                "-t", str(settings.visual_total_sample_duration_seconds),
                "-vf", f"fps=1/{max(settings.visual_dense_sample_seconds,1)}",
                "-frames:v", str(settings.visual_max_frames),
                out_pattern,
            ]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            if proc.returncode != 0:
                return []

            from PIL import Image
            frames = []
            for path in sorted(Path(tmpdir).glob("frame-*.jpg")):
                try:
                    frames.append(np.array(Image.open(path).convert("RGB")))
                except Exception:
                    continue
            return frames

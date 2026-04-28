from __future__ import annotations

import asyncio
from dataclasses import dataclass

from webcam_discovery.config import settings
from webcam_discovery.schemas import CameraRecord
from webcam_discovery.skills.visual_stream_analysis import VisualStreamAnalysis
from webcam_discovery.skills.audio_transcription import extract_audio_sample, transcribe_with_faster_whisper


@dataclass
class VideoSummaryResult:
    camera_url: str
    sample_duration: int
    frame_summary: str
    audio_summary: str
    combined_summary: str
    confidence: float
    error: str | None = None


class VideoSummarizationAgent:
    async def summarize(self, record: CameraRecord) -> VideoSummaryResult:
        analyzer = VisualStreamAnalysis()
        analysis = await asyncio.wait_for(
            analyzer.analyze(record.url),
            timeout=settings.video_summary_timeout_seconds,
        )

        frame_summary = (
            f"Visual summary: substatus={analysis.stream_substatus}; "
            f"reasons={'; '.join(analysis.stream_reasons)}"
        )
        audio_summary = "Audio summary: audio disabled."

        if settings.video_summary_enable_audio:
            try:
                audio_path = extract_audio_sample(record.url, settings.video_summary_audio_duration_seconds)
                transcript = transcribe_with_faster_whisper(audio_path, settings.video_summary_whisper_model)
                audio_summary = (
                    "Audio summary: " + transcript
                    if transcript
                    else "Audio summary: No clear speech detected during sampled interval."
                )
            except Exception as exc:
                audio_summary = f"Audio summary: extraction/transcription failed ({exc})."

        combined = f"{frame_summary} {audio_summary}".strip()
        return VideoSummaryResult(
            camera_url=record.url,
            sample_duration=settings.video_summary_sample_duration_seconds,
            frame_summary=frame_summary,
            audio_summary=audio_summary,
            combined_summary=combined,
            confidence=analysis.stream_confidence or 0.5,
        )

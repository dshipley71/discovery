from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def extract_audio_sample(stream_url: str, duration_seconds: int) -> Path:
    tmpdir = Path(tempfile.mkdtemp(prefix="wcd-audio-"))
    out = tmpdir / "sample.wav"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", stream_url,
        "-t", str(duration_seconds),
        "-vn", "-ac", "1", "-ar", "16000",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {proc.stderr[:200]}")
    return out


def transcribe_with_faster_whisper(audio_file: Path, model_name: str) -> str:
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError(
            "Video summarization audio transcription requires faster-whisper. Install with: pip install 'webcam-discovery[video-summary]'"
        ) from exc
    model = WhisperModel(model_name)
    segments, _info = model.transcribe(str(audio_file), vad_filter=True)
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    return text

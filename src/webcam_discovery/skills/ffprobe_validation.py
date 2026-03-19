#!/usr/bin/env python3
"""
ffprobe_validation.py — ffprobe/ffmpeg-based stream liveness verification.
Part of the Public Webcam Discovery System.

Why this exists
---------------
HTTP-level probing (FeedValidationSkill) confirms a stream URL is reachable and
returns valid HLS/MJPEG headers, but it cannot distinguish:

  - A stream with active live video vs. a playlist serving stale/empty segments
  - A valid playlist with blank or frozen frames (camera covered/offline/dark)
  - A CDN that returns 200 OK on a playlist URL even when the camera is offline

This skill runs ``ffprobe`` to decode a small number of frames from the stream
and applies signal-analysis heuristics (brightness, entropy, inter-frame motion)
to classify each URL as:

  active_streaming  — frames decode; image has content (entropy > threshold,
                      motion detected between frames)
  active_blank      — frames decode but are blank, black, or frozen
                      (low entropy / no inter-frame difference)
  disabled          — ffprobe can open the URL but finds no video stream, or
                      segment fetching fails (CDN offline, expired token, etc.)
  does_not_exist    — ffprobe cannot connect at all (DNS failure, 404, etc.)

Status mapping for CameraRecord:
  active_streaming  → "live"
  active_blank      → "unknown"   (stream exists but may not be viewable)
  disabled          → "dead"      (confirmed non-functional)
  does_not_exist    → "dead"

The skill gracefully degrades if ffprobe/ffmpeg are not installed — it returns
``None`` for every URL so the caller can treat results as absent.

Requires:
  apt-get install -y ffmpeg      (Ubuntu/Debian/Colab)
  brew install ffmpeg            (macOS)
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from loguru import logger
from pydantic import BaseModel


# ── Types ─────────────────────────────────────────────────────────────────────

StreamStatus = Literal["active_streaming", "active_blank", "disabled", "does_not_exist"]


# ── I/O models ────────────────────────────────────────────────────────────────

class FfprobeResult(BaseModel):
    """Result of ffprobe/frame-analysis for a single URL."""

    url: str
    stream_status: Optional[StreamStatus] = None
    """``None`` when ffprobe is unavailable or an unexpected error occurred."""

    frames_decoded: int = 0
    mean_brightness: Optional[float] = None
    entropy_avg: Optional[float] = None
    interframe_diff_max: Optional[float] = None
    """Max mean absolute difference between successive frames; low → frozen."""

    detail: str = ""
    """Human-readable explanation of the classification."""

    ffprobe_available: bool = True
    """``False`` when the ffprobe binary was not found — all other fields are default."""

    @property
    def camera_status(self) -> Optional[str]:
        """Map stream_status to CameraRecord status string."""
        if self.stream_status == "active_streaming":
            return "live"
        if self.stream_status == "active_blank":
            return "unknown"
        if self.stream_status in ("disabled", "does_not_exist"):
            return "dead"
        return None  # ffprobe unavailable — caller should not change existing status


# ── Constants ─────────────────────────────────────────────────────────────────

# Thresholds for blank/frozen detection — tuned for typical webcam streams.
_BLANK_DARK_RATIO_THRESHOLD  = 0.90   # fraction of pixels below value 12 → "dark"
_BLANK_STD_THRESHOLD         = 8.0    # std-dev of pixel values → low = uniform
_BLANK_ENTROPY_THRESHOLD     = 2.0    # bits of entropy in histogram → low = blank
_FROZEN_DIFF_THRESHOLD       = 1.5    # max inter-frame mean-abs-diff → low = frozen

# How many seconds / fps to sample from the stream.
_SAMPLE_SECONDS = 6
_SAMPLE_FPS     = 1   # 6 frames total — enough for blank/frozen detection

# ffprobe/ffmpeg invocation timeouts.
_FFPROBE_TIMEOUT = 20   # seconds
_FFMPEG_TIMEOUT  = 40   # seconds (includes download time for sample)

# Regex to detect common "no such host" / connection refused ffprobe errors.
_DNS_FAIL_RE = re.compile(
    r"(no such host|connection refused|name or service not known|"
    r"network is unreachable|failed to open|404 not found|403 forbidden)",
    re.IGNORECASE,
)


# ── FfprobeValidationSkill ────────────────────────────────────────────────────

class FfprobeValidationSkill:
    """
    Validate a list of live-stream URLs using ffprobe and frame analysis.

    Only HLS (.m3u8) URLs are probed — MJPEG and HTML page URLs are skipped.
    Concurrency is bounded by a semaphore to avoid overloading the host.

    Graceful degradation
    --------------------
    If the ``ffprobe`` binary is not found the skill returns a list of
    ``FfprobeResult`` objects with ``ffprobe_available=False`` for every URL,
    so the caller can detect the missing dependency and skip any status updates.
    """

    def __init__(self, concurrency: int = 5) -> None:
        """
        Args:
            concurrency: Max simultaneous ffprobe/ffmpeg subprocess calls.
                         Each call starts a new process and downloads a few
                         seconds of video, so keep this low (3-8).
        """
        self._concurrency = concurrency

    async def run(self, urls: list[str]) -> list[FfprobeResult]:
        """
        Probe each URL with ffprobe and return one FfprobeResult per input URL.

        Only HLS (.m3u8) URLs are probed; all others receive a result with
        ``stream_status=None`` and ``detail="skipped_not_hls"``.

        Args:
            urls: Stream URLs to probe (typically confirmed-live HLS URLs from
                  FeedValidationSkill).

        Returns:
            list[FfprobeResult] in the same order as urls.
        """
        # Quick availability check before spawning any tasks.
        if not await self._ffprobe_available():
            logger.warning(
                "FfprobeValidationSkill: ffprobe not found — "
                "install ffmpeg (apt-get install -y ffmpeg) to enable frame analysis"
            )
            return [
                FfprobeResult(url=u, ffprobe_available=False, detail="ffprobe_not_installed")
                for u in urls
            ]

        sem = asyncio.Semaphore(self._concurrency)
        tasks = [self._probe_url(url, sem) for url in urls]
        return list(await asyncio.gather(*tasks))

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _ffprobe_available(self) -> bool:
        """Return True if the ffprobe binary is on PATH."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except FileNotFoundError:
            return False

    async def _probe_url(self, url: str, sem: asyncio.Semaphore) -> FfprobeResult:
        """Run ffprobe + frame sampling for one URL under the semaphore."""
        # Only probe HLS streams — skip HTML pages and MJPEG
        u_lower = url.lower()
        if ".m3u8" not in u_lower:
            return FfprobeResult(url=url, detail="skipped_not_hls")

        async with sem:
            try:
                return await asyncio.wait_for(
                    self._classify(url),
                    timeout=_FFPROBE_TIMEOUT + _FFMPEG_TIMEOUT + 5,
                )
            except asyncio.TimeoutError:
                return FfprobeResult(url=url, stream_status="disabled", detail="ffprobe_timeout")
            except Exception as exc:
                logger.debug("FfprobeValidationSkill: unexpected error for {}: {}", url, exc)
                return FfprobeResult(url=url, stream_status="disabled", detail=str(exc)[:100])

    async def _classify(self, url: str) -> FfprobeResult:
        """
        Full classification pipeline for one HLS URL:
        1. ffprobe to confirm video stream presence
        2. ffmpeg frame sampling
        3. frame analysis (brightness + entropy + motion)
        """
        probe = await self._run_ffprobe(url)

        if not probe["ok"]:
            err = probe.get("error", "")
            if _DNS_FAIL_RE.search(err):
                return FfprobeResult(url=url, stream_status="does_not_exist", detail=err[:120])
            return FfprobeResult(url=url, stream_status="disabled", detail=err[:120])

        if not probe.get("has_video"):
            return FfprobeResult(
                url=url,
                stream_status="disabled",
                detail="no_video_stream_in_probe",
            )

        # Sample frames
        frames = await self._sample_frames(url)
        if not frames:
            return FfprobeResult(url=url, stream_status="disabled", detail="no_frames_decoded")

        analysis = _analyze_frames(frames)
        summary  = _summarize(analysis)

        if analysis["blank_like"]:
            return FfprobeResult(
                url=url,
                stream_status="active_blank",
                detail="blank_or_dark_frames",
                **summary,
            )
        if analysis["frozen_like"]:
            return FfprobeResult(
                url=url,
                stream_status="active_blank",
                detail="frozen_frames",
                **summary,
            )

        return FfprobeResult(
            url=url,
            stream_status="active_streaming",
            detail="frames_ok",
            **summary,
        )

    async def _run_ffprobe(self, url: str) -> dict:
        """Run ffprobe -show_streams and return parsed output."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_streams",
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_FFPROBE_TIMEOUT
            )
            if proc.returncode != 0:
                return {"ok": False, "error": stderr.decode(errors="replace").strip()[:200]}
            data = json.loads(stdout.decode(errors="replace"))
            video_streams = [
                s for s in data.get("streams", []) if s.get("codec_type") == "video"
            ]
            return {"ok": True, "has_video": bool(video_streams)}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "ffprobe_timeout"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def _sample_frames(self, url: str) -> list[bytes]:
        """
        Extract frames via ffmpeg and return them as JPEG bytes.

        Samples ``_SAMPLE_SECONDS`` seconds at ``_SAMPLE_FPS`` fps, yielding
        up to ``_SAMPLE_SECONDS * _SAMPLE_FPS`` frames.  Returns empty list
        if ffmpeg fails or the stream has no decodable frames.
        """
        frames: list[bytes] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            pattern = str(Path(tmpdir) / "frame_%03d.jpg")
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", url,
                "-t", str(_SAMPLE_SECONDS),
                "-vf", f"fps={_SAMPLE_FPS}",
                "-q:v", "5",
                pattern,
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_FFMPEG_TIMEOUT
                )
                if proc.returncode != 0:
                    logger.debug(
                        "FfprobeValidationSkill: ffmpeg error for {}: {}",
                        url,
                        stderr.decode(errors="replace").strip()[:100],
                    )
                    return frames
                for p in sorted(Path(tmpdir).glob("frame_*.jpg")):
                    frames.append(p.read_bytes())
            except (asyncio.TimeoutError, Exception) as exc:
                logger.debug("FfprobeValidationSkill: frame sampling error for {}: {}", url, exc)
        return frames


# ── Frame analysis (no cv2 dependency — pure stdlib + bytes) ──────────────────
#
# We deliberately avoid numpy / cv2 here so the skill runs in any environment
# without extra ML dependencies.  The analysis uses only stdlib operations on
# raw JPEG bytes decoded to greyscale pixel arrays via the built-in ``struct``
# module.  Accuracy is slightly lower than the cv2 version in the batch scanner,
# but sufficient to detect obviously blank or frozen frames.

def _jpeg_to_grey_pixels(jpeg_bytes: bytes) -> Optional[list[int]]:
    """
    Decode a JPEG to a flat list of 8-bit greyscale pixel values using PIL/Pillow.

    Returns None if Pillow is unavailable or the file cannot be decoded.
    PIL is a dependency of many packages and is almost always available.
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
        return list(img.getdata())
    except Exception:
        return None


def _frame_metrics(pixels: list[int]) -> dict:
    n = len(pixels)
    if n == 0:
        return {"mean": 0, "std": 0, "dark_ratio": 1.0, "entropy": 0.0}
    mean = sum(pixels) / n
    variance = sum((p - mean) ** 2 for p in pixels) / n
    std = math.sqrt(variance)
    dark_ratio = sum(1 for p in pixels if p < 12) / n
    # Shannon entropy from 256-bucket histogram
    hist = [0] * 256
    for p in pixels:
        hist[p] += 1
    entropy = 0.0
    for count in hist:
        if count > 0:
            prob = count / n
            entropy -= prob * math.log2(prob)
    return {"mean": mean, "std": std, "dark_ratio": dark_ratio, "entropy": entropy}


def _analyze_frames(jpeg_frames: list[bytes]) -> dict:
    """
    Analyse decoded frame metrics to detect blank and frozen streams.

    Returns a dict with:
        has_frames      bool
        blank_like      bool — frames are uniformly dark or featureless
        frozen_like     bool — frames are identical (no motion)
        stats           list[dict] per-frame metrics
        diffs           list[float] inter-frame pixel differences
    """
    pixel_lists = [_jpeg_to_grey_pixels(f) for f in jpeg_frames]
    pixel_lists = [p for p in pixel_lists if p is not None]

    if not pixel_lists:
        return {"has_frames": False, "blank_like": False, "frozen_like": False,
                "stats": [], "diffs": []}

    stats = [_frame_metrics(p) for p in pixel_lists]

    # Blank: most frames are dark + low variance + low entropy
    blank_count = sum(
        1 for s in stats
        if s["dark_ratio"] > _BLANK_DARK_RATIO_THRESHOLD
        and s["std"] < _BLANK_STD_THRESHOLD
        and s["entropy"] < _BLANK_ENTROPY_THRESHOLD
    )
    blank_like = blank_count >= max(2, math.ceil(len(stats) * 0.6))

    # Frozen: very little change between consecutive frames
    diffs: list[float] = []
    for a, b in zip(pixel_lists, pixel_lists[1:]):
        n = min(len(a), len(b))
        if n > 0:
            diffs.append(sum(abs(a[i] - b[i]) for i in range(n)) / n)
    frozen_like = bool(diffs) and max(diffs) < _FROZEN_DIFF_THRESHOLD

    return {
        "has_frames":  True,
        "blank_like":  blank_like,
        "frozen_like": frozen_like,
        "stats":       stats,
        "diffs":       diffs,
    }


def _summarize(analysis: dict) -> dict:
    """Flatten analysis into FfprobeResult keyword args."""
    stats = analysis.get("stats") or []
    diffs = analysis.get("diffs") or []
    return {
        "frames_decoded":       len(stats),
        "mean_brightness":      sum(s["mean"] for s in stats) / len(stats) if stats else None,
        "entropy_avg":          sum(s["entropy"] for s in stats) / len(stats) if stats else None,
        "interframe_diff_max":  max(diffs) if diffs else None,
    }


# ── Standalone test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    async def _main() -> None:
        urls = sys.argv[1:] or [
            "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.m3u8",
        ]
        skill = FfprobeValidationSkill()
        results = await skill.run(urls)
        for r in results:
            logger.info(
                "{} → status={} frames={} detail={}",
                r.url, r.stream_status, r.frames_decoded, r.detail,
            )

    asyncio.run(_main())

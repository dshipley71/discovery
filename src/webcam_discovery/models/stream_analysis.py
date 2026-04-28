from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StreamSubstatus = Literal[
    "dead_link",
    "offline_http",
    "restricted_http",
    "decode_failed",
    "active_live_static_view",
    "active_live_dynamic",
    "active_prerecorded_loop_short",
    "active_prerecorded_loop_long",
    "unknown",
]


class StreamAnalysisResult(BaseModel):
    url: str
    stream_status: Literal["live", "dead", "unknown"]
    stream_substatus: StreamSubstatus
    stream_confidence: float | None = None
    stream_reasons: list[str] = Field(default_factory=list)
    visual_metrics: dict = Field(default_factory=dict)

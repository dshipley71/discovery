from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from webcam_discovery.models.deep_discovery import StreamCandidate
from webcam_discovery.schemas import CameraCandidate
from webcam_discovery.skills.traversal import unwrap_player_url

HLS_URL_RE = re.compile(r"\.m3u8(?:\?|$)", re.IGNORECASE)
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "yclid", "twclid",
}


def is_direct_hls_url(url: str | None) -> bool:
    """Return True only for HTTP(S) URLs that appear to be direct HLS playlists."""
    if not url:
        return False
    clean = unwrap_player_url(str(url).strip())
    return clean.lower().startswith(("http://", "https://")) and bool(HLS_URL_RE.search(clean))


def normalize_stream_url(url: str | None) -> str:
    """
    Normalize a stream URL for identity/deduplication without assuming a source pattern.

    The query string is preserved except common tracking parameters because HLS
    tokens and signatures may be required for playback.  The fragment is always
    dropped because it is client-side only.
    """
    raw = unwrap_player_url(" ".join(str(url or "").split()))
    if not raw:
        return ""
    parsed = urlparse(raw)
    kept = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _TRACKING_PARAMS or key.lower().startswith("utm_"):
            continue
        kept.append((key, value))
    normalized = parsed._replace(
        scheme=(parsed.scheme or "").lower(),
        netloc=(parsed.netloc or "").lower(),
        path=(parsed.path or "/"),
        params="",
        query=urlencode(kept, doseq=True),
        fragment="",
    )
    return urlunparse(normalized)


def _source_domain(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return (parsed.netloc or value).lower().removeprefix("www.") or None


def candidate_identity_key(row: dict[str, Any]) -> str:
    """Use source-provided camera identity when available, otherwise stream URL."""
    camera_id = row.get("camera_id") or row.get("source_record_id")
    source_domain = row.get("source_domain") or _source_domain(row.get("source_page") or row.get("root_url"))
    if camera_id and source_domain:
        return f"source_id:{source_domain}:{camera_id}"
    return f"stream_url:{normalize_stream_url(row.get('candidate_url') or row.get('url'))}"


def _stream_candidate_to_row(stream: StreamCandidate, *, run_id: str | None = None, user_query: str | None = None) -> dict[str, Any]:
    return {
        "run_id": stream.run_id or run_id,
        "user_query": stream.user_query or user_query,
        "source_query": stream.source_query,
        "search_result_url": stream.search_result_url,
        "root_url": stream.root_url,
        "source_page": stream.source_page,
        "lineage": list(stream.parent_pages or []),
        "candidate_url": stream.candidate_url,
        "normalized_stream_url": normalize_stream_url(stream.candidate_url),
        "candidate_type": "direct_hls_stream" if is_direct_hls_url(stream.candidate_url) else stream.candidate_type,
        "discovered_by": stream.discovery_strategy or "agentic_discovery",
        "target_locations": list(stream.target_locations or []),
        "camera_types": list(stream.camera_types or []),
        "page_relevance_score": stream.page_relevance_score,
        "camera_likelihood_score": stream.camera_likelihood_score,
        "source_domain": _source_domain(stream.source_page or stream.root_url or stream.candidate_url),
    }


def _camera_candidate_to_row(candidate: CameraCandidate, *, run_id: str | None = None, user_query: str | None = None) -> dict[str, Any]:
    refs = list(candidate.source_refs or [])
    source_query = candidate.source_query or next((r[6:] for r in refs if isinstance(r, str) and r.startswith("query:")), None)
    source_page = candidate.source_page or next((r for r in refs if isinstance(r, str) and r.startswith(("http://", "https://"))), None)
    camera_id = candidate.source_record_id or (candidate.raw_metadata or {}).get("camera_id") or (candidate.raw_metadata or {}).get("id")
    return {
        "run_id": run_id,
        "user_query": user_query,
        "source_query": source_query,
        "search_result_url": source_page,
        "root_url": source_page,
        "source_page": source_page,
        "lineage": refs,
        "candidate_url": candidate.url,
        "normalized_stream_url": normalize_stream_url(candidate.url),
        "candidate_type": "direct_hls_stream" if is_direct_hls_url(candidate.url) else "non_hls_or_page",
        "discovered_by": "CameraCandidate",
        "camera_id": camera_id,
        "label": candidate.label,
        "latitude": candidate.latitude,
        "longitude": candidate.longitude,
        "city": candidate.city,
        "state_region": candidate.state_region,
        "country": candidate.country,
        "source_domain": candidate.source_domain or _source_domain(source_page or candidate.url),
        "source_record_id": candidate.source_record_id,
        "target_locations": list(candidate.target_locations or []),
        "camera_types": list((candidate.raw_metadata or {}).get("camera_types", [])),
        "raw_metadata": dict(candidate.raw_metadata or {}),
    }


def write_agentic_candidates(path: Path, items: list[StreamCandidate | CameraCandidate], *, run_id: str | None = None, user_query: str | None = None) -> dict[str, int]:
    """Write the raw agentic discovery artifact used as validation handoff."""
    rows: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, StreamCandidate):
            rows.append(_stream_candidate_to_row(item, run_id=run_id, user_query=user_query))
        else:
            rows.append(_camera_candidate_to_row(item, run_id=run_id, user_query=user_query))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return {"raw": len(rows), "direct_hls": sum(1 for row in rows if is_direct_hls_url(row.get("candidate_url")))}


def _row_to_stream_candidate(row: dict[str, Any], *, fallback_run_id: str | None = None, fallback_user_query: str | None = None) -> StreamCandidate:
    return StreamCandidate(
        run_id=row.get("run_id") or fallback_run_id,
        user_query=row.get("user_query") or fallback_user_query,
        source_query=row.get("source_query"),
        search_result_url=row.get("search_result_url"),
        root_url=row.get("root_url"),
        source_page=row.get("source_page"),
        parent_pages=list(row.get("lineage") or row.get("parent_pages") or []),
        depth=int(row.get("depth") or 0),
        discovery_strategy=row.get("discovered_by") or row.get("discovery_strategy") or "agentic_candidates_handoff",
        candidate_url=normalize_stream_url(row.get("candidate_url") or row.get("url")),
        candidate_type="direct_hls_stream",
        target_locations=list(row.get("target_locations") or []),
        camera_types=list(row.get("camera_types") or []),
        page_type=row.get("page_type") or "unknown",
        page_relevance_score=float(row.get("page_relevance_score") or 0.6),
        camera_likelihood_score=float(row.get("camera_likelihood_score") or 0.6),
    )


def _row_to_camera_candidate(row: dict[str, Any], stream: StreamCandidate, *, target_locations: list[str] | None = None) -> CameraCandidate:
    metadata = dict(row.get("raw_metadata") or {})
    metadata.update({
        "agentic_handoff": True,
        "identity_key": candidate_identity_key(row),
        "normalized_stream_url": stream.candidate_url,
        "source_page": row.get("source_page"),
        "source_query": row.get("source_query"),
        "lineage": row.get("lineage") or [],
        "geocode_source": "source_metadata" if row.get("latitude") is not None and row.get("longitude") is not None else None,
    })
    refs = [x for x in [row.get("source_page"), row.get("search_result_url"), row.get("root_url")] if x]
    refs.extend(str(x) for x in (row.get("lineage") or []) if x and x not in refs)
    if row.get("source_query"):
        refs.insert(0, f"query:{row.get('source_query')}")
    return CameraCandidate(
        url=stream.candidate_url,
        label=row.get("label"),
        city=row.get("city"),
        state_region=row.get("state_region") or row.get("region"),
        country=row.get("country"),
        source_refs=refs,
        latitude=row.get("latitude"),
        longitude=row.get("longitude"),
        source_page=row.get("source_page"),
        source_record_id=row.get("source_record_id") or row.get("camera_id"),
        raw_metadata=metadata,
        source_domain=row.get("source_domain") or _source_domain(row.get("source_page") or stream.candidate_url),
        source_query=row.get("source_query"),
        target_locations=list(target_locations or row.get("target_locations") or []),
    )


def load_agentic_candidate_handoff(
    path: Path,
    *,
    unique_output_path: Path | None = None,
    handoff_output_path: Path | None = None,
    fallback_run_id: str | None = None,
    fallback_user_query: str | None = None,
    target_locations: list[str] | None = None,
    max_candidates: int | None = None,
) -> tuple[list[StreamCandidate], dict[str, CameraCandidate], dict[str, int]]:
    """Load direct HLS candidates from agentic_candidates.jsonl and write audit artifacts."""
    rows: list[dict[str, Any]] = []
    malformed = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1

    unique_rows_by_key: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    skipped_non_hls = 0
    for row in rows:
        candidate_url = row.get("candidate_url") or row.get("url")
        if not is_direct_hls_url(candidate_url):
            skipped_non_hls += 1
            continue
        row = dict(row)
        row["candidate_url"] = normalize_stream_url(candidate_url)
        row["normalized_stream_url"] = row["candidate_url"]
        row["handoff_reason"] = "direct_hls_from_agentic_candidates"
        key = candidate_identity_key(row)
        row["identity_key"] = key
        if key in unique_rows_by_key:
            duplicate_count += 1
            continue
        unique_rows_by_key[key] = row

    all_unique_rows = list(unique_rows_by_key.values())
    capped_count = 0
    capped_rows: list[dict[str, Any]] = []
    unique_rows = all_unique_rows
    if max_candidates is not None and max_candidates >= 0 and len(all_unique_rows) > max_candidates:
        capped_count = len(all_unique_rows) - max_candidates
        unique_rows = all_unique_rows[:max_candidates]
        capped_rows = all_unique_rows[max_candidates:]

    streams: list[StreamCandidate] = []
    cameras: dict[str, CameraCandidate] = {}
    handoff_rows: list[dict[str, Any]] = []
    for row in unique_rows:
        stream = _row_to_stream_candidate(row, fallback_run_id=fallback_run_id, fallback_user_query=fallback_user_query)
        camera = _row_to_camera_candidate(row, stream, target_locations=target_locations)
        streams.append(stream)
        cameras[normalize_stream_url(stream.candidate_url)] = camera
        handoff_rows.append({
            **row,
            "sent_to_scope_review": True,
            "validation_allowed": True,
            "skip_reason": None,
        })

    if unique_output_path:
        unique_output_path.parent.mkdir(parents=True, exist_ok=True)
        unique_output_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_unique_rows), encoding="utf-8")
    dropped_rows: list[dict[str, Any]] = []
    for row in capped_rows:
        dropped_rows.append({
            **row,
            "sent_to_scope_review": False,
            "validation_allowed": False,
            "skip_reason": "max_validation_candidates_cap",
            "cap": max_candidates,
            "candidate_count_before_cap": len(all_unique_rows),
            "candidate_count_after_cap": len(unique_rows),
        })
    if handoff_output_path:
        handoff_output_path.parent.mkdir(parents=True, exist_ok=True)
        # This artifact is the actual validation handoff: one row per unique
        # direct HLS candidate that will proceed to stream-scope review /
        # validation.  Duplicates and cap drops are written to separate audit
        # artifacts so the handoff row count matches sent_to_validation.
        handoff_output_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in handoff_rows), encoding="utf-8")
        if dropped_rows:
            drop_path = handoff_output_path.parent / "agentic_candidates_validation_dropped.jsonl"
            drop_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in dropped_rows), encoding="utf-8")

    summary = {
        "raw": len(rows),
        "malformed_rows": malformed,
        "unique_hls": len(all_unique_rows),
        "skipped_non_hls": skipped_non_hls,
        "duplicates_removed": duplicate_count,
        "capped": capped_count,
        "sent_to_validation": len(streams),
    }
    return streams, cameras, summary

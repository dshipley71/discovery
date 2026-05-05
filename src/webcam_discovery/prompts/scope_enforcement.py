from __future__ import annotations

import json


def build_scope_inference_prompt(user_query: str, planner_plan: dict) -> str:
    return (
        "Infer the user's explicit discovery scope from the query and planner plan. "
        "Return STRICT JSON only (no markdown). "
        "Do not assume any default location/source. "
        "Do not broaden scope beyond what the user requested. "
        "If scope is insufficient, set has_sufficient_scope=false and explain why.\n\n"
        f"User query:\n{user_query}\n\n"
        f"Planner plan JSON:\n{json.dumps(planner_plan, ensure_ascii=False)}\n\n"
        "Return keys: has_sufficient_scope, scope_type, scope_label, scope_summary, "
        "normalized_targets, target_aliases, included_locations, excluded_locations, "
        "included_sources, excluded_sources, agency_or_owner, coordinates, hostnames, "
        "ip_addresses, camera_types, confidence, insufficient_scope_reason, user_message."
    )


def build_search_result_scope_prompt(page: dict, scope: dict) -> str:
    return (
        "Decide whether this search result page is within the inferred scope. "
        "Return STRICT JSON only with keys: decision, confidence, reason, matched_scope_terms, "
        "missing_evidence, risk_flags. decision must be accept/reject/review. "
        "Do not accept only because it has generic camera terms. "
        "Reject when only evidence is the original query text.\n\n"
        f"Scope JSON:\n{json.dumps(scope, ensure_ascii=False)}\n\n"
        f"Page candidate JSON:\n{json.dumps(page, ensure_ascii=False)}"
    )


def build_stream_scope_prompt(candidate: dict, scope: dict) -> str:
    return (
        "Decide whether this stream/camera candidate is within scope. "
        "Return STRICT JSON only with keys: decision, confidence, reason, matched_scope_terms, "
        "missing_evidence, risk_flags. decision must be accept/reject/review. "
        "Do not accept only because URL is .m3u8 or valid HLS. "
        "Require concrete evidence linking to scope.\n\n"
        f"Scope JSON:\n{json.dumps(scope, ensure_ascii=False)}\n\n"
        f"Candidate JSON:\n{json.dumps(candidate, ensure_ascii=False)}"
    )

from __future__ import annotations

import json


def build_scope_inference_prompt(user_query: str, planner_plan: dict) -> str:
    return (
        "Infer the user's explicit discovery scope from the query and planner plan.\n"
        "Return STRICT JSON only (no markdown, no comments, no prose).\n"
        "Use arrays for multi-value fields.\n"
        "Use agency_or_owners (array). Do not use agency_or_owner.\n"
        "Do not assume any default location/source.\n"
        "Do not broaden scope beyond what the user requested.\n"
        "Do not hardcode preferred city/state/country/agency/source.\n"
        "If the query lacks specific searchable scope, set has_sufficient_scope=false.\n\n"
        f"User query:\n{user_query}\n\n"
        f"Planner plan JSON:\n{json.dumps(planner_plan, ensure_ascii=False)}\n\n"
        "Schema keys (exact): has_sufficient_scope, scope_type, scope_label, scope_summary, "
        "normalized_targets, target_aliases, included_locations, excluded_locations, "
        "included_sources, excluded_sources, agency_or_owners, coordinates, hostnames, "
        "ip_addresses, camera_types, confidence, insufficient_scope_reason, user_message.\n\n"
        "Example (sufficient):\n"
        "{"
        "\"has_sufficient_scope\": true, \"scope_type\": \"place\", \"scope_label\": \"Lubbock, Texas\", "
        "\"scope_summary\": \"The user is asking for public live HLS cameras near Lubbock, Texas.\", "
        "\"normalized_targets\": [\"Lubbock, Texas\"], \"target_aliases\": [], "
        "\"included_locations\": [\"Lubbock, Texas\"], \"excluded_locations\": [], "
        "\"included_sources\": [], \"excluded_sources\": [], \"agency_or_owners\": [], "
        "\"coordinates\": [], \"hostnames\": [], \"ip_addresses\": [], "
        "\"camera_types\": [\"public live HLS cameras\"], \"confidence\": 0.95, "
        "\"insufficient_scope_reason\": null, \"user_message\": null"
        "}\n\n"
        "Example (insufficient):\n"
        "{"
        "\"has_sufficient_scope\": false, \"scope_type\": null, \"scope_label\": null, "
        "\"scope_summary\": null, \"normalized_targets\": [], \"target_aliases\": [], "
        "\"included_locations\": [], \"excluded_locations\": [], \"included_sources\": [], "
        "\"excluded_sources\": [], \"agency_or_owners\": [], \"coordinates\": [], "
        "\"hostnames\": [], \"ip_addresses\": [], \"camera_types\": [\"public live HLS cameras\"], "
        "\"confidence\": 0.9, \"insufficient_scope_reason\": \"The query provides a camera type but no specific place, landmark, coordinates, IP address, hostname, agency, public website, or other searchable source indicator.\", "
        "\"user_message\": \"Please provide a specific place, landmark, coordinates, IP address, hostname, agency, public website, or other searchable source indicator.\""
        "}"
    )


def build_search_result_scope_prompt(page: dict, scope: dict) -> str:
    return (
        "Decide whether this search result page is within the inferred scope. "
        "Return STRICT JSON only with keys: decision, confidence, reason, matched_scope_terms, "
        "missing_evidence, risk_flags. decision must be accept/reject/review. "
        "Do not use null for list fields; use [] when empty. "
        "matched_scope_terms, missing_evidence, and risk_flags must each be arrays of strings. "
        "confidence must be a number between 0.0 and 1.0. reason must be a string. "
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
        "Do not use null for list fields; use [] when empty. "
        "matched_scope_terms, missing_evidence, and risk_flags must each be arrays of strings. "
        "confidence must be a number between 0.0 and 1.0. reason must be a string. "
        "Do not accept only because URL is .m3u8 or valid HLS. "
        "Require concrete evidence linking to scope.\n\n"
        f"Scope JSON:\n{json.dumps(scope, ensure_ascii=False)}\n\n"
        f"Candidate JSON:\n{json.dumps(candidate, ensure_ascii=False)}"
    )

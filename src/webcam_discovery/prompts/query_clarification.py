from __future__ import annotations

import json


def build_query_clarification_prompt(user_query: str, planner_plan: dict) -> str:
    return (
        "Decide whether the user's camera discovery query needs one-time clarification before any search, feed discovery, validation, cataloging, or map generation.\n"
        "Return STRICT JSON only (no markdown, no prose).\n"
        "The system is location-agnostic and public-only. Do not assume a default city, state, country, agency, source, or camera URL pattern.\n"
        "Ask clarification only when the query is ambiguous/conflicting or lacks a specific searchable place/location/landmark/coordinates/IP/hostname/agency/public website.\n"
        "Examples that need clarification: 'Get me all traffic cameras from Paris' because Paris may mean Paris, France, Paris, Texas, or another Paris; 'Get me all traffic camera' because no place/source is provided.\n"
        "Examples that usually do not need clarification: 'Get me public live HLS traffic cameras from Paris, France'; 'Find public HLS cameras near Lubbock, Texas'; 'Find cameras for the Statue of Liberty'.\n"
        "If clarification is needed, provide 1 to 3 concise questions in the questions array. Ask only once.\n"
        "If the query is underspecified and no answer is provided later, the normal scope enforcement rules must stop discovery.\n"
        "If the query is sufficient, set needs_clarification=false and provide adjusted_query equal to the original query or a minimally normalized equivalent.\n\n"
        "Required JSON keys: needs_clarification, clarification_type, reason, questions, candidate_interpretations, adjusted_query, confidence.\n"
        "clarification_type must be one of: ambiguous_place, insufficient_scope, conflicting_scope, none.\n"
        "questions must be an array of strings with length 0 when no clarification is needed, otherwise length 1-3.\n"
        "candidate_interpretations must be an array of strings.\n"
        "confidence must be a number between 0.0 and 1.0.\n\n"
        f"User query:\n{user_query}\n\n"
        f"Planner plan JSON:\n{json.dumps(planner_plan, ensure_ascii=False)}\n\n"
        "Example ambiguous-place response:\n"
        "{\"needs_clarification\": true, \"clarification_type\": \"ambiguous_place\", \"reason\": \"Paris is ambiguous without a state/country.\", \"questions\": [\"Which Paris do you mean, such as Paris, France or Paris, Texas?\"], \"candidate_interpretations\": [\"Paris, France\", \"Paris, Texas\"], \"adjusted_query\": null, \"confidence\": 0.94}\n\n"
        "Example insufficient response:\n"
        "{\"needs_clarification\": true, \"clarification_type\": \"insufficient_scope\", \"reason\": \"The query requests traffic cameras but gives no place, source, or other searchable location indicator.\", \"questions\": [\"What place, location, landmark, agency, or public website should I search for traffic cameras?\"], \"candidate_interpretations\": [], \"adjusted_query\": null, \"confidence\": 0.96}\n\n"
        "Example sufficient response:\n"
        "{\"needs_clarification\": false, \"clarification_type\": \"none\", \"reason\": \"The query names Paris, France explicitly.\", \"questions\": [], \"candidate_interpretations\": [\"Paris, France\"], \"adjusted_query\": \"Get me public live HLS traffic cameras from Paris, France\", \"confidence\": 0.91}"
    )

from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qsl, unquote

TECHNICAL_TOKENS = {
    "playlist","stream","hls","m3u8","live","camera","cam","video","index","master","manifest","chunk","segment","cdn","play","player"
}


class URLMetadataExtractor:
    def extract(self, url: str, context: dict | None = None) -> dict:
        context = context or {}
        p = urlparse(url)
        raw_segments = [s for s in p.path.split("/") if s]
        raw_tokens = []
        raw_tokens.extend(raw_segments)
        raw_tokens.extend([k for k, _ in parse_qsl(p.query, keep_blank_values=True)])
        raw_tokens.extend([v for _, v in parse_qsl(p.query, keep_blank_values=True)])
        if p.fragment:
            raw_tokens.append(p.fragment)

        cleaned = []
        non_location = []
        for tok in raw_tokens:
            decoded = unquote(tok)
            pieces = self._split_token(decoded)
            for piece in pieces:
                low = piece.lower().strip()
                if not low:
                    continue
                if low in TECHNICAL_TOKENS:
                    non_location.append(low)
                    continue
                cleaned.append(low)

        phrases = self._candidate_phrases(cleaned)
        candidates = []
        for phrase in phrases[:10]:
            conf = "high" if len(phrase.split()) >= 2 else "medium"
            candidates.append({
                "text": phrase,
                "confidence": conf,
                "evidence_source": "url_path",
                "evidence": [phrase],
                "notes": "Derived from normalized URL tokens",
            })

        return {
            "has_location_hint": bool(candidates),
            "location_text_candidates": candidates,
            "label_hint": context.get("label") or " ".join(phrases[:1]) if phrases else None,
            "non_location_tokens": sorted(set(non_location)),
            "should_attempt_geocode": bool(candidates),
            "raw_url": url,
            "host": p.netloc,
            "raw_path_segments": raw_segments,
            "cleaned_tokens": cleaned,
            "candidate_phrases": phrases,
            "context": context,
        }

    def _split_token(self, token: str) -> list[str]:
        token = re.sub(r"([a-z])([A-Z])", r"\1 \2", token)
        token = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", token)
        token = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", token)
        token = re.sub(r"[_\-./]+", " ", token)
        return re.findall(r"[A-Za-z0-9]+", token)

    def _candidate_phrases(self, tokens: list[str]) -> list[str]:
        out = []
        for i in range(len(tokens)):
            if len(tokens[i]) < 3:
                continue
            out.append(tokens[i].title())
            if i + 1 < len(tokens) and len(tokens[i + 1]) >= 3:
                out.append(f"{tokens[i].title()} {tokens[i+1].title()}")
        seen = set()
        uniq = []
        for x in out:
            if x.lower() in seen:
                continue
            seen.add(x.lower())
            uniq.append(x)
        return uniq

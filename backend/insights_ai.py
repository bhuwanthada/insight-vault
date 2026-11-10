"""AI-suggested insights built from highlighted evidence.

Probabilistic part: the LLM proposes insights. Deterministic part: every suggestion is validated
(evidence ids must exist, titles must be non-empty) and a tag-grouping baseline is used when the
LLM is unavailable or returns nothing usable. Results are cached and concurrent identical
requests are collapsed into one LLM call.
"""
import hashlib
import json
from collections import defaultdict

from .llm import LLMUnavailable

PROMPT_VERSION = "insights-v1"
MAX_HIGHLIGHTS = 60
MAX_SUGGESTIONS = 8

SYSTEM_PROMPT = (
    "You are a research analyst. Group the customer quotes into distinct insights. "
    'Reply with JSON only: {"insights": [{"title": str, "description": str, "highlightIds": [str]}]}. '
    "Only use highlight ids you were given. Every insight needs at least one id."
)


def rule_suggestions(highlights):
    """Deterministic baseline: a tag that appears on 2+ quotes is a recurring theme."""
    by_tag = defaultdict(list)
    for h in highlights:
        for tag in h["tags"]:
            by_tag[tag].append(h)
    themes = sorted(((t, hs) for t, hs in by_tag.items() if len(hs) >= 2), key=lambda kv: (-len(kv[1]), kv[0]))
    return [
        {
            "title": f"Recurring theme: {tag}",
            "description": f"{len(hs)} quotes across {len({h['noteId'] for h in hs})} note(s) mention '{tag}'.",
            "highlightIds": [h["id"] for h in hs],
            "confidence": round(min(1.0, len(hs) / 5), 2),
        }
        for tag, hs in themes[:MAX_SUGGESTIONS]
    ]


def validate_llm_suggestions(raw, known_ids):
    """Drop hallucinated evidence ids and malformed items; confidence = share of ids that were real."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            continue
        ids = item.get("highlightIds")
        ids = ids if isinstance(ids, list) else []
        valid = [i for i in dict.fromkeys(ids) if i in known_ids]
        if not valid:
            continue
        out.append(
            {
                "title": str(item["title"]).strip()[:200],
                "description": str(item.get("description", "")).strip(),
                "highlightIds": valid,
                "confidence": round(len(valid) / len(set(ids)), 2),
            }
        )
    return out[:MAX_SUGGESTIONS]


class InsightSuggester:
    def __init__(self, llm, cache, flight):
        self.llm, self.cache, self.flight = llm, cache, flight

    def suggest(self, highlights):
        highlights = highlights[:MAX_HIGHLIGHTS]
        if not highlights:
            return {"method": "rules", "suggestions": []}
        payload = [{"id": h["id"], "text": h["text"][:300], "tags": h["tags"]} for h in highlights]
        key = hashlib.sha256(
            f"{PROMPT_VERSION}|{self.llm.settings.model}|{json.dumps(payload, sort_keys=True)}".encode()
        ).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        return self.flight.do(key, lambda: self._build(key, highlights, payload))

    def _build(self, key, highlights, payload):
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        try:
            data = self.llm.complete_json(SYSTEM_PROMPT, json.dumps(payload))
        except LLMUnavailable as exc:
            return {"method": "rules", "fallbackReason": str(exc), "suggestions": rule_suggestions(highlights)}
        suggestions = validate_llm_suggestions(data.get("insights"), {h["id"] for h in highlights})
        if not suggestions:
            return {
                "method": "rules",
                "fallbackReason": "LLM returned no valid insights",
                "suggestions": rule_suggestions(highlights),
            }
        result = {"method": "llm", "model": self.llm.settings.model, "suggestions": suggestions}
        self.cache.set(key, result)
        return result

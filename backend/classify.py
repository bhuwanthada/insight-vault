"""Support-ticket diagnosis: deterministic keyword rules first, LLM voting only when rules are unsure.

* Deterministic: keyword scoring decides clear-cut tickets (free, instant, reproducible).
* Probabilistic: ambiguous tickets go to the LLM several times; the majority answer wins and the
  agreement ratio is reported as confidence. LLM output is validated against the configured taxonomy.
* Concurrency: results are cached, and identical in-flight requests are collapsed (single-flight).
* If the LLM is unavailable the rule result is returned and flagged as low confidence.

The taxonomy and the fix-action patterns come from the rules file (config/rules.yaml by default).
"""
import hashlib
from collections import Counter

from .llm import LLMUnavailable

PROMPT_VERSION = "classify-v2"


def ticket_text(ticket):
    return " ".join(str(ticket.get(k, "")) for k in ("subject", "description", "resolution")).lower()


def rule_scores(text, rules):
    return {name: sum(len(p.findall(text)) for p in patterns) for name, patterns in rules.categories.items()}


def extract_fixes(ticket, rules):
    """Deterministically pull out what the agent did (actions) and what they used (tools)."""
    resolution = str(ticket.get("resolution", "")).lower()
    actions = [label for label, pattern in rules.actions if pattern.search(resolution)]
    tools = sorted({str(t).strip().lower() for t in ticket.get("tools_used", []) if str(t).strip()})
    return {"actions": actions, "tools": tools}


class Classifier:
    def __init__(self, llm, cache, flight, settings, rules, prompts):
        self.llm, self.cache, self.flight = llm, cache, flight
        self.settings, self.rules, self.prompts = settings, rules, prompts

    def classify(self, ticket):
        text = ticket_text(ticket)
        ranked = sorted(rule_scores(text, self.rules).items(), key=lambda kv: (-kv[1], kv[0]))
        top_cat, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0  # a rules file may define a single category
        if top >= self.settings.rule_min_hits and top - second >= self.settings.rule_min_margin:
            return {"category": top_cat, "confidence": round((top - second) / top, 2), "method": "rules"}

        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.llm.fingerprint('classify')}|{text}".encode()).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            return dict(cached)
        result = self.flight.do(key, lambda: self._resolve_ambiguous(key, text, top_cat, top, second))
        return dict(result)

    def _resolve_ambiguous(self, key, text, top_cat, top, second):
        cached = self.cache.get(key)  # a leader that just finished may have filled it
        if cached is not None:
            return cached
        ballots = self._vote(text)
        if ballots:
            winner, count = Counter(ballots).most_common(1)[0]
            result = {"category": winner, "confidence": round(count / len(ballots), 2), "method": "llm-vote"}
            self.cache.set(key, result)  # only trustworthy results are cached; fallbacks may recover later
            return result
        if top > 0:
            return {"category": top_cat, "confidence": round((top - second) / top, 2), "method": "rules-low-confidence"}
        return {"category": self.rules.uncategorised, "confidence": 0.0, "method": "unclassified"}

    def _vote(self, text):
        system = self.prompts.get("classify", categories="; ".join(self.rules.names))
        ballots = []
        for _ in range(self.settings.votes):
            try:
                data = self.llm.complete_json("classify", system, text[:2000], temperature=self.settings.vote_temperature)
            except LLMUnavailable:
                break
            if data.get("category") in self.rules.categories:
                ballots.append(data["category"])
        return ballots

"""Support-ticket diagnosis: deterministic keyword rules first, LLM voting only when rules are unsure.

* Deterministic: keyword scoring decides clear-cut tickets (free, instant, reproducible).
* Probabilistic: ambiguous tickets go to the LLM several times; the majority answer wins and the
  agreement ratio is reported as confidence. LLM output is validated against the fixed taxonomy.
* Concurrency: results are cached, and identical in-flight requests are collapsed (single-flight).
* If the LLM is unavailable the rule result is returned and flagged as low confidence.
"""
import hashlib
import re
from collections import Counter

from .llm import LLMUnavailable

PROMPT_VERSION = "classify-v1"
UNCATEGORISED = "Uncategorised"

TAXONOMY = {
    "Login & access": ["login", "log in", "password", "locked out", "sso", "2fa", "mfa", "authentication", "access denied", "permission"],
    "Performance": ["slow", "latency", "timeout", "timed out", "lag", "loading", "performance", "hang"],
    "Billing & payments": ["invoice", "billing", "refund", "charge", "payment", "subscription", "credit card"],
    "Data sync & import": ["sync", "import", "export", "csv", "missing data", "duplicate", "migration"],
    "Crashes & errors": ["crash", "error", "exception", "500", "freeze", "blank screen", "stack trace"],
    "Integrations & API": ["api", "webhook", "integration", "token", "rate limit", "oauth"],
    "Configuration & setup": ["setup", "configure", "configuration", "install", "onboarding", "settings"],
    "Notifications & email": ["email", "notification", "notify", "spam", "not receiving", "sms"],
}
_PATTERNS = {cat: [re.compile(r"\b" + re.escape(k)) for k in words] for cat, words in TAXONOMY.items()}

ACTION_PATTERNS = [
    ("Restarted service", re.compile(r"\brestart")),
    ("Cleared cache", re.compile(r"\bcleared? (the )?cache|\bcache (was )?(cleared|purged)")),
    ("Reset password / credentials", re.compile(r"\breset (the |their |his |her )?(password|credentials|2fa|mfa)")),
    ("Rolled back release", re.compile(r"\broll(ed)? ?back")),
    ("Reinstalled application", re.compile(r"\breinstall")),
    ("Applied patch / hotfix", re.compile(r"\b(patch|hotfix)")),
    ("Re-synced data", re.compile(r"\bre-?sync")),
    ("Updated configuration", re.compile(r"\b(updated?|changed?|corrected?) (the )?(config|configuration|settings)")),
    ("Escalated to engineering", re.compile(r"\bescalat")),
    ("Issued refund / credit", re.compile(r"\b(refund|credited|credit note)")),
    ("Regenerated API key / token", re.compile(r"\b(regenerat|re-?issu|rotat)\w* (the |a |new )?(api )?(key|token)")),
    ("Allow-listed IP / domain", re.compile(r"\ballow-?list|\bwhitelist")),
]

SYSTEM_PROMPT = (
    "You classify customer-support tickets. Reply with JSON only: "
    '{"category": "<one of the allowed categories>"}. Allowed categories: ' + "; ".join(TAXONOMY) + "."
)


def ticket_text(ticket):
    return " ".join(str(ticket.get(k, "")) for k in ("subject", "description", "resolution")).lower()


def rule_scores(text):
    return {cat: sum(len(p.findall(text)) for p in patterns) for cat, patterns in _PATTERNS.items()}


def extract_fixes(ticket):
    """Deterministically pull out what the agent did (actions) and what they used (tools)."""
    resolution = str(ticket.get("resolution", "")).lower()
    actions = [label for label, pattern in ACTION_PATTERNS if pattern.search(resolution)]
    tools = sorted({str(t).strip().lower() for t in ticket.get("tools_used", []) if str(t).strip()})
    return {"actions": actions, "tools": tools}


class Classifier:
    def __init__(self, llm, cache, flight, settings):
        self.llm, self.cache, self.flight, self.settings = llm, cache, flight, settings

    def classify(self, ticket):
        text = ticket_text(ticket)
        ranked = sorted(rule_scores(text).items(), key=lambda kv: (-kv[1], kv[0]))
        (top_cat, top), (_, second) = ranked[0], ranked[1]
        if top >= 2 and top - second >= 2:
            return {"category": top_cat, "confidence": round((top - second) / top, 2), "method": "rules"}

        key = hashlib.sha256(f"{PROMPT_VERSION}|{self.llm.settings.model}|{text}".encode()).hexdigest()
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
        return {"category": UNCATEGORISED, "confidence": 0.0, "method": "unclassified"}

    def _vote(self, text):
        ballots = []
        for _ in range(self.settings.votes):
            try:
                data = self.llm.complete_json(SYSTEM_PROMPT, text[:2000], temperature=self.settings.vote_temperature)
            except LLMUnavailable:
                break
            if data.get("category") in TAXONOMY:
                ballots.append(data["category"])
        return ballots

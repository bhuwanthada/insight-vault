"""LLM access through LiteLLM (works with OpenRouter and any other LiteLLM provider).

Every call is bounded (timeout, concurrency limit), retried with exponential backoff + jitter,
and guarded by a circuit breaker. Callers get `LLMUnavailable` and fall back to deterministic logic.
"""
import json
import logging
import random
import re
import threading
import time

from .resilience import CircuitBreaker

log = logging.getLogger("insight_vault.llm")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMUnavailable(Exception):
    """The LLM is not configured, failing, or the circuit is open."""


def parse_json_object(text):
    data = json.loads(_FENCE.sub("", text.strip()))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def litellm_completion(**kwargs):
    import litellm  # imported lazily so the app starts (and tests run) without network or keys

    return litellm.completion(**kwargs)


class LLMClient:
    def __init__(self, settings, completion=litellm_completion, sleep=time.sleep, rng=random.random, breaker=None):
        self.settings = settings
        self._completion = completion
        self._sleep = sleep
        self._rng = rng
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self.breaker = breaker or CircuitBreaker()

    @property
    def enabled(self):
        return bool(self.settings.model)

    def complete_json(self, system, user, *, model=None, temperature=0.0):
        chosen = model or self.settings.model
        if not chosen:
            raise LLMUnavailable("no LLM configured (set LLM_MODEL)")
        if not self.breaker.allow():
            raise LLMUnavailable("LLM circuit is open; retrying later")
        request = {
            "model": chosen,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
            "timeout": self.settings.timeout,
        }
        if self.settings.api_key:
            request["api_key"] = self.settings.api_key
        last = None
        for attempt in range(self.settings.max_retries + 1):
            try:
                with self._slots:
                    response = self._completion(**request)
                data = parse_json_object(response.choices[0].message.content)
            except Exception as exc:
                last = exc
                log.warning("LLM attempt %d failed: %s", attempt + 1, exc)
                if attempt < self.settings.max_retries:
                    self._sleep(min(8.0, 0.5 * 2**attempt) * (0.5 + self._rng()))
            else:
                self.breaker.record_success()
                return data
        self.breaker.record_failure()
        raise LLMUnavailable(f"LLM call failed: {last}") from last

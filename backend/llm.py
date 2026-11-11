"""LLM access with pluggable providers.

Providers (LLM_PROVIDER): ``litellm`` (any model LiteLLM supports), ``openrouter`` (direct HTTP),
``openai_compatible`` (any base URL, e.g. Ollama or vLLM) and ``none``.

Every call is bounded (timeout, concurrency limit, token budget), retried with exponential backoff and
jitter, and guarded by one circuit breaker per model. Models are tried in order (primary, then
LLM_FALLBACKS); callers get ``LLMUnavailable`` only when all of them fail, and fall back to rules.
"""
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .privacy import mask_pii
from .resilience import CircuitBreaker

log = logging.getLogger("insight_vault.llm")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
OPENROUTER_URL = "https://openrouter.ai/api/v1"


class LLMUnavailable(Exception):
    """The LLM is not configured, failing, or over budget."""


@dataclass(frozen=True)
class Target:
    provider: str
    model: str

    @property
    def label(self):
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Completion:
    text: str
    tokens: int


def parse_json_object(text):
    data = json.loads(_FENCE.sub("", text.strip()))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


# ---------------------------------------------------------------- providers
def litellm_completion(**kwargs):
    import litellm  # imported lazily so the app starts (and tests run) without network or keys

    return litellm.completion(**kwargs)


class LiteLLMProvider:
    def __init__(self, settings, completion=litellm_completion):
        self.settings, self._completion = settings, completion

    def complete(self, target, messages, temperature, max_tokens, timeout):
        request = {"model": target.model, "messages": messages, "temperature": temperature,
                   "max_tokens": max_tokens, "timeout": timeout}
        if self.settings.llm_api_key:
            request["api_key"] = self.settings.llm_api_key
        if self.settings.llm_base_url:
            request["api_base"] = self.settings.llm_base_url
        response = self._completion(**request)
        usage = getattr(response, "usage", None)
        return Completion(response.choices[0].message.content, int(getattr(usage, "total_tokens", 0) or 0))


class HTTPProvider:
    """OpenAI-style /chat/completions over plain HTTP (OpenRouter and any compatible server)."""

    def __init__(self, settings, base_url, extra_headers=None, opener=urllib.request.urlopen):
        self.settings, self.base_url, self.extra_headers, self._open = settings, base_url.rstrip("/"), extra_headers or {}, opener

    def complete(self, target, messages, temperature, max_tokens, timeout):
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        body = json.dumps({"model": target.model, "messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens}).encode()
        request = urllib.request.Request(self.base_url + "/chat/completions", data=body, headers=headers, method="POST")
        try:
            with self._open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code} from {self.base_url}") from exc
        usage = payload.get("usage") or {}
        return Completion(payload["choices"][0]["message"]["content"], int(usage.get("total_tokens", 0) or 0))


def build_provider(name, settings):
    if name == "litellm":
        return LiteLLMProvider(settings)
    if name == "openrouter":
        headers = {"X-Title": settings.openrouter_title}
        if settings.openrouter_referer:
            headers["HTTP-Referer"] = settings.openrouter_referer
        return HTTPProvider(settings, settings.llm_base_url or OPENROUTER_URL, headers)
    return HTTPProvider(settings, settings.llm_base_url)  # openai_compatible


# ---------------------------------------------------------------- token budget
class UsageTracker:
    """Daily token counter persisted in the data store, so the cap survives restarts."""

    def __init__(self, store, cap, clock=lambda: datetime.now(timezone.utc)):
        self.store, self.cap, self._clock = store, cap, clock

    def _today(self):
        return self._clock().date().isoformat()

    def snapshot(self):
        day = self.store.load()["usage"].get(self._today(), {})
        return {"date": self._today(), "tokens": day.get("tokens", 0), "calls": day.get("calls", 0), "cap": self.cap}

    def exhausted(self):
        return bool(self.cap) and self.snapshot()["tokens"] >= self.cap

    def record(self, tokens):
        with self.store.lock:
            db = self.store.load()
            day = db["usage"].setdefault(self._today(), {"tokens": 0, "calls": 0})
            day["tokens"] += tokens
            day["calls"] += 1
            for old in sorted(db["usage"])[:-30]:  # keep a month of history
                del db["usage"][old]
            self.store.save(db)


# ---------------------------------------------------------------- client
class LLMClient:
    def __init__(self, settings, providers=None, usage=None, sleep=time.sleep, rng=random.random):
        self.settings = settings
        self._providers = providers or {}
        self._usage = usage
        self._sleep, self._rng = sleep, rng
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self._breakers, self._lock = {}, threading.Lock()
        self.stats = {"calls": 0, "failures": 0, "tokens": 0}

    @property
    def enabled(self):
        return self.settings.llm_enabled

    def targets(self, task):
        """Models to try for a task, in order: the task's model (or the default), then the fallbacks."""
        if not self.enabled:
            return []
        s = self.settings
        primary = getattr(s, f"llm_model_{task}", "") or s.llm_model
        chain = [Target(s.llm_provider, primary)]
        for entry in s.llm_fallbacks:
            provider, _, model = entry.partition(":")
            chain.append(Target(provider, model))
        return list(dict.fromkeys(chain))

    def fingerprint(self, task):
        chain = self.targets(task)
        return chain[0].label if chain else ""

    def circuits(self):
        with self._lock:
            return {label: not breaker.allow() for label, breaker in self._breakers.items()}

    def _breaker(self, label):
        with self._lock:
            if label not in self._breakers:
                self._breakers[label] = CircuitBreaker(self.settings.breaker_threshold, self.settings.breaker_reset)
            return self._breakers[label]

    def _provider(self, name):
        if name not in self._providers:
            self._providers[name] = build_provider(name, self.settings)
        return self._providers[name]

    def _attempt(self, target, messages, temperature, retries):
        """Call one target with retries. Returns parsed JSON, or raises the last error."""
        last = None
        for attempt in range(retries + 1):
            try:
                with self._slots:
                    done = self._provider(target.provider).complete(
                        target, messages, temperature, self.settings.max_tokens, self.settings.timeout)
                data = parse_json_object(done.text)
            except Exception as exc:
                last = exc
                log.warning("LLM %s attempt %d failed: %s", target.label, attempt + 1, exc)
                if attempt < retries:
                    self._sleep(min(8.0, 0.5 * 2**attempt) * (0.5 + self._rng()))
            else:
                self.stats["tokens"] += done.tokens
                if self._usage:
                    self._usage.record(done.tokens)
                return data
        raise last

    def complete_json(self, task, system, user, *, temperature=None):
        chain = self.targets(task)
        if not chain:
            raise LLMUnavailable("no LLM configured (set LLM_PROVIDER and LLM_MODEL)")
        if self._usage and self._usage.exhausted():
            raise LLMUnavailable(f"daily token cap of {self.settings.daily_token_cap} reached")
        if self.settings.pii_masking:
            user = mask_pii(user)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        temperature = self.settings.temperature if temperature is None else temperature
        errors = []
        for target in chain:
            breaker = self._breaker(target.label)
            if not breaker.allow():
                errors.append(f"{target.label}: circuit open")
                continue
            try:
                data = self._attempt(target, messages, temperature, self.settings.max_retries)
            except Exception as exc:
                breaker.record_failure()
                self.stats["failures"] += 1
                errors.append(f"{target.label}: {exc}")
                continue
            breaker.record_success()
            self.stats["calls"] += 1
            return data
        raise LLMUnavailable("; ".join(errors))

    def ping(self):
        """One cheap call to the primary model, no retries. Used by the admin 'test connection' button."""
        chain = self.targets("default")
        if not chain:
            return {"ok": False, "error": "no LLM configured (set LLM_PROVIDER and LLM_MODEL)"}
        target, started = chain[0], time.monotonic()
        messages = [{"role": "system", "content": 'Reply with JSON only: {"ok": true}'}, {"role": "user", "content": "ping"}]
        try:
            self._attempt(target, messages, 0.0, 0)
        except Exception as exc:
            return {"ok": False, "provider": target.provider, "model": target.model, "error": str(exc)}
        return {"ok": True, "provider": target.provider, "model": target.model,
                "latencyMs": round((time.monotonic() - started) * 1000)}

import io
import json
import sys
import threading
import types
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from backend import llm as llm_module
from backend.config import Settings
from backend.llm import (Completion, HTTPProvider, LiteLLMProvider, LLMClient, LLMUnavailable, Target, UsageTracker,
                         build_provider, parse_json_object)
from backend.store import Store
from tests.conftest import completion_returning


def test_parse_json_object_handles_code_fences_and_rejects_non_objects():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('{"a": 2}') == {"a": 2}
    for bad in ("[1, 2]", "not json"):
        with pytest.raises(ValueError):
            parse_json_object(bad)


def test_target_label():
    assert Target("openrouter", "a/b").label == "openrouter:a/b"


# ---------- providers ----------
def test_litellm_provider_passes_options_and_reads_usage():
    seen = {}

    def completion(**kw):
        seen.update(kw)
        return completion_returning('{"ok": 1}', tokens=11)(**kw)

    s = Settings(llm_api_key="key", llm_base_url="http://gateway/v1")
    done = LiteLLMProvider(s, completion).complete(Target("litellm", "m"), [{"role": "user", "content": "x"}], 0.3, 99, 7)
    assert done == Completion('{"ok": 1}', 11)
    assert (seen["model"], seen["temperature"], seen["max_tokens"], seen["timeout"]) == ("m", 0.3, 99, 7)
    assert seen["api_key"] == "key" and seen["api_base"] == "http://gateway/v1"


def test_litellm_provider_omits_unset_options_and_tolerates_missing_usage():
    seen = {}

    def completion(**kw):
        seen.update(kw)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="{}"))])

    done = LiteLLMProvider(Settings(), completion).complete(Target("litellm", "m"), [], 0, 1, 1)
    assert done.tokens == 0 and "api_key" not in seen and "api_base" not in seen


def test_default_litellm_completion_imports_lazily(monkeypatch):
    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=lambda **kw: ("called", kw)))
    assert llm_module.litellm_completion(model="m")[0] == "called"


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_provider(payload=None, error=None, **kw):
    captured = {}

    def opener(request, timeout):
        captured.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data), timeout=timeout)
        if error:
            raise error
        return FakeHTTPResponse(json.dumps(payload).encode())

    return HTTPProvider(Settings(llm_api_key="sk-1"), "http://host/v1/", opener=opener, **kw), captured


def test_http_provider_posts_chat_completions():
    payload = {"choices": [{"message": {"content": '{"a": 1}'}}], "usage": {"total_tokens": 9}}
    provider, seen = http_provider(payload, extra_headers={"X-Title": "T"})
    done = provider.complete(Target("openrouter", "m"), [{"role": "user", "content": "hi"}], 0.1, 50, 3)
    assert done == Completion('{"a": 1}', 9)
    assert seen["url"] == "http://host/v1/chat/completions" and seen["timeout"] == 3
    assert seen["headers"]["Authorization"] == "Bearer sk-1" and seen["headers"]["X-title"] == "T"
    assert seen["body"] == {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.1, "max_tokens": 50}


def test_http_provider_without_key_or_usage_and_with_http_errors():
    keyless = HTTPProvider(Settings(), "http://h", opener=lambda r, timeout: FakeHTTPResponse(b'{"choices":[{"message":{"content":"{}"}}]}'))
    assert keyless.complete(Target("openai_compatible", "m"), [], 0, 1, 1) == Completion("{}", 0)
    provider, _ = http_provider(error=urllib.error.HTTPError("http://h", 429, "slow down", {}, None))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        provider.complete(Target("openrouter", "m"), [], 0, 1, 1)


def test_build_provider_variants():
    assert isinstance(build_provider("litellm", Settings()), LiteLLMProvider)
    plain = build_provider("openrouter", Settings(openrouter_title="Vault"))
    assert plain.base_url == "https://openrouter.ai/api/v1" and plain.extra_headers == {"X-Title": "Vault"}
    custom = build_provider("openrouter", Settings(llm_base_url="http://proxy/v1", openrouter_referer="https://me"))
    assert custom.base_url == "http://proxy/v1" and custom.extra_headers["HTTP-Referer"] == "https://me"
    compat = build_provider("openai_compatible", Settings(llm_base_url="http://localhost:11434/v1"))
    assert compat.base_url == "http://localhost:11434/v1" and compat.extra_headers == {}


# ---------- usage tracker ----------
def test_usage_tracker_counts_caps_and_trims_history(tmp_path):
    day = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    usage = UsageTracker(Store(str(tmp_path / "u.json")), cap=10, clock=lambda: day["now"])
    assert usage.snapshot() == {"date": "2026-01-01", "tokens": 0, "calls": 0, "cap": 10} and not usage.exhausted()
    usage.record(6)
    usage.record(4)
    assert usage.snapshot()["tokens"] == 10 and usage.snapshot()["calls"] == 2 and usage.exhausted()
    day["now"] = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert not usage.exhausted()  # a new day starts fresh
    for i in range(40):
        day["now"] = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=10 + i)
        usage.record(1)
    assert len(usage.store.load()["usage"]) <= 30
    assert not UsageTracker(usage.store, cap=0).exhausted()  # 0 means unlimited


# ---------- client ----------
class FakeProvider:
    def __init__(self, *outcomes, tokens=5):
        self.outcomes, self.tokens, self.calls = list(outcomes), tokens, []

    def complete(self, target, messages, temperature, max_tokens, timeout):
        self.calls.append((target, messages, temperature))
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, Exception):
            raise out
        return Completion(out, self.tokens)


def client(settings=None, providers=None, usage=None, **kw):
    sleeps = []
    s = settings or Settings(llm_provider="litellm", llm_model="primary", max_retries=2)
    return LLMClient(s, providers=providers, usage=usage, sleep=sleeps.append, rng=lambda: 0.5, **kw), sleeps


def test_targets_per_task_with_fallbacks_and_dedupe():
    s = Settings(llm_provider="openrouter", llm_model="big", llm_model_classify="cheap",
                 llm_fallbacks=("litellm:backup", "openrouter:big"))
    c = LLMClient(s)
    assert [t.label for t in c.targets("classify")] == ["openrouter:cheap", "litellm:backup", "openrouter:big"]
    assert [t.label for t in c.targets("insights")] == ["openrouter:big", "litellm:backup"]  # deduped, falls back to default model
    assert c.fingerprint("classify") == "openrouter:cheap"
    assert LLMClient(Settings()).targets("x") == [] and LLMClient(Settings()).fingerprint("x") == "" and not LLMClient(Settings()).enabled


def test_disabled_client_refuses_calls():
    with pytest.raises(LLMUnavailable, match="no LLM configured"):
        LLMClient(Settings()).complete_json("classify", "s", "u")


def test_success_records_tokens_and_uses_default_temperature(tmp_path):
    provider = FakeProvider('{"v": 1}', tokens=12)
    usage = UsageTracker(Store(str(tmp_path / "u.json")), cap=0)
    c, _ = client(providers={"litellm": provider}, usage=usage)
    assert c.complete_json("classify", "sys", "usr") == {"v": 1}
    assert provider.calls[0][2] == 0.2  # settings.temperature
    assert c.stats == {"calls": 1, "failures": 0, "tokens": 12} and usage.snapshot()["tokens"] == 12
    c.complete_json("classify", "sys", "usr", temperature=0.9)
    assert provider.calls[1][2] == 0.9 and not c.circuits()["litellm:primary"]


def test_retries_with_backoff_then_succeeds():
    provider = FakeProvider(RuntimeError("net"), "not json", '{"v": 1}')
    c, sleeps = client(providers={"litellm": provider})
    assert c.complete_json("t", "s", "u") == {"v": 1}
    assert sleeps == [0.5, 1.0]  # 0.5 * 2**attempt * (0.5 + rng 0.5)


def test_falls_back_to_the_next_model_and_reports_all_errors():
    s = Settings(llm_provider="litellm", llm_model="a", llm_fallbacks=("openrouter:b",), max_retries=0)
    providers = {"litellm": FakeProvider(RuntimeError("a is down")), "openrouter": FakeProvider('{"from": "b"}')}
    c, _ = client(s, providers)
    assert c.complete_json("t", "s", "u") == {"from": "b"} and c.stats["failures"] == 1
    providers["openrouter"].outcomes = [RuntimeError("b is down")]
    with pytest.raises(LLMUnavailable) as info:
        c.complete_json("t", "s", "u")
    assert "litellm:a: a is down" in str(info.value) and "openrouter:b: b is down" in str(info.value)


def test_circuit_breaker_skips_a_failing_model():
    s = Settings(llm_provider="litellm", llm_model="a", max_retries=0, breaker_threshold=2, breaker_reset=1000)
    provider = FakeProvider(RuntimeError("down"))
    c, _ = client(s, {"litellm": provider})
    for _ in range(2):
        with pytest.raises(LLMUnavailable):
            c.complete_json("t", "s", "u")
    with pytest.raises(LLMUnavailable, match="circuit open"):
        c.complete_json("t", "s", "u")
    assert len(provider.calls) == 2 and c.circuits() == {"litellm:a": True}


def test_daily_token_cap_blocks_calls(tmp_path):
    usage = UsageTracker(Store(str(tmp_path / "u.json")), cap=5)
    c, _ = client(Settings(llm_provider="litellm", llm_model="m", daily_token_cap=5),
                  {"litellm": FakeProvider("{}", tokens=5)}, usage)
    c.complete_json("t", "s", "u")
    with pytest.raises(LLMUnavailable, match="daily token cap of 5 reached"):
        c.complete_json("t", "s", "u")


def test_pii_is_masked_only_when_enabled():
    for enabled in (True, False):
        provider = FakeProvider("{}")
        c, _ = client(Settings(llm_provider="litellm", llm_model="m", pii_masking=enabled), {"litellm": provider})
        c.complete_json("t", "system mail@x.com", "contact jane@corp.com")
        sent = provider.calls[0][1]
        assert ("[email]" in sent[1]["content"]) is enabled
        assert "mail@x.com" in sent[0]["content"]  # the system prompt is ours, never masked


def test_builds_providers_lazily_when_none_injected(monkeypatch):
    monkeypatch.setattr(llm_module, "build_provider", lambda name, s: FakeProvider('{"built": ' + json.dumps(name) + "}"))
    c = LLMClient(Settings(llm_provider="openrouter", llm_model="m"))
    assert c.complete_json("t", "s", "u") == {"built": "openrouter"}
    c.complete_json("t", "s", "u")  # second call reuses the cached provider


def test_concurrency_limit_is_enforced():
    active, peak, lock, release = 0, 0, threading.Lock(), threading.Event()

    class Slow:
        def complete(self, *a):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            release.wait(0.2)
            with lock:
                active -= 1
            return Completion("{}", 0)

    c, _ = client(Settings(llm_provider="litellm", llm_model="m", max_concurrency=2), {"litellm": Slow()})
    threads = [threading.Thread(target=lambda: c.complete_json("t", "s", "u")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 2


def test_ping_reports_success_failure_and_missing_config():
    c, _ = client(providers={"litellm": FakeProvider("{}")})
    ok = c.ping()
    assert ok["ok"] and ok["model"] == "primary" and ok["provider"] == "litellm" and ok["latencyMs"] >= 0
    bad, _ = client(providers={"litellm": FakeProvider(RuntimeError("401 unauthorized"))})
    result = bad.ping()
    assert result["ok"] is False and "401 unauthorized" in result["error"] and result["model"] == "primary"
    assert LLMClient(Settings()).ping() == {"ok": False, "error": "no LLM configured (set LLM_PROVIDER and LLM_MODEL)"}

import io
import json
import urllib.error

import pytest

from backend import llm as llm_module
from backend.config import Settings
from backend.llm import HTTPProvider, LiteLLMProvider, LLMClient, Target, build_provider
from tests.conftest import completion_returning


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ScriptedProvider:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def complete(self, target, messages, temperature, max_tokens, timeout):
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, Exception):
            raise out
        return llm_module.Completion(out, 1)


def test_groq_provider_uses_the_builtin_endpoint_unless_overridden():
    assert build_provider("groq", Settings()).base_url == "https://api.groq.com/openai/v1"
    assert build_provider("groq", Settings(llm_base_url="http://proxy/v1")).base_url == "http://proxy/v1"


@pytest.mark.parametrize("enabled", [True, False])
def test_json_mode_is_requested_only_when_enabled(enabled):
    seen = {}
    ok = json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    def opener(request, timeout):
        seen["body"] = json.loads(request.data)
        return FakeHTTPResponse(ok)

    HTTPProvider(Settings(llm_json_mode=enabled), "http://h", opener=opener).complete(Target("groq", "m"), [], 0, 1, 1)
    assert ("response_format" in seen["body"]) is enabled
    assert not enabled or seen["body"]["response_format"] == {"type": "json_object"}

    captured = {}

    def completion(**kwargs):
        captured.update(kwargs)
        return completion_returning("{}")(**kwargs)

    LiteLLMProvider(Settings(llm_json_mode=enabled), completion).complete(Target("litellm", "m"), [], 0, 1, 1)
    assert ("response_format" in captured) is enabled


def http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return urllib.error.HTTPError("http://h", code, "x", headers, None)


@pytest.mark.parametrize("header,expected", [("7", 7.0), ("2.5", 2.5), ("soon", None), (None, None)])
def test_http_429_reports_how_long_the_provider_asked_us_to_wait(header, expected):
    provider = HTTPProvider(Settings(), "http://h", opener=lambda r, timeout: (_ for _ in ()).throw(http_error(429, header)))
    with pytest.raises(llm_module.RateLimited) as info:
        provider.complete(Target("groq", "m"), [], 0, 1, 1)
    assert info.value.retry_after == expected and "429" in str(info.value)


def test_the_client_waits_as_long_as_the_provider_asked_but_never_beyond_the_cap():
    provider = ScriptedProvider(llm_module.RateLimited("slow down", 3.0), llm_module.RateLimited("slower", 500.0), '{"ok": 1}')
    sleeps = []
    client = LLMClient(Settings(llm_provider="litellm", llm_model="m", max_retries=2), providers={"litellm": provider},
                       sleep=sleeps.append, rng=lambda: 0.5)
    assert client.complete_json("t", "s", "u") == {"ok": 1}
    assert sleeps == [3.0, llm_module.MAX_RETRY_AFTER]  # honours Retry-After, capped at 30 seconds

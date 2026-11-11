from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app import create_app
from backend.config import Settings
from backend.llm import LLMUnavailable

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class FakeLLM:
    """Scriptable stand-in for LLMClient. `script` is a callable(system, user, temperature) -> dict."""

    def __init__(self, script=None, model="fake/model"):
        self.script = script
        self.model = model
        self.calls = []
        self.stats = {"calls": 0, "failures": 0, "tokens": 0}

    @property
    def enabled(self):
        return bool(self.model)

    def fingerprint(self, task):
        return self.model

    def circuits(self):
        return {}

    def ping(self):
        return {"ok": True, "provider": "fake", "model": self.model, "latencyMs": 1}

    def complete_json(self, task, system, user, *, temperature=None):
        self.calls.append({"task": task, "system": system, "user": user, "temperature": temperature})
        if self.script is None:
            raise LLMUnavailable("scripted outage")
        return self.script(system, user, temperature)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def make_client(tmp_path):
    def build(llm=None, settings=None, clock=lambda: NOW, **kwargs):
        app = create_app(str(tmp_path / "db.json"), settings=settings or Settings(), llm=llm, clock=clock, **kwargs)
        return app.test_client()

    return build


def completion_returning(*texts, tokens=7):
    """Build a LiteLLM-shaped completion function that returns each text in turn (last one repeats)."""
    queue = list(texts)

    def completion(**kwargs):
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(total_tokens=tokens),
        )

    return completion

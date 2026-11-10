from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app import create_app
from backend.config import Settings
from backend.llm import LLMUnavailable
from backend.resilience import CircuitBreaker

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class FakeLLM:
    """Scriptable stand-in for LLMClient. `script` is a callable(system, user, temperature) -> dict."""

    def __init__(self, script=None, model="fake/model", votes=3):
        self.script = script
        self.settings = Settings(model=model, votes=votes)
        self.breaker = CircuitBreaker()
        self.calls = []

    @property
    def enabled(self):
        return bool(self.settings.model)

    def complete_json(self, system, user, *, model=None, temperature=0.0):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        if self.script is None:
            raise LLMUnavailable("scripted outage")
        return self.script(system, user, temperature)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def make_client(tmp_path):
    def build(llm=None, settings=None, clock=lambda: NOW):
        app = create_app(str(tmp_path / "db.json"), settings=settings or Settings(), llm=llm, clock=clock)
        return app.test_client()

    return build


def completion_returning(*texts):
    """Build a LiteLLM-shaped completion function that returns each text in turn (last one repeats)."""
    queue = list(texts)

    def completion(**kwargs):
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    return completion

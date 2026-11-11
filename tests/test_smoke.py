import io
import json
import runpy
import sys

import pytest

from backend import smoke
from backend.config import Settings
from backend.llm import Completion

BY_TEXT = {f"{s} {d} {r}".lower(): expected for s, d, r, expected in smoke.LABELED}
GROQ = Settings(llm_provider="groq", llm_model="test-model", votes=2, llm_api_key="gsk_SECRET_VALUE", pii_masking=True, llm_json_mode=True)


class Model:
    """A stand-in provider that answers like a competent model (or a broken one)."""

    def __init__(self, accuracy=True, valid=True, fail_ping=False, fail_summary=False, bad_insights=False):
        self.accuracy, self.valid, self.fail_ping, self.fail_summary, self.bad_insights = accuracy, valid, fail_ping, fail_summary, bad_insights

    def complete(self, target, messages, temperature, max_tokens, timeout):
        system, user = messages[0]["content"], messages[1]["content"]
        if "ok" in system and "ping" in user:
            if self.fail_ping:
                raise RuntimeError("401 invalid api key")
            return Completion('{"ok": true}', 3)
        if "classify" in system:
            if not self.valid:
                return Completion("I think it is probably a login problem", 5)
            answer = BY_TEXT.get(user, "Performance") if self.accuracy else "Notifications & email"
            return Completion(json.dumps({"category": answer}), 11)
        if "research analyst" in system:
            ids = [h["id"] for h in json.loads(user)]
            body = [] if self.bad_insights else [{"title": "Sign-in trouble", "description": "d", "highlightIds": ids[:2]}]
            return Completion(json.dumps({"insights": body}), 40)
        if "executive summaries" in system:
            if self.fail_summary:
                raise RuntimeError("model overloaded")
            return Completion('{"summary": "Most tickets were sign-in and performance problems."}', 25)
        raise AssertionError(system)


def run(model, settings=GROQ, samples=4):
    out = io.StringIO()
    report = smoke.run(settings, out, samples, providers={"groq": model})
    return report, out.getvalue()


def test_a_healthy_model_passes_every_check_and_the_key_is_never_printed():
    report, text = run(Model())
    assert report["ok"] and report["accuracy"] == 1.0 and report["validity"] == 1.0 and all(report["checks"].values())
    assert "[1] Connection: OK" in text and "4/4 correct (100%)" in text and "RESULT: all checks passed" in text
    assert "Sign-in trouble" in text and "Most tickets were sign-in" in text and "[email]" in text and "[phone]" in text
    assert "gsk_SECRET_VALUE" not in text and "API key: set" in text and "Personal-data masking: on; JSON mode: on" in text
    assert report["stats"]["tokens"] > 0 and report["stats"]["failures"] == 0


def test_a_failed_connection_stops_early_with_the_reason():
    report, text = run(Model(fail_ping=True))
    assert not report["ok"] and report["checks"] == {"connection": False} and "FAILED: " in text and "401 invalid api key" in text
    assert "[2]" not in text  # nothing else is attempted


def test_a_model_that_misclassifies_fails_and_lists_the_misses():
    report, text = run(Model(accuracy=False))
    assert not report["ok"] and report["checks"]["classification"] is False and report["accuracy"] == 0.0
    assert "missed: 'Locked out after phone upgrade' expected Login & access, got Notifications & email" in text and "classification" in text.split("RESULT:")[1]


def test_a_model_that_ignores_json_fails_the_reliability_check():
    report, text = run(Model(valid=False))
    assert report["checks"]["json_reliability"] is False and report["validity"] == 0.0 and "Valid JSON answers: 0/" in text and "got None" in text


def test_insights_and_summary_problems_are_reported():
    report, text = run(Model(bad_insights=True, fail_summary=True))
    assert report["checks"]["insights"] is False and report["checks"]["summary"] is False
    assert "Insights: FAILED" in text and "returned no valid insights" in text and "Summary: FAILED" in text


def test_a_provider_key_is_accepted_without_llm_api_key_and_flagged_when_missing():
    _, text = run(Model(), Settings(llm_provider="groq", llm_model="m", votes=1))
    assert "API key: NOT SET" in text


# ---------- command line ----------
def cli(monkeypatch, tmp_path, env, argv=()):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    for key in ("LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out = io.StringIO()
    return smoke.main(list(argv), out=out), out.getvalue()


def test_the_command_explains_how_to_configure_groq_when_nothing_is_set(monkeypatch, tmp_path):
    code, text = cli(monkeypatch, tmp_path, {})
    assert code == 2 and "LLM_PROVIDER=groq" in text and "LLM_API_KEY" in text


def test_the_command_reports_invalid_configuration(monkeypatch, tmp_path, capsys):
    code, _ = cli(monkeypatch, tmp_path, {"LLM_PROVIDER": "groq"})  # a provider without a model
    assert code == 1 and "LLM_MODEL is required" in capsys.readouterr().err


def test_the_command_runs_and_reports_the_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(smoke, "run", lambda settings, out, samples, providers=None: {"ok": samples == 3})
    code, text = cli(monkeypatch, tmp_path, {"LLM_PROVIDER": "groq", "LLM_MODEL": "m", "LLM_API_KEY": "k"}, ["--samples", "3"])
    assert code == 0 and "(took " in text
    code, _ = cli(monkeypatch, tmp_path, {"LLM_PROVIDER": "groq", "LLM_MODEL": "m", "LLM_API_KEY": "k"}, ["--samples", "99"])
    assert code == 1  # more samples than exist is clamped to the number available (10), not 3


def test_the_module_runs_as_a_program(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr(sys, "argv", ["backend.smoke"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("backend.smoke", run_name="__main__")
    assert info.value.code == 2


def test_percentiles_pick_sensible_values():
    assert smoke._percentile([5], 95) == 5 and smoke._percentile([1, 2, 3, 4, 100], 95) == 100 and smoke._percentile([1, 2, 3, 4, 100], 50) == 3

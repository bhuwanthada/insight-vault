import json
import logging

import pytest
from flask import Flask, g

from backend.config import ConfigError
from backend.limits import RateLimiter
from backend.observability import JsonFormatter, Metrics, RequestIdFilter, configure_logging
from backend.privacy import mask_pii
from backend.prompts import DEFAULTS, Prompts
from backend.rules import load_rules


# ---------- privacy ----------
def test_mask_pii_replaces_emails_cards_and_phones_but_keeps_dates_and_short_numbers():
    text = "Mail jane.doe+x@corp.co.uk or call +1 (555) 123-4567; card 4111 1111 1111 1111; on 2026-11-10 order 12345"
    out = mask_pii(text)
    assert "[email]" in out and "[phone]" in out and "[card]" in out
    assert "2026-11-10" in out and "12345" in out
    assert "jane" not in out and "4111" not in out and "555" not in out


# ---------- prompts ----------
def test_prompts_defaults_variables_and_language():
    p = Prompts(language="German")
    assert "Login" in p.get("classify", categories="Login; Billing") and "$categories" not in p.get("classify", categories="x")
    assert p.get("insights").endswith("Write all free-text fields in German.")
    assert "Write all free-text" not in p.get("classify", categories="x")  # classification output is not prose


def test_prompt_files_override_defaults(tmp_path):
    (tmp_path / "classify.txt").write_text("Custom: $categories. Unknown $thing stays.\n", encoding="utf-8")
    p = Prompts(str(tmp_path))
    assert p.get("classify", categories="A; B") == "Custom: A; B. Unknown $thing stays."
    assert p.get("summary") == Prompts().get("summary")  # missing files keep the default
    assert set(DEFAULTS) == {"classify", "insights", "summary"}


# ---------- rules ----------
def test_default_rules_load():
    rules = load_rules()
    assert "Login & access" in rules.names and len(rules.actions) == 12 and rules.uncategorised == "Uncategorised"


def test_custom_rules_file(tmp_path):
    f = tmp_path / "r.yaml"
    f.write_text('categories:\n  Alpha: [one, "two words"]\nactions:\n  - {label: Did it, pattern: "did"}\n', encoding="utf-8")
    rules = load_rules(str(f))
    assert rules.names == ["Alpha"] and rules.uncategorised == "Uncategorised" and rules.actions[0][0] == "Did it"


@pytest.mark.parametrize(
    "content,expected",
    [
        ("categories: {}\n", "'categories' must be a non-empty mapping"),
        ("- just a list\n", "'categories' must be a non-empty mapping"),
        ("categories:\n  A: []\n", "category 'A' needs a non-empty list"),
        ("categories:\n  A: [x]\nactions:\n  - {label: L, pattern: '('}\n", "bad action"),
        ("categories:\n  A: [x]\nactions:\n  - {nolabel: 1}\n", "bad action"),
    ],
)
def test_invalid_rules_are_rejected(tmp_path, content, expected):
    f = tmp_path / "r.yaml"
    f.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid rules file") as info:
        load_rules(str(f))
    assert expected in str(info.value)


def test_unreadable_rules_file(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read rules file"):
        load_rules(str(tmp_path / "missing.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Cannot read rules file"):
        load_rules(str(bad))


# ---------- rate limiter ----------
def test_rate_limiter_window_and_retry_after():
    now = {"t": 0.0}
    limiter = RateLimiter(2, window=60, clock=lambda: now["t"])
    assert limiter.allow("a") == (True, 0) and limiter.allow("a") == (True, 0)
    allowed, retry = limiter.allow("a")
    assert not allowed and retry >= 1
    assert limiter.allow("b")[0]  # keys are independent
    now["t"] = 61
    assert limiter.allow("a")[0]  # the window slid past the old hits


def test_rate_limiter_off_and_memory_bound():
    assert RateLimiter(0).allow("x") == (True, 0)
    now = {"t": 0.0}
    limiter = RateLimiter(5, window=10, clock=lambda: now["t"])
    for i in range(10001):
        limiter.allow(f"k{i}")
    now["t"] = 100
    limiter.allow("fresh")
    assert len(limiter._hits) < 10  # expired keys were dropped


# ---------- logging and metrics ----------
def make_record(msg="hello", **extra):
    record = logging.LogRecord("x", logging.INFO, __file__, 1, msg, (), None)
    record.__dict__.update(extra)
    return record


def test_json_formatter_includes_extras_and_exceptions():
    entry = json.loads(JsonFormatter().format(make_record("hi", method="GET", status=200, request_id="r1")))
    assert entry["message"] == "hi" and entry["method"] == "GET" and entry["status"] == 200 and entry["request_id"] == "r1"
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = make_record("bad")
        record.exc_info = sys.exc_info()
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exception"]
    assert json.loads(JsonFormatter().format(make_record()))["request_id"] == "-"


def test_request_id_filter_inside_and_outside_a_request():
    record = make_record()
    RequestIdFilter().filter(record)
    assert record.request_id == "-"
    app = Flask(__name__)
    with app.test_request_context():
        g.request_id = "abc"
        RequestIdFilter().filter(record)
    assert record.request_id == "abc"
    with app.test_request_context():  # a request without an id yet
        RequestIdFilter().filter(record)
    assert record.request_id == "-"


def test_configure_logging_text_and_json(capsys):
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("WARNING", "json")
        logging.getLogger("t").warning("structured")
        assert json.loads(capsys.readouterr().err.strip())["message"] == "structured"
        configure_logging("INFO", "text")
        logging.getLogger("t").info("plain")
        assert "plain" in capsys.readouterr().err
    finally:
        root.handlers[:], root.level = saved


def test_metrics_render_prometheus_text():
    m = Metrics()
    m.inc("http_requests_total", method="GET", status="2xx")
    m.inc("http_requests_total", method="GET", status="2xx")
    m.inc("plain_total")
    text = m.render({"cache_entries": 3})
    assert 'http_requests_total{method="GET",status="2xx"} 2' in text and "plain_total 1" in text and "cache_entries 3" in text
    assert Metrics().render() == "\n"

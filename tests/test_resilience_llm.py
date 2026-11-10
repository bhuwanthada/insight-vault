import sys
import threading
import types

import pytest

from backend import llm as llm_module
from backend.config import Settings
from backend.llm import LLMClient, LLMUnavailable, parse_json_object
from backend.resilience import CircuitBreaker, SingleFlight, TTLCache
from backend.store import Store
from tests.conftest import completion_returning


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---------- settings ----------
def test_settings_defaults_and_env_overrides():
    assert Settings.from_env({}).model == "" and Settings.from_env({}).votes == 3
    env = {"LLM_MODEL": "openrouter/x/y", "LLM_API_KEY": "k", "LLM_TIMEOUT": "5", "LLM_MAX_RETRIES": "1",
           "LLM_MAX_CONCURRENCY": "2", "LLM_VOTES": "5", "LLM_VOTE_TEMPERATURE": "0.9", "CACHE_TTL": "10", "CACHE_SIZE": "7"}
    s = Settings.from_env(env)
    assert (s.model, s.api_key, s.timeout, s.max_retries, s.max_concurrency, s.votes, s.vote_temperature, s.cache_ttl, s.cache_size) == (
        "openrouter/x/y", "k", 5.0, 1, 2, 5, 0.9, 10.0, 7)


def test_settings_read_process_environment(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m")
    assert Settings.from_env().model == "m"


# ---------- TTL cache ----------
def test_cache_hit_miss_expiry_and_lru_eviction():
    clock = Clock()
    cache = TTLCache(maxsize=2, ttl=10, clock=clock)
    assert cache.get("a") is None and cache.get("a", "dflt") == "dflt"
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1  # touching "a" makes "b" the oldest
    cache.set("c", 3)
    assert cache.get("b") is None and cache.get("a") == 1 and len(cache) == 2
    clock.t = 11
    assert cache.get("a") is None and len(cache) == 1  # expired entry removed on read


# ---------- single flight ----------
def test_single_flight_runs_once_for_concurrent_callers():
    flight, gate, runs = SingleFlight(), threading.Event(), []
    results = []

    def slow():
        runs.append(1)
        gate.wait(2)
        return "value"

    leader = threading.Thread(target=lambda: results.append(flight.do("k", slow)))
    leader.start()
    while not runs:  # wait until the leader is inside fn
        pass
    followers = [threading.Thread(target=lambda: results.append(flight.do("k", slow))) for _ in range(5)]
    for t in followers:
        t.start()
    gate.set()
    for t in [leader, *followers]:
        t.join()
    assert results == ["value"] * 6 and len(runs) == 1
    assert flight.do("k", lambda: "again") == "again"  # key is released afterwards


def test_single_flight_shares_errors_with_waiters():
    flight, gate, started = SingleFlight(), threading.Event(), threading.Event()
    seen = []

    def boom():
        started.set()
        gate.wait(2)
        raise RuntimeError("boom")

    def call():
        try:
            flight.do("k", boom)
        except RuntimeError as exc:
            seen.append(str(exc))

    leader = threading.Thread(target=call)
    leader.start()
    started.wait(2)
    follower = threading.Thread(target=call)
    follower.start()
    gate.set()
    leader.join()
    follower.join()
    assert seen == ["boom", "boom"]


# ---------- circuit breaker ----------
def test_breaker_opens_half_opens_and_closes():
    clock = Clock()
    cb = CircuitBreaker(threshold=2, reset_after=10, clock=clock)
    assert cb.allow()
    cb.record_failure()
    assert cb.allow()
    cb.record_failure()  # threshold reached -> open
    assert not cb.allow()
    clock.t = 10
    assert cb.allow()  # half-open trial
    assert not cb.allow()  # timer re-armed: only one trial
    cb.record_success()
    assert cb.allow()


# ---------- JSON parsing ----------
def test_parse_json_object_handles_code_fences_and_rejects_non_objects():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")
    with pytest.raises(ValueError):
        parse_json_object("not json")


# ---------- LLM client ----------
def client(completion, **kw):
    sleeps = []
    settings = Settings(model="fake/model", max_retries=kw.pop("retries", 2), **kw)
    return LLMClient(settings, completion=completion, sleep=sleeps.append, rng=lambda: 0.5), sleeps


def test_disabled_without_model():
    c = LLMClient(Settings())
    assert not c.enabled
    with pytest.raises(LLMUnavailable, match="no LLM configured"):
        c.complete_json("s", "u")


def test_success_passes_model_temperature_and_api_key():
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return completion_returning('{"ok": true}')(**kwargs)

    c = LLMClient(Settings(model="m1", api_key="secret", timeout=7), completion=completion)
    assert c.enabled
    assert c.complete_json("sys", "usr", model="override", temperature=0.4) == {"ok": True}
    assert seen["model"] == "override" and seen["temperature"] == 0.4 and seen["api_key"] == "secret" and seen["timeout"] == 7
    assert seen["messages"][0] == {"role": "system", "content": "sys"}


def test_no_api_key_is_not_sent():
    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return completion_returning("{}")(**kwargs)

    LLMClient(Settings(model="m"), completion=completion).complete_json("s", "u")
    assert "api_key" not in seen


def test_retries_with_backoff_then_succeeds():
    c, sleeps = client(completion_returning(RuntimeError("net"), "not json", '{"v": 1}'))
    assert c.complete_json("s", "u") == {"v": 1}
    assert sleeps == [0.5 * 1.0, 1.0 * 1.0]  # 0.5*2**attempt * (0.5 + rng=0.5)


def test_gives_up_after_retries_and_opens_circuit():
    c, sleeps = client(completion_returning(RuntimeError("down")), retries=1)
    c.breaker = CircuitBreaker(threshold=2)
    for _ in range(2):
        with pytest.raises(LLMUnavailable, match="down"):
            c.complete_json("s", "u")
    assert len(sleeps) == 2  # one backoff per call (between its 2 attempts), none after the last
    with pytest.raises(LLMUnavailable, match="circuit is open"):
        c.complete_json("s", "u")


def test_concurrency_limit_is_enforced():
    active, peak, lock = 0, 0, threading.Lock()
    release = threading.Event()

    def completion(**kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        release.wait(0.2)
        with lock:
            active -= 1
        return completion_returning("{}")(**kwargs)

    c = LLMClient(Settings(model="m", max_concurrency=2), completion=completion)
    threads = [threading.Thread(target=lambda: c.complete_json("s", "u")) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 2


def test_default_completion_uses_litellm(monkeypatch):
    fake = types.SimpleNamespace(completion=lambda **kw: ("called", kw))
    monkeypatch.setitem(sys.modules, "litellm", fake)
    assert llm_module.litellm_completion(model="m")[0] == "called"


# ---------- store under contention (Windows: replace fails while a reader has the file open) ----------
def test_store_survives_concurrent_readers_and_writers(tmp_path):
    store = Store(str(tmp_path / "db.json"))
    store.save({"notes": [], "highlights": [], "insights": [], "tickets": [], "reports": []})
    errors = []

    def writer(n):
        try:
            for i in range(25):
                with store.lock:
                    db = store.load()
                    db["notes"].append({"w": n, "i": i})
                    store.save(db)
        except Exception as exc:
            errors.append(exc)

    def reader():
        try:
            for _ in range(60):
                store.load()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(store.load()["notes"]) == 100  # no lost updates

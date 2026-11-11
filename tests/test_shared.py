"""Redis-backed cache, single-flight and rate limiting, tested against fakeredis (a faithful in-memory Redis)."""
import threading
from datetime import datetime, timezone

import fakeredis
import pytest
import redis

from backend.app import create_app
from backend.config import Settings
from backend.limits import RateLimiter
from backend.resilience import SingleFlight, TTLCache
from backend.shared import RedisCache, RedisRateLimiter, RedisSingleFlight, Shared
from tests.conftest import FakeLLM
from tests.dburl import db_url

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def client():
    return fakeredis.FakeRedis(decode_responses=True)


class Down:
    """A Redis that is unreachable."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.ConnectionError("redis is down")

        return fail

    def scan_iter(self, **kwargs):
        raise redis.ConnectionError("redis is down")


# ---------- cache ----------
def test_cache_stores_json_with_a_ttl_and_counts_hits(client):
    cache = RedisCache(client, "iv:acme:cache:", ttl=120)
    assert cache.get("k") is None and cache.get("k", "dflt") == "dflt" and cache.misses == 2
    cache.set("k", {"category": "Performance", "confidence": 1.0})
    assert cache.get("k") == {"category": "Performance", "confidence": 1.0} and cache.hits == 1 and len(cache) == 1
    assert 0 < client.ttl("iv:acme:cache:k") <= 120


def test_each_organizations_cache_is_separate(client):
    a, b = RedisCache(client, "iv:acme:cache:", 60), RedisCache(client, "iv:beta:cache:", 60)
    a.set("same-key", "A's answer")
    assert b.get("same-key") is None and a.get("same-key") == "A's answer" and len(b) == 0


def test_cache_survives_redis_outages_without_raising():
    cache = RedisCache(Down(), "p:", 60)
    cache.set("k", 1)  # skipped
    assert cache.get("k", "fallback") == "fallback" and cache.misses == 1 and len(cache) == 0


# ---------- single flight ----------
def test_one_leader_computes_and_publishes_for_everyone(client):
    flight, calls = RedisSingleFlight(client, "f:"), []

    def compute():
        calls.append(1)
        return {"answer": 42}

    assert flight.do("key", compute) == {"answer": 42} and calls == [1]
    assert client.get("f:lock:key") is None and client.get("f:result:key") is not None  # lock released, result left for late readers


def test_callers_arriving_during_the_computation_share_its_result(client):
    flight, started, release, calls, results = RedisSingleFlight(client, "f:", poll_seconds=0.01), threading.Event(), threading.Event(), [], []

    def slow():
        calls.append(1)
        started.set()
        release.wait(5)
        return "shared answer"

    leader = threading.Thread(target=lambda: results.append(flight.do("k", slow)))
    leader.start()
    started.wait(5)
    followers = [threading.Thread(target=lambda: results.append(flight.do("k", slow))) for _ in range(4)]
    for t in followers:
        t.start()
    release.set()
    for t in [leader, *followers]:
        t.join()
    assert results == ["shared answer"] * 5 and calls == [1]  # five callers, one computation


def test_a_waiting_caller_computes_for_itself_if_the_leader_vanished(client):
    flight = RedisSingleFlight(client, "f:", sleep=lambda s: client.delete("f:lock:k"))  # the leader dies while we wait
    client.set("f:lock:k", "someone-else")
    assert flight.do("k", lambda: "mine") == "mine"


def test_a_waiting_caller_gives_up_after_the_wait_limit(client):
    now = {"t": 0.0}
    flight = RedisSingleFlight(client, "f:", wait_seconds=5, sleep=lambda s: now.update(t=now["t"] + 3), clock=lambda: now["t"])
    client.set("f:lock:k", "stuck-leader")
    assert flight.do("k", lambda: "computed myself") == "computed myself" and now["t"] >= 5


def test_a_failure_releases_the_lock_and_reaches_the_caller(client):
    flight = RedisSingleFlight(client, "f:")
    with pytest.raises(ValueError, match="boom"):
        flight.do("k", lambda: (_ for _ in ()).throw(ValueError("boom")))
    assert client.get("f:lock:k") is None and flight.do("k", lambda: "recovered") == "recovered"


def test_a_lock_that_changed_hands_is_left_alone(client):
    flight = RedisSingleFlight(client, "f:")

    def steal_then_return():
        client.set("f:lock:k", "another-leader")  # our lock expired and someone else took it
        return "done"

    assert flight.do("k", steal_then_return) == "done" and client.get("f:lock:k") == "another-leader"


def test_results_that_cannot_be_shared_are_still_returned(client, caplog):
    flight = RedisSingleFlight(client, "f:")
    assert flight.do("k", lambda: {"not json": {1, 2}}) == {"not json": {1, 2}} and "could not share" in caplog.text


def test_single_flight_degrades_to_local_computation_when_redis_is_down(caplog):
    flight, calls = RedisSingleFlight(Down(), "f:"), []
    assert flight.do("k", lambda: calls.append(1) or "ok") == "ok" and calls == [1] and "computing locally" in caplog.text


def test_lost_contact_while_waiting_and_while_releasing_never_repeats_the_work(client, caplog):
    class Flaky(Down):
        """Works for the first few calls, then Redis goes away."""

        def __init__(self, real, ok_calls):
            self.real, self.left = real, ok_calls

        def __getattr__(self, name):
            def call(*args, **kwargs):
                if self.left <= 0:
                    raise redis.ConnectionError("redis went away")
                self.left -= 1
                return getattr(self.real, name)(*args, **kwargs)

            return call

    follower_flight = RedisSingleFlight(Flaky(client, 1), "f:")  # acquiring the lock fails (held) then waiting fails
    client.set("f:lock:k", "leader")
    assert follower_flight.do("k", lambda: "local") == "local" and "lost contact" in caplog.text
    calls = []
    leader_flight = RedisSingleFlight(Flaky(client, 1), "g:")  # we get the lock, then Redis dies before publishing/releasing
    assert leader_flight.do("k", lambda: calls.append(1) or "result") == "result" and calls == [1]  # computed exactly once
    assert "could not share" in caplog.text and "could not release" in caplog.text


# ---------- rate limiter ----------
def test_the_rate_limit_is_shared_and_reports_when_to_retry(client):
    now = {"t": 600.0}
    a = RedisRateLimiter(client, "l:", limit=2, window=60, clock=lambda: now["t"])
    b = RedisRateLimiter(client, "l:", limit=2, window=60, clock=lambda: now["t"])  # a second process
    assert a.allow("1.1.1.1") == (True, 0) and b.allow("1.1.1.1") == (True, 0)
    allowed, retry = a.allow("1.1.1.1")
    assert not allowed and 1 <= retry <= 61
    assert a.allow("2.2.2.2") == (True, 0)  # other clients are independent
    now["t"] = 661.0  # the next window
    assert b.allow("1.1.1.1") == (True, 0)
    assert 0 < client.ttl(f"l:1.1.1.1:{int(600 // 60)}") <= 61  # counters clean themselves up


def test_rate_limiting_is_off_at_zero_and_fails_open_when_redis_is_down(caplog):
    assert RedisRateLimiter(Down(), "l:", limit=0).allow("x") == (True, 0)
    assert RedisRateLimiter(Down(), "l:", limit=1).allow("x") == (True, 0) and "allowing the request" in caplog.text


# ---------- the selector ----------
def test_shared_picks_memory_or_redis(client):
    memory, shared = Shared(), Shared(client)
    settings = Settings(cache_size=5, cache_ttl=30)
    assert memory.backend == "memory" and shared.backend == "redis"
    assert isinstance(memory.cache("a", settings), TTLCache) and isinstance(memory.flight("a"), SingleFlight) and isinstance(memory.limiter("g", 3), RateLimiter)
    assert isinstance(shared.cache("a", settings), RedisCache) and isinstance(shared.flight("a"), RedisSingleFlight) and isinstance(shared.limiter("g", 3), RedisRateLimiter)
    assert shared.cache("acme", settings).prefix != shared.cache("beta", settings).prefix
    memory.check()
    shared.check()
    with pytest.raises(redis.ConnectionError):
        Shared(Down()).check()


def test_shared_builds_a_client_from_the_redis_url():
    assert Shared.from_settings(Settings()).backend == "memory"
    built = Shared.from_settings(Settings(redis_url="redis://cache.internal:6379/0"))  # lazy: no connection is made
    assert built.backend == "redis" and built.client.connection_pool.connection_kwargs["host"] == "cache.internal"


# ---------- two API processes sharing one Redis ----------
def two_processes(client, tmp_path, **settings_kw):
    db = db_url(tmp_path, 'shared')
    llm = FakeLLM(lambda *a: {"category": "Performance"})
    make = lambda: create_app(settings=Settings(database_url=db, **settings_kw), llm=llm, clock=lambda: NOW, shared=Shared(client))
    return make(), make(), llm


def test_a_classification_made_by_one_process_is_reused_by_the_other(client, tmp_path):
    first, second, llm = two_processes(client, tmp_path)
    a, b = first.test_client(), second.test_client()
    a.post("/api/tickets", json={"id": "T1", "subject": "Strange behaviour", "description": "odd", "createdAt": "2026-06-14T00:00:00Z", "resolvedAt": "2026-06-14T01:00:00Z"})
    assert a.get("/api/report?days=7").get_json()["categories"][0]["category"] == "Performance" and len(llm.calls) == 3
    assert b.get("/api/report?days=7").get_json()["categories"][0]["category"] == "Performance"
    assert len(llm.calls) == 3  # no new model calls: the answer came from the shared cache
    assert any(k.startswith("iv:default:cache:") for k in client.keys("*"))


def test_the_rate_limit_applies_across_processes(client, tmp_path):
    first, second, _ = two_processes(client, tmp_path, rate_limit=3)
    a, b = first.test_client(), second.test_client()
    assert [a.get("/api/state").status_code, b.get("/api/state").status_code, a.get("/api/state").status_code] == [200, 200, 200]
    assert b.get("/api/state").status_code == 429  # four requests in total, whichever process served them


def test_readiness_includes_the_shared_cache(client, tmp_path, monkeypatch):
    app, _, _ = two_processes(client, tmp_path)
    c = app.test_client()
    assert c.get("/api/ready").get_json() == {"status": "ready"}
    monkeypatch.setattr(client, "ping", lambda: (_ for _ in ()).throw(redis.ConnectionError("cache down")))
    response = c.get("/api/ready")
    assert response.status_code == 503 and "cache down" in response.get_json()["error"]

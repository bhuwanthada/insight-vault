import threading

from backend.resilience import CircuitBreaker, SingleFlight, TTLCache
from backend.store import JsonStore


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ---------- TTL cache ----------
def test_cache_hit_miss_expiry_and_lru_eviction():
    clock = Clock()
    cache = TTLCache(maxsize=2, ttl=10, clock=clock)
    assert cache.get("a") is None and cache.get("a", "dflt") == "dflt" and cache.misses == 2
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1 and cache.hits == 1  # touching "a" makes "b" the oldest
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


# ---------- store under contention (Windows: replace fails while a reader has the file open) ----------
def test_store_survives_concurrent_readers_and_writers(tmp_path):
    store = JsonStore(str(tmp_path / "db.json"))
    store.save({"notes": []})
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

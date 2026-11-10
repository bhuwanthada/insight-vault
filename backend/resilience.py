"""Concurrency and reliability primitives: TTL cache, single-flight, circuit breaker."""
import threading
import time
from collections import OrderedDict

_MISS = object()


class TTLCache:
    """Thread-safe LRU cache whose entries expire after `ttl` seconds."""

    def __init__(self, maxsize=512, ttl=3600.0, clock=time.monotonic):
        self.maxsize, self.ttl, self._clock = maxsize, ttl, clock
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            entry = self._items.get(key, _MISS)
            if entry is _MISS:
                return default
            expires_at, value = entry
            if expires_at <= self._clock():
                del self._items[key]
                return default
            self._items.move_to_end(key)
            return value

    def set(self, key, value):
        with self._lock:
            self._items[key] = (self._clock() + self.ttl, value)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def __len__(self):
        with self._lock:
            return len(self._items)


class _Call:
    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class SingleFlight:
    """Collapse concurrent calls for the same key into one execution (prevents cache stampedes).

    The first caller (leader) runs `fn`; callers arriving meanwhile wait and share its result or error.
    """

    def __init__(self):
        self._calls = {}
        self._lock = threading.Lock()

    def do(self, key, fn):
        with self._lock:
            call = self._calls.get(key)
            leader = call is None
            if leader:
                call = self._calls[key] = _Call()
        if not leader:
            call.event.wait()
            if call.error is not None:
                raise call.error
            return call.result
        try:
            call.result = fn()
        except BaseException as exc:
            call.error = exc
            raise
        finally:
            with self._lock:
                del self._calls[key]
            call.event.set()
        return call.result


class CircuitBreaker:
    """Stop calling a failing dependency for `reset_after` seconds, then allow one trial call."""

    def __init__(self, threshold=5, reset_after=30.0, clock=time.monotonic):
        self.threshold, self.reset_after, self._clock = threshold, reset_after, clock
        self._failures = 0
        self._opened_at = None
        self._lock = threading.Lock()

    def allow(self):
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self.reset_after:
                self._opened_at = self._clock()  # half-open: let one trial through, re-arm the timer
                return True
            return False

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = self._clock()

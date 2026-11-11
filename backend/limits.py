"""Sliding-window rate limiter (in memory, per process)."""
import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit, window=60.0, clock=time.monotonic):
        self.limit, self.window, self._clock = limit, window, clock
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        """Record a hit. Returns (allowed, seconds_until_retry)."""
        if self.limit <= 0:
            return True, 0
        now = self._clock()
        with self._lock:
            if len(self._hits) > 10000:  # bound memory: drop keys whose window has fully passed
                self._hits = {k: q for k, q in self._hits.items() if q and q[-1] > now - self.window}
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1, int(hits[0] + self.window - now) + 1)
            hits.append(now)
            return True, 0

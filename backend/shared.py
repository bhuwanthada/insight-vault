"""Cache, request coalescing and rate limiting: in memory for one process, in Redis for many.

With REDIS_URL set, every API process shares the same cache, the same "only one of us calls the LLM"
lock and the same rate-limit counters. Keys are prefixed per organization, so organizations never share
cached answers. If Redis is unreachable the app keeps working: caches miss, locks are skipped and the
rate limiter lets requests through (and a warning is logged).
"""
import json
import logging
import threading
import time
import uuid

import redis

from .limits import RateLimiter
from .resilience import SingleFlight, TTLCache

log = logging.getLogger("insight_vault.shared")


class RedisCache:
    """Same interface as TTLCache, stored in Redis (values must be JSON)."""

    def __init__(self, client, prefix, ttl):
        self.client, self.prefix, self.ttl = client, prefix, int(ttl)
        self.hits = self.misses = 0

    def get(self, key, default=None):
        try:
            raw = self.client.get(self.prefix + key)
        except redis.RedisError as exc:
            log.warning("cache read failed (treated as a miss): %s", exc)
            raw = None
        if raw is None:
            self.misses += 1
            return default
        self.hits += 1
        return json.loads(raw)

    def set(self, key, value):
        try:
            self.client.set(self.prefix + key, json.dumps(value), ex=max(1, self.ttl))
        except redis.RedisError as exc:
            log.warning("cache write failed (skipped): %s", exc)

    def __len__(self):
        try:
            return sum(1 for _ in self.client.scan_iter(match=self.prefix + "*", count=500))
        except redis.RedisError:
            return 0


class RedisSingleFlight:
    """Across processes, one caller computes a value while the others wait for its answer.

    If the leader fails or is too slow, a waiting caller computes the value itself, so nobody is stuck."""

    def __init__(self, client, prefix, lock_seconds=60, wait_seconds=30.0, poll_seconds=0.05, sleep=time.sleep, clock=time.monotonic):
        self.client, self.prefix = client, prefix
        self.lock_ms, self.wait_seconds, self.poll_seconds, self._sleep, self._clock = int(lock_seconds * 1000), wait_seconds, poll_seconds, sleep, clock

    def do(self, key, fn):
        lock, result_key, token = f"{self.prefix}lock:{key}", f"{self.prefix}result:{key}", uuid.uuid4().hex
        try:
            leader = self.client.set(lock, token, nx=True, px=self.lock_ms)
        except redis.RedisError as exc:
            log.warning("shared lock unavailable, computing locally: %s", exc)
            return fn()
        if not leader:
            try:
                raw = self._wait_for(lock, result_key)
            except redis.RedisError as exc:
                log.warning("lost contact with the shared lock, computing locally: %s", exc)
                raw = None
            return fn() if raw is None else json.loads(raw)
        try:
            result = fn()  # computed exactly once: a failure to publish below never triggers a second computation
            self._publish(result_key, result)
            return result
        finally:
            self._release(lock, token)

    def _publish(self, result_key, result):
        try:
            self.client.set(result_key, json.dumps(result), px=30_000)  # long enough for waiting callers to read it
        except (redis.RedisError, TypeError) as exc:
            log.warning("could not share the result with waiting callers: %s", exc)

    def _release(self, lock, token):
        try:
            if self.client.get(lock) == token:  # only release a lock that is still ours
                self.client.delete(lock)
        except redis.RedisError as exc:
            log.warning("could not release the shared lock (it will expire): %s", exc)

    def _wait_for(self, lock, result_key):
        deadline = self._clock() + self.wait_seconds
        while self._clock() < deadline:
            raw = self.client.get(result_key)
            if raw is not None:
                return raw
            if self.client.get(lock) is None:  # the leader finished without leaving an answer, or died
                return self.client.get(result_key)
            self._sleep(self.poll_seconds)
        return None


class RedisRateLimiter:
    """Fixed-window counter shared by every process. Fails open when Redis is down."""

    def __init__(self, client, prefix, limit, window=60.0, clock=time.time):
        self.client, self.prefix, self.limit, self.window, self._clock = client, prefix, limit, window, clock

    def allow(self, key):
        if self.limit <= 0:
            return True, 0
        now = self._clock()
        window_id = int(now // self.window)
        try:
            counter = f"{self.prefix}{key}:{window_id}"
            count = self.client.incr(counter)
            if count == 1:
                self.client.expire(counter, int(self.window) + 1)
        except redis.RedisError as exc:
            log.warning("rate limiter unavailable, allowing the request: %s", exc)
            return True, 0
        if count > self.limit:
            return False, max(1, int((window_id + 1) * self.window - now) + 1)
        return True, 0


class Shared:
    """Hands out the right implementation for the configuration."""

    def __init__(self, client=None):
        self.client = client

    @classmethod
    def from_settings(cls, settings):
        if not settings.redis_url:
            return cls()
        return cls(redis.Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2))

    @property
    def backend(self):
        return "redis" if self.client is not None else "memory"

    def cache(self, org_id, settings):
        if self.client is None:
            return TTLCache(settings.cache_size, settings.cache_ttl)
        return RedisCache(self.client, f"iv:{org_id}:cache:", settings.cache_ttl)

    def flight(self, org_id):
        return SingleFlight() if self.client is None else RedisSingleFlight(self.client, f"iv:{org_id}:flight:")

    def limiter(self, name, limit):
        return RateLimiter(limit) if self.client is None else RedisRateLimiter(self.client, f"iv:limit:{name}:", limit)

    def check(self):
        """Raise redis.RedisError if the shared service is configured but unreachable."""
        if self.client is not None:
            self.client.ping()

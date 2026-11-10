"""Runtime settings, read once from environment variables (fail fast on malformed numbers)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    model: str = ""  # LiteLLM model id, e.g. "openrouter/<vendor>/<model>". Empty = LLM disabled.
    api_key: str = ""  # optional; otherwise LiteLLM reads the provider's own env var (e.g. OPENROUTER_API_KEY)
    timeout: float = 30.0
    max_retries: int = 2
    max_concurrency: int = 4
    votes: int = 3
    vote_temperature: float = 0.7
    cache_ttl: float = 3600.0
    cache_size: int = 512

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(
            model=env.get("LLM_MODEL", ""),
            api_key=env.get("LLM_API_KEY", ""),
            timeout=float(env.get("LLM_TIMEOUT", cls.timeout)),
            max_retries=int(env.get("LLM_MAX_RETRIES", cls.max_retries)),
            max_concurrency=int(env.get("LLM_MAX_CONCURRENCY", cls.max_concurrency)),
            votes=int(env.get("LLM_VOTES", cls.votes)),
            vote_temperature=float(env.get("LLM_VOTE_TEMPERATURE", cls.vote_temperature)),
            cache_ttl=float(env.get("CACHE_TTL", cls.cache_ttl)),
            cache_size=int(env.get("CACHE_SIZE", cls.cache_size)),
        )

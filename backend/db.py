"""Database engine creation and per-organization store lookup."""
import threading

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from .store import JsonStore, SqlStore


def normalise_url(url):
    """Accept the short forms people paste (postgres://, postgresql://) and pick the installed driver."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url, pool_size=10):
    url = normalise_url(url)
    if url.startswith("sqlite"):
        in_memory = url in ("sqlite://", "sqlite:///:memory:")
        kwargs = {"poolclass": StaticPool} if in_memory else {}
        engine = sa.create_engine(url, connect_args={"check_same_thread": False}, **kwargs)

        @sa.event.listens_for(engine, "connect")
        def _pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA busy_timeout=5000")
            if not in_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine
    return sa.create_engine(url, pool_size=pool_size, max_overflow=pool_size, pool_pre_ping=True)


class StoreFactory:
    """Hands out one store per organization (one instance each, so locks are shared within a process)."""

    def __init__(self, engine=None, json_path=None):
        self.engine, self.json_path = engine, json_path
        self._stores, self._lock = {}, threading.Lock()

    def for_org(self, org_id):
        with self._lock:
            if org_id not in self._stores:
                self._stores[org_id] = SqlStore(self.engine, org_id) if self.engine is not None else JsonStore(self.json_path, org_id)
            return self._stores[org_id]

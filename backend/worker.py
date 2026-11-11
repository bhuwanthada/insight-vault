"""Background worker: syncs connected ticketing tools on their schedules.

    python -m backend.worker

Run one or several copies next to the API (same environment). Each connector is leased in the
database while it syncs, so copies never sync the same connector at once, and a crashed worker's
lease simply expires.
"""
import logging
import os
import signal
import time
from datetime import datetime, timezone

from .config import Settings
from .connectors.repo import ConnectorRepo
from .connectors.sync import LEASE_SECONDS, SyncService
from .runtime import build_runtime

log = logging.getLogger("insight_vault.worker")


def run_once(service, repo, clock):
    """Sync one due connector. Returns its summary, or None when nothing is due."""
    row = repo.claim_due(clock(), LEASE_SECONDS)
    return service.run(row) if row else None


def loop(service, repo, clock, poll_seconds, stop, sleep=time.sleep):
    """Keep syncing until ``stop()`` says so. Idle workers wait ``poll_seconds`` between checks."""
    while not stop():
        try:
            result = run_once(service, repo, clock)
        except Exception:  # a database hiccup must not kill the worker
            log.exception("worker iteration failed")
            result = None
        if result is None:
            sleep(poll_seconds)


def main():
    from dotenv import load_dotenv

    from .observability import configure_logging

    load_dotenv(os.environ.get("ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    settings = Settings.from_env()
    if not settings.database_url:
        raise SystemExit("The worker needs DATABASE_URL: connectors are stored in the database.")
    configure_logging(settings.log_level, settings.log_format)
    clock = lambda: datetime.now(timezone.utc)
    runtime = build_runtime(settings, clock)
    repo = ConnectorRepo(runtime.engine)
    service = SyncService(runtime.tenants, repo, runtime.box, clock, runtime.settings)
    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    log.info("worker started (poll every %ss)", settings.worker_poll_seconds)
    loop(service, repo, clock, settings.worker_poll_seconds, lambda: bool(stopping))
    log.info("worker stopped")

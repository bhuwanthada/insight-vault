"""Where configured connectors live (database). Every lookup an organization makes is scoped to its org id."""
import json
import uuid
from datetime import timedelta

import sqlalchemy as sa

from .. import schema

PUBLIC = ("id", "kind", "name", "config", "mapping", "interval_minutes", "enabled", "last_sync_at", "last_status", "last_error", "last_count", "next_run_at")


def _row(r):
    d = dict(r._mapping)
    d["config"], d["mapping"] = json.loads(d["config"]), json.loads(d["mapping"])
    return d


def public(row):
    """A connector as shown to people: never the encrypted secrets or the sync cursor."""
    return {k: row[k] for k in PUBLIC}


class ConnectorRepo:
    def __init__(self, engine):
        self.engine = engine

    def create(self, org_id, kind, name, config, secrets_enc, mapping, interval_minutes, enabled, now_iso):
        connector_id = uuid.uuid4().hex[:12]
        with self.engine.begin() as conn:
            conn.execute(schema.connectors.insert().values(
                id=connector_id, org_id=org_id, kind=kind, name=name, config=json.dumps(config), secrets_enc=secrets_enc,
                mapping=json.dumps(mapping), interval_minutes=interval_minutes, enabled=enabled, created_at=now_iso,
                next_run_at=now_iso, last_count=0))
        return self.get(org_id, connector_id)

    def get(self, org_id, connector_id):
        c = schema.connectors
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(c).where(c.c.id == connector_id, c.c.org_id == org_id)).first()
        return _row(row) if row else None

    def get_any(self, connector_id):
        """Internal use by the worker: look a connector up without knowing its organization."""
        c = schema.connectors
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(c).where(c.c.id == connector_id)).first()
        return _row(row) if row else None

    def list(self, org_id):
        c = schema.connectors
        with self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(sa.select(c).where(c.c.org_id == org_id).order_by(c.c.created_at, c.c.id))]

    def count(self, org_id):
        c = schema.connectors
        with self.engine.connect() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(c).where(c.c.org_id == org_id)).scalar()

    def update(self, org_id, connector_id, **fields):
        values = {k: (json.dumps(v) if k in ("config", "mapping") else v) for k, v in fields.items()}
        c = schema.connectors
        with self.engine.begin() as conn:
            conn.execute(c.update().where(c.c.id == connector_id, c.c.org_id == org_id).values(**values))

    def delete(self, org_id, connector_id):
        c = schema.connectors
        with self.engine.begin() as conn:
            return conn.execute(c.delete().where(c.c.id == connector_id, c.c.org_id == org_id)).rowcount > 0

    # ---- scheduling (used by the worker and by "sync now")
    def claim_due(self, now, lease_seconds):
        """Atomically take one connector that is due and not leased by another worker. Returns it, or None."""
        c, now_iso = schema.connectors, now.isoformat()
        with self.engine.begin() as conn:
            candidates = conn.execute(sa.select(c.c.id).where(
                c.c.enabled.is_(True), sa.or_(c.c.next_run_at.is_(None), c.c.next_run_at <= now_iso),
                sa.or_(c.c.lease_until.is_(None), c.c.lease_until < now_iso)).order_by(c.c.next_run_at).limit(5)).scalars().all()
            for connector_id in candidates:  # only one worker's UPDATE can succeed per connector
                taken = conn.execute(c.update().where(
                    c.c.id == connector_id, sa.or_(c.c.lease_until.is_(None), c.c.lease_until < now_iso)
                ).values(lease_until=(now + timedelta(seconds=lease_seconds)).isoformat())).rowcount
                if taken:
                    row = conn.execute(sa.select(c).where(c.c.id == connector_id)).first()
                    return _row(row)
        return None

    def lease(self, connector_id, now, lease_seconds):
        """Take the lease for one connector (the "sync now" button). False if another worker holds it."""
        c, now_iso = schema.connectors, now.isoformat()
        with self.engine.begin() as conn:
            return bool(conn.execute(c.update().where(
                c.c.id == connector_id, sa.or_(c.c.lease_until.is_(None), c.c.lease_until < now_iso)
            ).values(lease_until=(now + timedelta(seconds=lease_seconds)).isoformat())).rowcount)

    def save_cursor(self, connector_id, cursor):
        c = schema.connectors
        with self.engine.begin() as conn:
            conn.execute(c.update().where(c.c.id == connector_id).values(cursor=cursor))

    def finish(self, connector_id, status, error, count, now, delay_minutes):
        """Record the outcome, release the lease and schedule the next run."""
        c = schema.connectors
        with self.engine.begin() as conn:
            conn.execute(c.update().where(c.c.id == connector_id).values(
                last_sync_at=now.isoformat(), last_status=status, last_error=error, last_count=count, lease_until=None,
                next_run_at=(now + timedelta(minutes=delay_minutes)).isoformat()))

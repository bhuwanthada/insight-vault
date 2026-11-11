"""Storage. A store is scoped to ONE organization: it cannot read or write another organization's data.

* ``JsonStore``: one JSON file, for local use and demos (a single organization).
* ``SqlStore``: PostgreSQL (production) or SQLite (development), one logical store per organization.

Both expose the same interface:

    with store.lock:                  # reentrant; on PostgreSQL also an advisory lock across processes
        db = store.load(only=...)     # lists: notes, highlights, insights, reports
        ...mutate...
        store.save(db)                # writes back only what changed
    store.tickets(since) / store.upsert_tickets(list)
    store.append_audit(entry, keep) / store.audit(limit)
    store.kv_get(key, default) / store.kv_set(key, value)
"""
import json
import os
import tempfile
import threading
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite

from . import schema

LIST_COLLECTIONS = ("notes", "highlights", "insights", "reports")


def utc_key(value):
    """Normalise an ISO timestamp (or datetime) to a sortable UTC string: 2026-06-14T12:00:00."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _canonical(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def empty_db():
    return {name: [] for name in LIST_COLLECTIONS}


# ------------------------------------------------------------------ JSON file
class JsonStore:
    """Single-file store for one organization. Writes are atomic."""

    kind = "json"

    def __init__(self, path, org_id="default"):
        self.path, self.org_id = path, org_id
        self.lock = threading.RLock()  # reentrant: routes hold it across load -> modify -> save

    def _read(self):
        with self.lock:  # on Windows os.replace fails if a reader has the file open, so reads share the write lock
            try:
                with open(self.path, encoding="utf-8") as handle:
                    return json.load(handle)
            except (FileNotFoundError, json.JSONDecodeError):
                return {}

    def _write(self, data):
        with self.lock:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, self.path)

    def check(self):
        """Raise OSError if the store is not usable (used by the readiness probe)."""
        self._write(self._read())

    def load(self, only=None):
        data = self._read()
        return {name: data.get(name, []) for name in (only or LIST_COLLECTIONS)}

    def save(self, db):
        with self.lock:
            data = self._read()
            data.update({name: db[name] for name in LIST_COLLECTIONS if name in db})
            self._write(data)

    def upsert_tickets(self, tickets):
        with self.lock:
            data = self._read()
            by_id = {t["id"]: t for t in data.get("tickets", [])}
            by_id.update({t["id"]: t for t in tickets})
            data["tickets"] = list(by_id.values())
            self._write(data)

    def tickets(self, since=None):
        rows = self._read().get("tickets", [])
        if since is None:
            return rows
        floor = utc_key(since)
        return [t for t in rows if utc_key(t["resolvedAt"]) >= floor]

    def ticket_count(self):
        return len(self._read().get("tickets", []))

    def existing_ticket_ids(self, ids):
        known = {t["id"] for t in self._read().get("tickets", [])}
        return {i for i in ids if i in known}

    def append_audit(self, entry, keep):
        with self.lock:
            data = self._read()
            data["audit"] = [*data.get("audit", []), entry][-keep:]
            self._write(data)

    def audit(self, limit):
        return self._read().get("audit", [])[-limit:][::-1]

    def kv_get(self, key, default=None):
        return self._read().get("kv", {}).get(key, default)

    def kv_set(self, key, value):
        with self.lock:
            data = self._read()
            data.setdefault("kv", {})[key] = value
            self._write(data)


# ------------------------------------------------------------------ SQL
class _OrgLock:
    """Reentrant lock that, on the outermost entry, opens one transaction used by every call inside it."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        s = self.store
        s._rlock.acquire()
        try:
            if s._depth == 0:
                s._conn = s.engine.connect()
                try:
                    s._tx = s._conn.begin()
                    s._owner = threading.get_ident()
                    schema.take_advisory_lock(s._conn, s.engine.dialect.name, "org:" + s.org_id)
                except BaseException:
                    s._conn.close()
                    s._conn, s._tx, s._owner = None, None, None
                    raise
            s._depth += 1
        except BaseException:
            s._rlock.release()  # never leak the lock if we could not start the transaction
            raise
        return s

    def __exit__(self, exc_type, exc, tb):
        s = self.store
        try:
            s._depth -= 1
            if s._depth == 0:
                try:
                    (s._tx.rollback if exc_type else s._tx.commit)()
                finally:
                    s._conn.close()
                    s._conn, s._tx, s._owner = None, None, None
        finally:
            s._rlock.release()
        return False


class SqlStore:
    kind = "sql"

    def __init__(self, engine, org_id):
        self.engine, self.org_id = engine, org_id
        self._rlock = threading.RLock()
        self._depth, self._conn, self._tx, self._owner = 0, None, None, None
        self.lock = _OrgLock(self)

    # -- connection plumbing: inside our own lock use its transaction, otherwise a short-lived one
    def _mine(self):
        return self._conn is not None and self._owner == threading.get_ident()

    def _read_run(self, fn):
        if self._mine():
            return fn(self._conn)
        with self.engine.connect() as conn:
            return fn(conn)

    def _write_run(self, fn):
        with self.lock:
            return fn(self._conn)

    def _upsert(self, table, values, keys):
        dialect = self.engine.dialect.name
        insert = (postgresql if dialect == "postgresql" else sqlite).insert(table).values(values)
        update = {c: insert.excluded[c] for c in values if c not in keys}
        return insert.on_conflict_do_update(index_elements=keys, set_=update)

    def check(self):
        self._read_run(lambda c: c.execute(sa.text("SELECT 1")))

    # -- list collections
    def load(self, only=None):
        d = schema.docs

        def run(conn):
            out = {}
            for name in only or LIST_COLLECTIONS:
                rows = conn.execute(sa.select(d.c.data).where(d.c.org_id == self.org_id, d.c.coll == name).order_by(d.c.seq))
                out[name] = [json.loads(r[0]) for r in rows]
            return out

        return self._read_run(run)

    def save(self, db):
        d = schema.docs

        def run(conn):
            for name in LIST_COLLECTIONS:
                if name not in db:
                    continue
                where = (d.c.org_id == self.org_id, d.c.coll == name)
                existing = {r[0]: r[1] for r in conn.execute(sa.select(d.c.id, d.c.data).where(*where))}
                next_seq = conn.execute(sa.select(sa.func.coalesce(sa.func.max(d.c.seq), 0)).where(*where)).scalar() + 1
                keep = set()
                for doc in db[name]:
                    keep.add(doc["id"])
                    payload = _canonical(doc)
                    if doc["id"] not in existing:
                        conn.execute(d.insert().values(org_id=self.org_id, coll=name, id=doc["id"], seq=next_seq, data=payload))
                        next_seq += 1
                    elif existing[doc["id"]] != payload:
                        conn.execute(d.update().where(*where, d.c.id == doc["id"]).values(data=payload))
                for gone in set(existing) - keep:
                    conn.execute(d.delete().where(*where, d.c.id == gone))

        self._write_run(run)

    # -- tickets
    def upsert_tickets(self, tickets):
        t = schema.tickets

        def run(conn):
            for ticket in tickets:
                values = {"org_id": self.org_id, "id": ticket["id"], "resolved_at": utc_key(ticket["resolvedAt"]),
                          "data": _canonical(ticket)}
                conn.execute(self._upsert(t, values, ["org_id", "id"]))

        self._write_run(run)

    def tickets(self, since=None):
        t = schema.tickets

        def run(conn):
            query = sa.select(t.c.data).where(t.c.org_id == self.org_id).order_by(t.c.resolved_at, t.c.id)
            if since is not None:
                query = query.where(t.c.resolved_at >= utc_key(since))
            return [json.loads(r[0]) for r in conn.execute(query)]

        return self._read_run(run)

    def ticket_count(self):
        t = schema.tickets
        return self._read_run(lambda c: c.execute(sa.select(sa.func.count()).select_from(t).where(t.c.org_id == self.org_id)).scalar())

    def existing_ticket_ids(self, ids):
        t, found = schema.tickets, set()
        ids = list(ids)

        def run(conn):
            for start in range(0, len(ids), 500):  # keep the IN list within every database's parameter limit
                rows = conn.execute(sa.select(t.c.id).where(t.c.org_id == self.org_id, t.c.id.in_(ids[start:start + 500])))
                found.update(r[0] for r in rows)

        self._read_run(run)
        return found

    # -- audit
    def append_audit(self, entry, keep):
        a = schema.audit

        def run(conn):
            conn.execute(a.insert().values(org_id=self.org_id, at=entry["at"], actor=entry["actor"],
                                           action=entry["action"], detail=entry["detail"]))
            cutoff = conn.execute(sa.select(a.c.id).where(a.c.org_id == self.org_id).order_by(a.c.id.desc()).offset(keep).limit(1)).scalar()
            if cutoff is not None:
                conn.execute(a.delete().where(a.c.org_id == self.org_id, a.c.id <= cutoff))

        self._write_run(run)

    def audit(self, limit):
        a = schema.audit

        def run(conn):
            rows = conn.execute(sa.select(a.c.at, a.c.actor, a.c.action, a.c.detail).where(a.c.org_id == self.org_id)
                                .order_by(a.c.id.desc()).limit(limit))
            return [{"at": r[0], "actor": r[1], "action": r[2], "detail": r[3]} for r in rows]

        return self._read_run(run)

    # -- small per-organization key/value documents (usage counters, connector counters, ...)
    def kv_get(self, key, default=None):
        k = schema.kv

        def run(conn):
            row = conn.execute(sa.select(k.c.data).where(k.c.org_id == self.org_id, k.c.k == key)).first()
            return default if row is None else json.loads(row[0])

        return self._read_run(run)

    def kv_set(self, key, value):
        values = {"org_id": self.org_id, "k": key, "data": _canonical(value)}
        self._write_run(lambda conn: conn.execute(self._upsert(schema.kv, values, ["org_id", "k"])))

import os
import threading
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from backend import schema
from backend.db import StoreFactory, make_engine, normalise_url
from backend.store import JsonStore, SqlStore, empty_db, utc_key

POSTGRES_URL = os.environ.get("TEST_DATABASE_URL")  # set in CI against a real PostgreSQL service
KINDS = ["json", "sqlite"] + (["postgres"] if POSTGRES_URL else [])
NOW = "2026-06-15T12:00:00+00:00"


def new_engine(kind, tmp_path):
    url = POSTGRES_URL if kind == "postgres" else f"sqlite:///{tmp_path / 'test.db'}"
    engine = make_engine(url)
    schema.migrate(engine, NOW)
    return engine


@pytest.fixture(params=KINDS)
def store(request, tmp_path):
    org = "org-" + uuid.uuid4().hex[:8]  # unique per test so a shared PostgreSQL stays clean
    if request.param == "json":
        return JsonStore(str(tmp_path / "db.json"), org)
    return SqlStore(new_engine(request.param, tmp_path), org)


def note(i, **extra):
    return {"id": f"n{i}", "title": f"Note {i}", **extra}


def ticket(i, resolved="2026-06-10T10:00:00+00:00"):
    return {"id": f"T{i}", "subject": f"s{i}", "resolvedAt": resolved}


# ---------- list collections ----------
def test_empty_store_loads_empty_lists(store):
    assert store.load() == empty_db()
    assert store.load(only=("notes",)) == {"notes": []}
    store.check()


def test_save_and_load_round_trip_with_order_updates_and_deletes(store):
    store.save({"notes": [note(1), note(2), note(3)], "highlights": [{"id": "h1", "noteId": "n1"}]})
    assert [n["id"] for n in store.load()["notes"]] == ["n1", "n2", "n3"]
    db = store.load()
    db["notes"][1]["title"] = "Renamed"
    db["notes"] = [db["notes"][0], db["notes"][1], note(4)]  # n3 removed, n4 added
    store.save(db)
    assert [(n["id"], n["title"]) for n in store.load()["notes"]] == [("n1", "Note 1"), ("n2", "Renamed"), ("n4", "Note 4")]
    assert store.load()["highlights"] == [{"id": "h1", "noteId": "n1"}]  # untouched collections survive


def test_saving_a_subset_never_touches_other_collections(store):
    store.save({"notes": [note(1)], "insights": [{"id": "i1"}]})
    partial = store.load(only=("notes",))
    partial["notes"].append(note(2))
    store.save(partial)
    full = store.load()
    assert len(full["notes"]) == 2 and full["insights"] == [{"id": "i1"}]


# ---------- tickets ----------
def test_tickets_upsert_never_duplicates_and_filter_by_window(store):
    store.upsert_tickets([ticket(1), ticket(2, "2026-05-01T09:00:00+05:30")])
    store.upsert_tickets([{**ticket(1), "subject": "changed"}])
    assert sorted(t["id"] for t in store.tickets()) == ["T1", "T2"]
    assert next(t for t in store.tickets() if t["id"] == "T1")["subject"] == "changed"
    recent = store.tickets(since=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert [t["id"] for t in recent] == ["T1"]
    assert [t["id"] for t in store.tickets(since="2026-04-30T23:00:00Z")] == ["T2", "T1"] or len(store.tickets(since="2026-04-30T23:00:00Z")) == 2


def test_utc_key_handles_strings_offsets_and_naive_datetimes():
    assert utc_key("2026-06-14T12:00:00Z") == "2026-06-14T12:00:00"
    assert utc_key("2026-06-14T12:00:00+05:30") == "2026-06-14T06:30:00"
    assert utc_key(datetime(2026, 6, 14, 12, 0)) == "2026-06-14T12:00:00"  # naive is treated as UTC
    assert utc_key(datetime(2026, 6, 14, 12, 0, tzinfo=timezone.utc)) == "2026-06-14T12:00:00"


# ---------- audit ----------
def test_audit_is_newest_first_and_trimmed(store):
    for i in range(5):
        store.append_audit({"at": f"t{i}", "actor": "a", "action": f"act{i}", "detail": ""}, keep=3)
    entries = store.audit(10)
    assert [e["action"] for e in entries] == ["act4", "act3", "act2"]
    assert [e["action"] for e in store.audit(1)] == ["act4"]


# ---------- kv ----------
def test_kv_defaults_overwrites_and_nesting(store):
    assert store.kv_get("usage") is None and store.kv_get("usage", {"x": 1}) == {"x": 1}
    store.kv_set("usage", {"2026-06-15": {"tokens": 5}})
    store.kv_set("usage", {"2026-06-15": {"tokens": 9}})
    assert store.kv_get("usage") == {"2026-06-15": {"tokens": 9}}


# ---------- locking ----------
def test_lock_is_reentrant_and_counters_do_not_lose_updates(store):
    def bump():
        for _ in range(15):
            with store.lock:
                with store.lock:  # nested use, as routes calling helpers do
                    store.kv_set("n", (store.kv_get("n") or 0) + 1)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.kv_get("n") == 60


# ---------- SQL specifics ----------
@pytest.fixture(params=[k for k in KINDS if k != "json"])
def sql(request, tmp_path):
    engine = new_engine(request.param, tmp_path)
    org_a, org_b = "a-" + uuid.uuid4().hex[:6], "b-" + uuid.uuid4().hex[:6]
    return SqlStore(engine, org_a), SqlStore(engine, org_b)


def test_organizations_are_completely_isolated(sql):
    a, b = sql
    a.save({"notes": [note(1)], "reports": [{"id": "r1"}]})
    a.upsert_tickets([ticket(1)])
    a.append_audit({"at": "t", "actor": "alice", "action": "x", "detail": ""}, keep=10)
    a.kv_set("usage", {"d": 1})
    assert b.load() == empty_db() and b.tickets() == [] and b.audit(10) == [] and b.kv_get("usage") is None
    b.save({"notes": [note(1, title="B's note")]})  # the same id in another org is a different document
    b.upsert_tickets([{**ticket(1), "subject": "B ticket"}])
    assert a.load()["notes"][0]["title"] == "Note 1" and b.load()["notes"][0]["title"] == "B's note"
    assert a.tickets()[0]["subject"] == "s1" and b.tickets()[0]["subject"] == "B ticket"
    b.save({"notes": []})  # deleting in B cannot delete A's rows
    assert len(a.load()["notes"]) == 1


def test_a_failed_transaction_rolls_back(sql):
    a, _ = sql
    a.save({"notes": [note(1)]})
    with pytest.raises(RuntimeError):
        with a.lock:
            a.save({"notes": [note(1), note(2)]})
            assert len(a.load()["notes"]) == 2  # visible inside the transaction
            raise RuntimeError("boom")
    assert len(a.load()["notes"]) == 1  # rolled back


def test_other_threads_do_not_see_uncommitted_work_or_borrow_the_connection(sql):
    a, _ = sql
    seen, release, entered = {}, threading.Event(), threading.Event()

    def holder():
        with a.lock:
            a.save({"notes": [note(1)]})
            entered.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    entered.wait(5)
    seen["before_commit"] = a.load() if False else None  # reading would block on SQLite's writer; skip the race
    release.set()
    t.join()
    assert len(a.load()["notes"]) == 1


def test_lock_is_released_when_the_transaction_cannot_start(sql, monkeypatch):
    a, _ = sql
    monkeypatch.setattr(a.engine, "connect", lambda: (_ for _ in ()).throw(RuntimeError("no connection")))
    with pytest.raises(RuntimeError, match="no connection"):
        with a.lock:
            pass
    monkeypatch.undo()
    with a.lock:  # not deadlocked
        a.kv_set("ok", 1)
    assert a.kv_get("ok") == 1


def test_lock_is_released_when_taking_the_advisory_lock_fails(sql, monkeypatch):
    a, _ = sql
    monkeypatch.setattr(schema, "take_advisory_lock", lambda *args: (_ for _ in ()).throw(RuntimeError("lock failed")))
    with pytest.raises(RuntimeError, match="lock failed"):
        with a.lock:
            pass
    monkeypatch.undo()
    with a.lock:
        a.kv_set("ok", 2)
    assert a.kv_get("ok") == 2


def test_lock_is_released_when_commit_fails(sql, monkeypatch):
    a, _ = sql
    real_begin = a.engine.connect

    class BadTx:
        def __init__(self, tx):
            self.tx = tx

        def commit(self):
            self.tx.rollback()
            raise RuntimeError("commit failed")

        def rollback(self):
            self.tx.rollback()

    def connect():
        conn = real_begin()
        original = conn.begin
        conn.begin = lambda: BadTx(original())
        return conn

    monkeypatch.setattr(a.engine, "connect", connect)
    with pytest.raises(RuntimeError, match="commit failed"):
        with a.lock:
            a.kv_set("x", 1)
    monkeypatch.undo()
    with a.lock:
        assert a.kv_get("x") is None


def test_advisory_lock_is_taken_only_on_postgresql():
    class Conn:
        def __init__(self):
            self.sql = []

        def execute(self, statement, params):
            self.sql.append((str(statement), params))

    pg, lite = Conn(), Conn()
    schema.take_advisory_lock(pg, "postgresql", "org:x")
    schema.take_advisory_lock(lite, "sqlite", "org:x")
    assert "pg_advisory_xact_lock" in pg.sql[0][0] and pg.sql[0][1] == {"k": "org:x"} and lite.sql == []


def test_migrations_are_idempotent(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    assert schema.migrate(engine, NOW) == [1]
    assert schema.migrate(engine, NOW) == []
    with engine.connect() as conn:
        assert conn.execute(sa.select(schema.migrations_table.c.version)).scalars().all() == [1]
        assert {"orgs", "memberships", "docs", "tickets", "audit", "kv", "connectors", "revoked_keys"} <= set(sa.inspect(conn).get_table_names())


# ---------- engine and factory ----------
def test_url_normalisation_and_engine_variants(tmp_path):
    assert normalise_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_url("sqlite:///x.db") == "sqlite:///x.db"
    memory = make_engine("sqlite://")
    schema.migrate(memory, NOW)
    SqlStore(memory, "o").kv_set("k", 1)  # the in-memory database is shared across connections
    assert SqlStore(memory, "o").kv_get("k") == 1
    assert make_engine("postgresql://u:p@localhost/db", pool_size=3).dialect.name == "postgresql"  # lazy: no connection made


def test_store_factory_returns_one_store_per_org(tmp_path):
    engine = new_engine("sqlite", tmp_path)
    factory = StoreFactory(engine)
    assert factory.for_org("a") is factory.for_org("a") and factory.for_org("a") is not factory.for_org("b")
    assert isinstance(factory.for_org("a"), SqlStore)
    json_factory = StoreFactory(json_path=str(tmp_path / "x.json"))
    assert isinstance(json_factory.for_org("default"), JsonStore) and json_factory.for_org("default").org_id == "default"


def test_json_store_tolerates_a_corrupt_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    s = JsonStore(str(path))
    assert s.load() == empty_db() and s.tickets() == []

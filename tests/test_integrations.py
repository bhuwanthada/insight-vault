"""Connector registry, sync service, worker and the integrations API."""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from backend import orgkeys, schema, secrets_box, worker
from backend.app import create_app
from backend.config import Settings
from backend.connectors import ConnectorAuthError, HttpClient
from backend.connectors.repo import ConnectorRepo, public
from backend.connectors.sync import LEASE_SECONDS, SyncService
from backend.db import make_engine
from backend.secrets_box import SecretBox
from tests.test_connectors import FakeResponse, FakeSession, ZD, ZD_SECRET, zendesk_routes
from tests.test_multitenant import World, bearer, pair  # noqa: F401  (pair is a fixture)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
ZENDESK = {"kind": "zendesk", "name": "Support desk", "config": ZD, "secrets": ZD_SECRET, "intervalMinutes": 30}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def build(tmp_path, **settings_kw):
    """A single-company installation on SQLite with a master key, so connectors can be stored."""
    clock = Clock()
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'c.db'}", secrets_key=secrets_box.generate_key(), **settings_kw)
    app = create_app(settings=settings, clock=clock)
    ext = app.extensions["insight_vault"]
    return app, app.test_client(), ext, clock


def fake_tool(ext, routes=None):
    """Point the sync service at recorded responses instead of the network."""
    session = FakeSession(routes if routes is not None else zendesk_routes())
    ext["sync"].http_factory = lambda: HttpClient(session, sleep=lambda s: None, retries=1)
    return session


# ---------- registry ----------
@pytest.fixture
def repo(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    schema.migrate(engine, NOW.isoformat())
    return ConnectorRepo(engine)


def create(repo, org="acme", **kw):
    params = dict(kind="zendesk", name="Desk", config={"subdomain": "a"}, secrets_enc="enc", mapping={"x": "y"}, interval_minutes=30,
                  enabled=True, now_iso=NOW.isoformat())
    params.update(kw)
    return repo.create(org, params.pop("kind"), params.pop("name"), params.pop("config"), params.pop("secrets_enc"), params.pop("mapping"),
                       params.pop("interval_minutes"), params.pop("enabled"), params.pop("now_iso"))


def test_the_registry_scopes_every_lookup_to_the_organization(repo):
    a, b = create(repo, "acme"), create(repo, "beta", name="Other")
    assert a["config"] == {"subdomain": "a"} and a["mapping"] == {"x": "y"} and a["enabled"] is True and a["last_count"] == 0
    assert repo.get("acme", a["id"])["name"] == "Desk" and repo.get("beta", a["id"]) is None  # another organization's id finds nothing
    assert [c["id"] for c in repo.list("acme")] == [a["id"]] and repo.count("acme") == 1 and repo.count("beta") == 1
    repo.update("beta", a["id"], name="hijacked")  # a cross-organization update changes nothing
    assert repo.get("acme", a["id"])["name"] == "Desk"
    assert repo.delete("beta", a["id"]) is False and repo.delete("acme", a["id"]) is True and repo.get_any(a["id"]) is None
    assert repo.get_any(b["id"])["org_id"] == "beta"


def test_public_view_hides_secrets_and_cursor(repo):
    row = create(repo)
    shown = public(row)
    assert "secrets_enc" not in shown and "cursor" not in shown and "org_id" not in shown and shown["kind"] == "zendesk"


def test_update_stores_json_and_plain_columns(repo):
    row = create(repo)
    repo.update("acme", row["id"], config={"subdomain": "b"}, mapping={}, enabled=False, name="New", cursor="c1")
    now = repo.get("acme", row["id"])
    assert now["config"] == {"subdomain": "b"} and now["mapping"] == {} and now["enabled"] is False and now["name"] == "New" and now["cursor"] == "c1"


def test_workers_claim_a_due_connector_exactly_once(repo):
    row = create(repo)
    first = repo.claim_due(NOW, 900)
    assert first["id"] == row["id"] and repo.claim_due(NOW, 900) is None  # leased: a second worker gets nothing
    assert repo.claim_due(NOW + timedelta(seconds=901), 900)["id"] == row["id"]  # a crashed worker's lease expires


def test_only_due_enabled_connectors_are_claimed(repo):
    disabled = create(repo, enabled=False)
    later = create(repo)
    repo.finish(later["id"], "ok", None, 3, NOW, delay_minutes=60)  # next run in an hour
    assert repo.claim_due(NOW, 900) is None
    claimed = repo.claim_due(NOW + timedelta(minutes=61), 900)
    assert claimed["id"] == later["id"] and disabled["id"] != claimed["id"]


def test_finish_records_the_outcome_releases_the_lease_and_schedules_the_next_run(repo):
    row = create(repo)
    repo.claim_due(NOW, 900)
    repo.finish(row["id"], "error", "boom", 0, NOW, delay_minutes=30)
    done = repo.get("acme", row["id"])
    assert done["last_status"] == "error" and done["last_error"] == "boom" and done["last_count"] == 0 and done["lease_until"] is None
    assert done["next_run_at"] == (NOW + timedelta(minutes=30)).isoformat() and done["last_sync_at"] == NOW.isoformat()
    repo.save_cursor(row["id"], "cur")
    assert repo.get("acme", row["id"])["cursor"] == "cur"


def test_sync_now_takes_the_lease_unless_someone_holds_it(repo):
    row = create(repo)
    assert repo.lease(row["id"], NOW, 900) is True and repo.lease(row["id"], NOW, 900) is False
    assert repo.lease(row["id"], NOW + timedelta(seconds=901), 900) is True


def test_concurrent_workers_never_claim_the_same_connector_twice(repo):
    import threading

    ids = {create(repo, name=f"c{i}")["id"] for i in range(8)}
    claimed, lock = [], threading.Lock()

    def worker_loop():
        while True:
            row = repo.claim_due(NOW, 900)
            if row is None:
                return
            with lock:
                claimed.append(row["id"])

    threads = [threading.Thread(target=worker_loop) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)  # every connector exactly once, however the workers interleave


# ---------- API: no database ----------
def test_integrations_need_a_database(tmp_path):
    client = create_app(str(tmp_path / "db.json"), settings=Settings()).test_client()
    for method, path in (("get", "/api/connectors"), ("post", "/api/connectors"), ("put", "/api/connectors/x"), ("delete", "/api/connectors/x"),
                         ("post", "/api/connectors/x/test"), ("post", "/api/connectors/x/sync")):
        response = getattr(client, method)(path, json={})
        assert response.status_code == 400 and "DATABASE_URL" in response.get_json()["error"], path


# ---------- API: managing connectors ----------
def test_a_connector_is_stored_with_encrypted_credentials_and_never_shown_back(tmp_path):
    app, client, ext, _ = build(tmp_path)
    created = client.post("/api/connectors", json=ZENDESK)
    row = created.get_json()
    assert created.status_code == 201 and row["kind"] == "zendesk" and row["name"] == "Support desk" and row["interval_minutes"] == 30 and row["enabled"] is True
    assert "secrets" not in row and "secrets_enc" not in row and "tok" not in str(row)
    listing = client.get("/api/connectors").get_json()
    assert [c["id"] for c in listing["connectors"]] == [row["id"]] and {k["kind"] for k in listing["kinds"]} == {"zendesk", "freshdesk", "jira", "servicenow", "salesforce"}
    stored = ext["repo"].get_any(row["id"])["secrets_enc"]
    assert stored and "tok" not in stored and ext["tenants"].box.decrypt(stored) == ZD_SECRET  # encrypted at rest
    assert client.get("/api/org").get_json()["usage"]["connectors"] == 1


@pytest.mark.parametrize(
    "payload,expected",
    [({}, "kind must be one of"), ({"kind": "nope"}, "kind must be one of"), ({"kind": "zendesk"}, "missing: subdomain, email, api_token"),
     ({**ZENDESK, "config": {"subdomain": "evil.com", "email": "e"}}, "letters, digits and hyphens"),
     ({"kind": "jira", "config": {"site": "evil.com", "email": "e"}, "secrets": {"api_token": "t"}}, "must be a host ending in"),
     ({**ZENDESK, "mapping": {"bogus": "x"}}, "mapping may only contain"), ({**ZENDESK, "mapping": "x"}, "mapping may only contain"),
     ({**ZENDESK, "mapping": {"resolution_field": 5}}, "mapping may only contain"), ({**ZENDESK, "intervalMinutes": 1}, "intervalMinutes"),
     ({**ZENDESK, "intervalMinutes": True}, "intervalMinutes"), ({**ZENDESK, "intervalMinutes": "60"}, "intervalMinutes")],
)
def test_connector_input_is_validated_before_anything_is_stored(tmp_path, payload, expected):
    _, client, ext, _ = build(tmp_path)
    response = client.post("/api/connectors", json=payload)
    assert response.status_code == 400 and expected in response.get_json()["error"]
    assert ext["repo"].count("default") == 0


def test_unknown_settings_are_dropped_and_names_default_to_the_tool(tmp_path):
    _, client, _, _ = build(tmp_path)
    row = client.post("/api/connectors", json={"kind": "zendesk", "config": {**ZD, "evil": "x"}, "secrets": {**ZD_SECRET, "extra": "y"}, "enabled": False}).get_json()
    assert row["name"] == "Zendesk" and row["config"] == ZD and row["enabled"] is False and row["interval_minutes"] == 60


def test_credentials_cannot_be_stored_without_a_master_key(tmp_path):
    app = create_app(settings=Settings(database_url=f"sqlite:///{tmp_path / 'nokey.db'}"), clock=Clock())
    response = app.test_client().post("/api/connectors", json=ZENDESK)
    assert response.status_code == 503 and "SECRETS_KEY" in response.get_json()["error"]


def test_updating_a_connector_merges_credentials_and_restarts_the_sync_when_the_target_changes(tmp_path):
    _, client, ext, _ = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    ext["repo"].save_cursor(row["id"], "old-cursor")
    renamed = client.put(f"/api/connectors/{row['id']}", json={"name": "Renamed", "enabled": False, "intervalMinutes": 15}).get_json()
    assert renamed["name"] == "Renamed" and renamed["enabled"] is False and renamed["interval_minutes"] == 15
    assert ext["repo"].get_any(row["id"])["cursor"] == "old-cursor"  # a rename does not restart the sync
    changed = client.put(f"/api/connectors/{row['id']}", json={"config": {"subdomain": "other"}, "secrets": {"api_token": "new-token"}, "mapping": {"resolution_field": "9"}})
    assert changed.status_code == 200 and changed.get_json()["config"]["subdomain"] == "other" and changed.get_json()["config"]["email"] == "ops@acme.com"
    stored = ext["repo"].get_any(row["id"])
    assert stored["cursor"] is None and stored["mapping"] == {"resolution_field": "9"} and ext["tenants"].box.decrypt(stored["secrets_enc"]) == {"api_token": "new-token"}
    assert client.put(f"/api/connectors/{row['id']}", json={"name": "   "}).get_json()["name"] == "Renamed"  # a blank name keeps the old one


def test_connector_updates_are_validated_and_unknown_ids_are_404(tmp_path):
    _, client, _, _ = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    assert client.put("/api/connectors/missing", json={}).status_code == 404
    for payload in ({"config": {"subdomain": "evil.com"}}, {"mapping": {"bogus": "x"}}, {"intervalMinutes": 2}):
        assert client.put(f"/api/connectors/{row['id']}", json=payload).status_code == 400
    assert client.delete(f"/api/connectors/{row['id']}").get_json() == {"ok": True}
    assert client.delete(f"/api/connectors/{row['id']}").status_code == 404


def test_updating_credentials_fails_cleanly_if_the_master_key_changed(tmp_path):
    _, client, ext, _ = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    ext["tenants"].box._fernet = SecretBox(secrets_box.generate_key())._fernet  # as if SECRETS_KEY were replaced
    response = client.put(f"/api/connectors/{row['id']}", json={"name": "x"})
    assert response.status_code == 503 and "cannot be decrypted" in response.get_json()["error"]


def test_unreadable_credentials_make_a_connection_test_fail_not_crash(tmp_path):
    _, client, ext, _ = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    ext["tenants"].box._fernet = SecretBox(secrets_box.generate_key())._fernet
    result = client.post(f"/api/connectors/{row['id']}/test").get_json()
    assert result["ok"] is False and "cannot be decrypted" in result["error"]


# ---------- API: testing and syncing ----------
def test_a_connection_test_reports_success_or_the_reason_for_failure(tmp_path):
    _, client, ext, _ = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    fake_tool(ext)
    assert client.post(f"/api/connectors/{row['id']}/test").get_json() == {"ok": True, "detail": "Connected to acme.zendesk.com as Ops Bot"}
    fake_tool(ext, {"/users/me.json": FakeResponse(401)})
    failed = client.post(f"/api/connectors/{row['id']}/test").get_json()
    assert failed["ok"] is False and "authentication failed" in failed["error"]
    assert client.post("/api/connectors/missing/test").status_code == 404


def test_syncing_pulls_tickets_into_the_report_and_continues_from_the_cursor(tmp_path):
    _, client, ext, _ = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    session = fake_tool(ext)
    result = client.post(f"/api/connectors/{row['id']}/sync").get_json()
    assert result == {"status": "ok", "error": None, "fetched": 2, "pages": 2, "more": False}
    assert sorted(t["id"] for t in client.get("/api/tickets").get_json()) == ["zd-1", "zd-3"]
    shown = client.get("/api/connectors").get_json()["connectors"][0]
    assert shown["last_status"] == "ok" and shown["last_count"] == 2 and shown["last_sync_at"] == NOW.isoformat()
    assert [e["action"] for e in client.get("/api/audit").get_json()][0] == "connector_sync" or any(
        e["action"] == "connector_sync" for e in client.get("/api/audit").get_json())
    assert any(e["action"] == "connector_sync" and "ok: 2 tickets" in e["detail"] for e in client.get("/api/audit").get_json())
    assert client.get("/api/report?days=30").get_json()["totalTickets"] == 2  # the synced tickets are in the report straight away
    first = [c for c in session.calls if c["url"].endswith("cursor.json")]
    assert "start_time" in first[0]["params"] and first[1]["params"]["cursor"] == "c1"
    nothing_new = {**zendesk_routes(), "/incremental/tickets/cursor.json": {"tickets": [], "after_cursor": "c2", "end_of_stream": True}}
    resumed = fake_tool(ext, nothing_new)
    again = client.post(f"/api/connectors/{row['id']}/sync").get_json()
    assert again["fetched"] == 0 and again["status"] == "ok"
    assert resumed.calls[0]["params"]["cursor"] == "c2" and "start_time" not in resumed.calls[0]["params"]  # an incremental sync, not a restart


def test_a_sync_that_is_already_running_is_refused(tmp_path):
    _, client, ext, clock = build(tmp_path)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    ext["repo"].lease(row["id"], clock(), LEASE_SECONDS)
    response = client.post(f"/api/connectors/{row['id']}/sync")
    assert response.status_code == 409 and "already running" in response.get_json()["error"]
    assert client.post("/api/connectors/missing/sync").status_code == 404


# ---------- sync service ----------
def service_for(tmp_path, **settings_kw):
    app, client, ext, clock = build(tmp_path, **settings_kw)
    row = client.post("/api/connectors", json=ZENDESK).get_json()
    repo = ext["repo"]
    repo.lease(row["id"], clock(), LEASE_SECONDS)
    return ext["sync"], repo, ext, row["id"], client


def test_progress_is_saved_page_by_page_and_long_syncs_continue_next_run(tmp_path):
    service, repo, ext, cid, _ = service_for(tmp_path)
    fake_tool(ext)
    result = service.run(repo.get_any(cid), max_pages=1)
    assert result["more"] is True and result["pages"] == 1 and result["fetched"] == 1
    saved = repo.get_any(cid)
    assert saved["cursor"] == "c1" and saved["next_run_at"] == NOW.isoformat() and saved["lease_until"] is None  # due again immediately
    repo.lease(cid, NOW, 900)
    finished = service.run(repo.get_any(cid))  # resumes from c1
    assert finished["more"] is False and finished["fetched"] == 1 and repo.get_any(cid)["cursor"] == "c2"
    assert repo.get_any(cid)["next_run_at"] == (NOW + timedelta(minutes=30)).isoformat()


def test_a_rejected_credential_is_recorded_without_losing_earlier_progress(tmp_path):
    service, repo, ext, cid, _ = service_for(tmp_path)
    routes = zendesk_routes()
    routes["/incremental/tickets/cursor.json"] = [routes["/incremental/tickets/cursor.json"][0], FakeResponse(403)]
    fake_tool(ext, routes)
    result = service.run(repo.get_any(cid))
    assert result["status"] == "error" and "authentication failed" in result["error"] and result["fetched"] == 1
    row = repo.get_any(cid)
    assert row["last_status"] == "error" and row["cursor"] == "c1" and ext["factory"].for_org("default").ticket_count() == 1


def test_an_unexpected_vendor_response_never_crashes_the_worker(tmp_path, caplog):
    service, repo, ext, cid, _ = service_for(tmp_path)
    fake_tool(ext, {**zendesk_routes(), "/incremental/tickets/cursor.json": {"tickets": [{"status": "solved", "subject": "x"}]}})  # no id, dates...
    result = service.run(repo.get_any(cid))
    assert result["status"] == "error" and "unexpected response" in result["error"] and "KeyError" in result["error"]
    assert "crashed" in caplog.text and repo.get_any(cid)["lease_until"] is None


def test_the_plan_ticket_limit_stops_a_sync_and_says_why(tmp_path, pair):  # noqa: F811
    token = orgkeys.mint(pair[0], org_id="acme-corp", mode="license", quotas={"tickets": 1}, now=NOW)
    service, repo, ext, cid, _ = service_for(tmp_path, license_key=token, org_key_public_key=pair[1])
    fake_tool(ext)
    result = service.run(repo.get_any(cid))
    assert result["status"] == "error" and "tickets (limit 1)" in result["error"] and result["fetched"] == 1


def test_a_connector_of_a_missing_organization_fails_cleanly(tmp_path):
    service, repo, _, cid, _ = service_for(tmp_path)
    row = repo.get_any(cid)
    result = service.run({**row, "org_id": "ghost"})
    assert result["status"] == "error" and "ghost" in result["error"]


def test_unreadable_secrets_are_recorded_as_a_sync_error(tmp_path):
    service, repo, ext, cid, _ = service_for(tmp_path)
    ext["tenants"].box._fernet = SecretBox(secrets_box.generate_key())._fernet
    assert "cannot be decrypted" in service.run(repo.get_any(cid))["error"]


def test_a_failing_audit_write_does_not_hide_the_sync_result(tmp_path, monkeypatch, caplog):
    service, repo, ext, cid, _ = service_for(tmp_path)
    fake_tool(ext)
    monkeypatch.setattr(type(ext["factory"].for_org("default")), "append_audit", lambda self, entry, keep: (_ for _ in ()).throw(RuntimeError("gone")))
    assert service.run(repo.get_any(cid))["status"] == "ok" and "could not audit" in caplog.text


# ---------- worker ----------
def test_the_worker_syncs_what_is_due_and_idles_otherwise(tmp_path):
    service, repo, ext, cid, _ = service_for(tmp_path)
    repo.finish(cid, "ok", None, 0, NOW, delay_minutes=0)  # release the lease taken by the fixture
    fake_tool(ext)
    summary = worker.run_once(service, repo, lambda: NOW)
    assert summary["status"] == "ok" and summary["fetched"] == 2
    assert worker.run_once(service, repo, lambda: NOW) is None  # nothing else is due


def test_the_worker_loop_sleeps_when_idle_survives_errors_and_stops_on_request():
    class Idle:
        def claim_due(self, now, seconds):
            return None

    class Boom:
        def claim_due(self, now, seconds):
            raise RuntimeError("database hiccup")

    for repo in (Idle(), Boom()):
        sleeps, ticks = [], iter([False, False, True])
        worker.loop(service=None, repo=repo, clock=lambda: NOW, poll_seconds=7, stop=lambda: next(ticks), sleep=sleeps.append)
        assert sleeps == [7, 7]

    class Busy:
        def claim_due(self, now, seconds):
            return {"id": "x"}

    class Service:
        def run(self, row):
            return {"status": "ok"}

    sleeps, ticks = [], iter([False, True])
    worker.loop(Service(), Busy(), lambda: NOW, 7, lambda: next(ticks), sleeps.append)
    assert sleeps == []  # busy workers do not wait


def test_the_worker_needs_a_database_and_wires_everything_up(tmp_path, monkeypatch):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="needs DATABASE_URL"):
        worker.main()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'w.db'}")
    monkeypatch.setenv("WORKER_POLL_SECONDS", "3")
    seen = {}
    monkeypatch.setattr(worker, "loop", lambda service, repo, clock, poll, stop, **kw: seen.update(service=service, poll=poll, stop=stop, before=stop()))
    registered = []
    monkeypatch.setattr(worker.signal, "signal", lambda sig, handler: registered.append((sig, handler)))
    worker.main()
    assert isinstance(seen["service"], SyncService) and seen["poll"] == 3 and seen["before"] is False and len(registered) == 2
    registered[0][1](None, None)  # SIGINT asks the loop to stop
    assert seen["stop"]() is True


# ---------- CSV import API ----------
CSV = ("Ticket ID,Title,Resolution,Assignee,Tools,Created,Resolved\n"
       "A-1,Cannot log in,Reset the password,Asha,admin console,2026-06-01 10:00:00,2026-06-01 12:00:00\n"
       "A-2,,x,x,x,2026-06-01 10:00:00,2026-06-01 12:00:00\n")


def test_csv_import_adds_tickets_reports_skipped_rows_and_is_repeatable(tmp_path):
    client = create_app(str(tmp_path / "db.json"), settings=Settings(), clock=Clock()).test_client()
    response = client.post("/api/tickets/import", json={"csv": CSV})
    assert response.status_code == 201 and response.get_json() == {"imported": 1, "skipped": 1, "skippedRows": [{"row": 3, "reason": "missing subject"}]}
    client.post("/api/tickets/import", json={"csv": CSV})  # importing the same file again never duplicates
    assert [t["id"] for t in client.get("/api/tickets").get_json()] == ["csv-A-1"]
    assert client.get("/api/tickets").get_json()[0]["tools_used"] == ["admin console"]


@pytest.mark.parametrize("payload,expected", [({}, "csv (the file's text) is required"), ({"csv": "  "}, "required"), ({"csv": 5}, "required"),
                                              ({"csv": "a,b\n1,2\n"}, "could not find a column")])
def test_csv_import_rejects_bad_input_clearly(tmp_path, payload, expected):
    client = create_app(str(tmp_path / "db.json"), settings=Settings()).test_client()
    response = client.post("/api/tickets/import", json=payload)
    assert response.status_code == 400 and expected in response.get_json()["error"]


def test_csv_import_respects_the_ticket_limit(tmp_path, pair):  # noqa: F811
    token = orgkeys.mint(pair[0], org_id="acme-corp", mode="license", quotas={"tickets": 1}, now=NOW)
    client = create_app(str(tmp_path / "db.json"), settings=Settings(license_key=token, org_key_public_key=pair[1])).test_client()
    two = CSV.replace(",x,x,x,", ",y,y,y,").replace("A-2,,", "A-2,Second,")
    response = client.post("/api/tickets/import", json={"csv": two})
    assert response.status_code == 402 and "tickets (limit 1)" in response.get_json()["error"]


# ---------- multi-tenant guarantees ----------
def test_one_organization_can_never_see_or_touch_anothers_connectors(tmp_path, pair):  # noqa: F811
    world = World(tmp_path, pair)
    a, b = bearer(world.activate("acme")), bearer(world.activate("beta"))
    mine = world.client.post("/api/connectors", json=ZENDESK, headers=a).get_json()
    assert world.client.get("/api/connectors", headers=a).get_json()["connectors"][0]["id"] == mine["id"]
    assert world.client.get("/api/connectors", headers=b).get_json()["connectors"] == []
    for method, path in (("put", ""), ("delete", ""), ("post", "/test"), ("post", "/sync")):
        assert getattr(world.client, method)(f"/api/connectors/{mine['id']}{path}", json={"name": "x"}, headers=b).status_code == 404, (method, path)
    assert world.client.get("/api/org", headers=a).get_json()["usage"]["connectors"] == 1
    assert world.client.get("/api/org", headers=b).get_json()["usage"]["connectors"] == 0


def test_the_connector_limit_and_the_integrations_feature_come_from_the_plan(tmp_path, pair):  # noqa: F811
    world = World(tmp_path, pair)
    limited = bearer(world.activate("acme", quotas={"connectors": 1}))
    assert world.client.post("/api/connectors", json=ZENDESK, headers=limited).status_code == 201
    over = world.client.post("/api/connectors", json=ZENDESK, headers=limited)
    assert over.status_code == 402 and "connectors (limit 1)" in over.get_json()["error"]
    basic = bearer(world.activate("beta", features=("notes", "support")))
    assert world.client.get("/api/connectors", headers=basic).status_code == 403
    assert world.client.post("/api/tickets/import", json={"csv": CSV}, headers=basic).status_code == 403
    editor = bearer(world.activate("gamma", role="editor"))
    assert world.client.get("/api/connectors", headers=editor).status_code == 403  # connectors are an admin job
    assert world.client.post("/api/tickets/import", json={"csv": CSV}, headers=editor).status_code == 201  # importing is an editor job


def test_connectors_sync_into_the_right_organization_only(tmp_path, pair):  # noqa: F811
    world = World(tmp_path, pair)
    a, b = bearer(world.activate("acme")), bearer(world.activate("beta"))
    mine = world.client.post("/api/connectors", json=ZENDESK, headers=a).get_json()
    fake_tool(world.internals)
    assert world.client.post(f"/api/connectors/{mine['id']}/sync", headers=a).get_json()["fetched"] == 2
    assert len(world.client.get("/api/tickets", headers=a).get_json()) == 2 and world.client.get("/api/tickets", headers=b).get_json() == []
    with world.internals["factory"].for_org("beta").engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(schema.tickets).where(schema.tickets.c.org_id == "beta")).scalar() == 0

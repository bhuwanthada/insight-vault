"""Feature gates, org roles, and a few multi-tenant edge cases."""
import sqlalchemy as sa

from backend.app import create_app
from backend.auth import is_public, required_feature, required_role
from backend.config import Settings
from tests.test_multitenant import World, bearer, pair  # noqa: F401  (pair is a fixture)
from tests.dburl import db_url


def test_required_feature_maps_paths_to_plan_features():
    cases = {
        "/api/insights/suggest": "ai_suggestions", "/api/insights": "insights", "/api/insights/abc": "insights",
        "/api/search": "search", "/api/search?q=x": "search", "/api/connectors": "integrations",
        "/api/connectors/1/sync": "integrations", "/api/tickets/import": "integrations", "/api/report": "support",
        "/api/reports": "support", "/api/reports/1/submit": "support", "/api/tickets": "support",
        "/api/tickets/sample": "support", "/api/notes": None, "/api/state": None, "/api/org": None,
    }
    for path, feature in cases.items():
        assert required_feature(path) == feature, path


def test_org_endpoints_need_the_right_roles():
    assert required_role("GET", "/api/org") == "viewer" and required_role("POST", "/api/org/switch") == "viewer"
    admin_only = (("GET", "/api/org/settings"), ("PUT", "/api/org/settings"), ("PUT", "/api/org/secrets"),
                  ("GET", "/api/org/members"), ("POST", "/api/org/members"), ("DELETE", "/api/org/members/a@b.co"),
                  ("GET", "/api/org/export"), ("DELETE", "/api/org"), ("GET", "/api/connectors"),
                  ("POST", "/api/connectors/abc/sync"))
    for method, path in admin_only:
        assert required_role(method, path) == "admin", (method, path)
    assert is_public("/api/org/activate") and not is_public("/api/org")


def test_a_single_company_install_can_use_a_database_and_survives_restarts(tmp_path):
    url = db_url(tmp_path, 'company')
    first = create_app(settings=Settings(database_url=url)).test_client()
    first.post("/api/notes", json={"title": "kept", "text": "across restarts"})
    first.post("/api/tickets/sample")
    assert first.get("/api/report?days=30").get_json()["totalTickets"] == 21
    restarted = create_app(settings=Settings(database_url=url)).test_client()  # a new process on the same database
    assert restarted.get("/api/state").get_json()["notes"][0]["title"] == "kept"
    assert len(restarted.get("/api/tickets").get_json()) == 30
    assert restarted.get("/api/ready").get_json() == {"status": "ready"}


def test_plain_api_keys_do_not_work_in_multi_tenant_mode(tmp_path, pair):  # noqa: F811
    world = World(tmp_path, pair)
    world.activate("acme")
    assert world.client.get("/api/state", headers=bearer("a-plain-static-key-value")).status_code == 401


def test_readiness_reports_database_problems(tmp_path, pair, monkeypatch):  # noqa: F811
    world = World(tmp_path, pair)
    store = world.internals["factory"].for_org(world.internals["tenants"].platform.org_id)
    assert world.client.get("/api/ready").get_json() == {"status": "ready"}
    down = sa.exc.OperationalError("SELECT 1", {}, Exception("db down"))
    monkeypatch.setattr(type(store), "check", lambda self: (_ for _ in ()).throw(down))
    response = world.client.get("/api/ready")
    assert response.status_code == 503 and "unavailable" in response.get_json()["error"]

"""Multi-tenant behaviour over HTTP: onboarding by key, isolation, plans, expiry, suspension, platform API."""
from datetime import datetime, timedelta, timezone

import pytest

from backend import orgkeys, secrets_box
from backend.app import create_app
from backend.config import ConfigError, Settings
from backend.db import make_engine
from tests.conftest import FakeLLM
from tests.test_auth_app import FakeOAuth, sign_in

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
PLATFORM = {"X-Platform-Key": "p" * 16}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def pair():
    return orgkeys.generate_keypair()


class World:
    """A running multi-tenant installation plus helpers to mint keys and act as organizations."""

    def __init__(self, tmp_path, pair, **over):
        self.pair, self.clock, self.idp = pair, Clock(), FakeOAuth()
        self.llm = FakeLLM(lambda *a: {"category": "Performance"})
        settings = Settings(
            deployment_mode="multi_tenant", auth_mode="sso", secret_key="s" * 32, public_url="http://localhost:3000",
            google_client_id="g", google_client_secret="s", secrets_key=secrets_box.generate_key(),
            org_key_public_key=pair[1], platform_admin_keys=(("ops", "p" * 16),), **over)
        self.app = create_app(settings=settings, engine=make_engine(f"sqlite:///{tmp_path / 'mt.db'}"), llm=self.llm,
                              clock=self.clock, oauth=self.idp)
        self.client = self.app.test_client()
        self.internals = self.app.extensions["insight_vault"]

    def key(self, org="acme", **kw):
        params = dict(org_id=org, org_name=org.title(), now=NOW, role="admin", admins=[f"boss@{org}.com"], days=365)
        params.update(kw)
        return orgkeys.mint(self.pair[0], **params)

    def activate(self, org="acme", **kw):
        token = self.key(org, **kw)
        response = self.client.post("/api/org/activate", json={"key": token})
        assert response.status_code in (200, 201), response.get_json()
        return token

    def browser(self, email, name="Person"):
        """A separate browser that has signed in through the identity provider."""
        browser = self.app.test_client()
        assert sign_in(browser, self.idp, email, name).headers["Location"] == "http://localhost:3000/"
        return browser


@pytest.fixture
def world(tmp_path, pair):
    return World(tmp_path, pair)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- onboarding ----------
def test_activating_a_key_creates_the_organization_and_its_admins(world):
    token = world.key(admins=["boss@acme.com"], plan="growth", quotas={"seats": 3})
    first = world.client.post("/api/org/activate", json={"key": token})
    assert first.status_code == 201
    body = first.get_json()
    assert body["created"] is True and body["org"]["id"] == "acme" and body["org"]["plan"] == "growth"
    assert body["org"]["quotas"] == {"seats": 3} and body["admins"] == ["boss@acme.com"] and body["org"]["expiresAt"].startswith("2027")
    again = world.client.post("/api/org/activate", json={"key": token})
    assert again.status_code == 200 and again.get_json()["created"] is False  # idempotent, safe to repeat


@pytest.mark.parametrize("payload,expected", [({}, "invalid key"), ({"key": "forged.key.value"}, "invalid key")])
def test_activation_rejects_bad_keys(world, payload, expected):
    response = world.client.post("/api/org/activate", json=payload)
    assert response.status_code == 400 and expected in response.get_json()["error"]


def test_a_license_key_cannot_activate_a_saas_organization(world):
    response = world.client.post("/api/org/activate", json={"key": world.key(mode="license")})
    assert response.status_code == 400 and "license key" in response.get_json()["error"]


def test_a_revoked_key_cannot_activate(world):
    token = world.key()
    jti = orgkeys.inspect(token)["jti"]
    assert world.client.post("/api/platform/keys/revoke", json={"jti": jti, "reason": "leaked"}, headers=PLATFORM).status_code == 200
    assert "revoked" in world.client.post("/api/org/activate", json={"key": token}).get_json()["error"]


def test_the_activated_admin_can_sign_in_and_strangers_cannot(world):
    world.activate()
    boss = world.browser("boss@acme.com", "Boss")
    me = boss.get("/api/auth/me").get_json()
    assert me["user"]["role"] == "admin" and me["user"]["org"] == "acme" and me["user"]["orgs"] == ["acme"]
    stranger = world.app.test_client()
    assert sign_in(stranger, world.idp, "nobody@acme.com").headers["Location"].endswith("?login_error=denied")  # deny by default
    assert stranger.get("/api/state").status_code == 401


# ---------- keys as API credentials ----------
def test_an_org_key_acts_as_that_organization_with_the_role_in_the_key(world):
    editor = world.activate("acme", role="editor")
    viewer = world.key("acme", role="viewer")
    assert world.client.post("/api/notes", json={"title": "t", "text": "hello"}, headers=bearer(editor)).status_code == 201
    assert world.client.get("/api/state", headers=bearer(viewer)).get_json()["notes"][0]["title"] == "t"
    denied = world.client.post("/api/notes", json={"title": "t", "text": "x"}, headers=bearer(viewer))
    assert denied.status_code == 403 and "editor role" in denied.get_json()["error"]
    assert world.client.get("/api/state", headers=bearer("tampered." + editor.split(".", 1)[1])).status_code == 401
    me = world.client.get("/api/auth/me", headers=bearer(editor)).get_json()["user"]
    assert me["provider"] == "orgkey" and me["org"] == "acme" and me["role"] == "editor"


def test_a_valid_key_for_an_organization_that_was_never_activated_is_refused(world):
    never = world.key("ghost")
    response = world.client.get("/api/state", headers=bearer(never))
    assert response.status_code == 403 and response.get_json()["error"] == "organization not found"
    assert world.client.get("/api/config/public", headers=bearer(never)).get_json()["appName"] == "Insight Vault"  # platform defaults


# ---------- isolation ----------
def test_organizations_never_see_each_others_data(world):
    a, b = world.activate("acme"), world.activate("beta")
    for token, label in ((a, "ACME"), (b, "BETA")):
        h = bearer(token)
        note = world.client.post("/api/notes", json={"title": f"{label} note", "text": f"{label} secret text"}, headers=h).get_json()
        world.client.post("/api/highlights", json={"noteId": note["id"], "start": 0, "end": 4, "tags": [label.lower()]}, headers=h)
        world.client.post("/api/tickets", json={"id": "T-1", "subject": f"{label} ticket", "createdAt": "2026-06-14T00:00:00Z",
                                                "resolvedAt": "2026-06-14T01:00:00Z", "resolution": "Restarted."}, headers=h)
        world.client.post("/api/reports", json={"days": 30, "title": f"{label} report"}, headers=h)
    for token, mine, theirs in ((a, "ACME", "BETA"), (b, "BETA", "ACME")):
        h = bearer(token)
        everything = (world.client.get("/api/state", headers=h).get_data(as_text=True)
                      + world.client.get("/api/tickets", headers=h).get_data(as_text=True)
                      + world.client.get("/api/reports", headers=h).get_data(as_text=True)
                      + world.client.get("/api/search?q=secret", headers=h).get_data(as_text=True)
                      + world.client.get("/api/org/export", headers=h).get_data(as_text=True)
                      + world.client.get("/api/audit", headers=h).get_data(as_text=True))
        assert mine in everything and theirs not in everything, f"{mine} can see {theirs}"
        assert world.client.get("/api/tickets", headers=h).get_json()[0]["subject"] == f"{mine} ticket"  # same ticket id in both orgs


def test_audit_trails_are_per_organization(world):
    a, b = world.activate("acme"), world.activate("beta")
    world.client.post("/api/notes", json={"title": "t", "text": "x"}, headers=bearer(a))
    assert [e["action"] for e in world.client.get("/api/audit", headers=bearer(a)).get_json()][0] == "POST /api/notes"
    assert all(e["action"] != "POST /api/notes" for e in world.client.get("/api/audit", headers=bearer(b)).get_json())


def test_a_browser_session_cannot_reach_an_organization_it_is_not_in(world):
    world.activate("acme"), world.activate("beta")
    boss = world.browser("boss@acme.com")
    assert boss.post("/api/org/switch", json={"org": "beta"}).status_code == 403
    assert boss.post("/api/org/switch", json={}).status_code == 403
    assert boss.get("/api/state").status_code == 200


def test_people_in_two_organizations_can_switch_between_them(world):
    world.activate("acme", admins=["pat@both.com"]), world.activate("beta", admins=["pat@both.com"])
    pat = world.browser("pat@both.com")
    assert pat.get("/api/auth/me").get_json()["user"]["orgs"] == ["acme", "beta"] and pat.get("/api/org").get_json()["id"] == "acme"
    pat.post("/api/notes", json={"title": "in acme", "text": "x"})
    assert pat.post("/api/org/switch", json={"org": "beta"}).get_json() == {"ok": True, "org": "beta"}
    assert pat.get("/api/org").get_json()["id"] == "beta" and pat.get("/api/state").get_json()["notes"] == []


# ---------- plans ----------
def test_features_come_from_the_plan_in_the_key(world):
    token = world.activate("acme", features=("notes", "insights"))
    h = bearer(token)
    assert world.client.get("/api/state", headers=h).status_code == 200 and world.client.get("/api/insights/suggest", headers=h).status_code != 200
    for method, path in (("get", "/api/search?q=x"), ("get", "/api/report"), ("get", "/api/tickets"), ("post", "/api/reports"),
                         ("post", "/api/insights/suggest"), ("get", "/api/connectors"), ("post", "/api/tickets/import")):
        response = getattr(world.client, method)(path, headers=h)
        assert response.status_code == 403 and "not included" in response.get_json()["error"], path
    assert world.client.post("/api/insights", json={"title": "ok"}, headers=h).status_code == 201
    assert world.client.get("/api/config/public", headers=h).get_json()["features"] == ["notes", "insights"]


def test_the_ticket_limit_counts_new_tickets_only(world):
    h = bearer(world.activate("acme", quotas={"tickets": 2}))
    ticket = lambda i, **kw: {"id": f"T{i}", "subject": "s", "createdAt": "2026-06-14T00:00:00Z", "resolvedAt": "2026-06-14T01:00:00Z", **kw}
    assert world.client.post("/api/tickets", json=[ticket(1), ticket(2)], headers=h).status_code == 201
    assert world.client.post("/api/tickets", json=[ticket(1, subject="edited")], headers=h).status_code == 201  # an update is free
    over = world.client.post("/api/tickets", json=[ticket(3)], headers=h)
    assert over.status_code == 402 and "tickets (limit 2)" in over.get_json()["error"]
    assert len(world.client.get("/api/tickets", headers=h).get_json()) == 2
    assert world.client.get("/api/org", headers=h).get_json()["usage"]["tickets"] == 2


def test_seat_limits_apply_to_new_members_only(world):
    world.activate("acme", quotas={"seats": 2}, admins=["boss@acme.com"])
    boss = world.browser("boss@acme.com")
    assert boss.post("/api/org/members", json={"email": "a@acme.com", "role": "editor"}).status_code == 201
    assert boss.post("/api/org/members", json={"email": "a@acme.com", "role": "viewer"}).status_code == 201  # change, not a new seat
    full = boss.post("/api/org/members", json={"email": "b@acme.com", "role": "viewer"})
    assert full.status_code == 402 and "seats (limit 2)" in full.get_json()["error"]


# ---------- expiry, suspension, revocation ----------
def test_when_the_key_expires_the_organization_becomes_read_only(world):
    token = world.activate("acme", days=1, role="editor")
    boss = world.browser("boss@acme.com")
    boss.post("/api/notes", json={"title": "kept", "text": "x"})
    world.clock.t = NOW + timedelta(days=2)
    assert world.client.get("/api/state", headers=bearer(token)).status_code == 401  # the key itself no longer works
    assert boss.get("/api/state").get_json()["notes"][0]["title"] == "kept"  # but people can still read and export
    assert boss.get("/api/org/export").status_code == 200
    blocked = boss.post("/api/notes", json={"title": "new", "text": "x"})
    assert blocked.status_code == 402 and "read-only" in blocked.get_json()["error"]
    assert boss.get("/api/org").get_json()["readOnly"] is True
    renewed = world.key("acme", now=world.clock.t, days=30)  # a renewal key restores write access
    world.client.post("/api/org/activate", json={"key": renewed})
    world.internals["tenants"].invalidate("acme")
    assert boss.post("/api/notes", json={"title": "new", "text": "x"}).status_code == 201


def test_suspending_an_organization_locks_everyone_out_until_restored(world):
    token = world.activate("acme")
    boss = world.browser("boss@acme.com")
    assert world.client.post("/api/platform/orgs/acme/status", json={"status": "suspended"}, headers=PLATFORM).status_code == 200
    blocked = world.client.get("/api/state", headers=bearer(token))
    assert blocked.status_code == 403 and "suspended" in blocked.get_json()["error"]
    assert boss.get("/api/state").status_code == 401  # a suspended organization has no active memberships
    world.client.post("/api/platform/orgs/acme/status", json={"status": "active"}, headers=PLATFORM)
    assert world.client.get("/api/state", headers=bearer(token)).status_code == 200 and boss.get("/api/state").status_code == 200


def test_revoking_a_key_takes_effect_immediately(world):
    token = world.activate("acme")
    assert world.client.get("/api/state", headers=bearer(token)).status_code == 200
    world.client.post("/api/platform/keys/revoke", json={"jti": orgkeys.inspect(token)["jti"]}, headers=PLATFORM)
    assert world.client.get("/api/state", headers=bearer(token)).status_code == 401


# ---------- platform operator API ----------
def test_the_platform_api_needs_the_operator_key(world):
    world.activate("acme")
    assert world.client.get("/api/platform/orgs").status_code == 401
    assert world.client.get("/api/platform/orgs", headers={"X-Platform-Key": "wrong" * 4}).status_code == 401
    assert world.client.get("/api/platform/orgs", headers=bearer(world.key("acme"))).status_code == 401  # a tenant key is not an operator key
    listed = world.client.get("/api/platform/orgs", headers=PLATFORM).get_json()
    assert listed[0]["id"] == "acme" and listed[0]["status"] == "active" and listed[0]["members"] == 1 and listed[0]["keyExpiresAt"]


def test_platform_input_validation(world):
    world.activate("acme")
    assert world.client.post("/api/platform/orgs/acme/status", json={"status": "gone"}, headers=PLATFORM).status_code == 400
    assert world.client.post("/api/platform/orgs/ghost/status", json={"status": "active"}, headers=PLATFORM).status_code == 404
    assert world.client.post("/api/platform/keys/revoke", json={}, headers=PLATFORM).status_code == 400
    assert world.client.delete("/api/platform/orgs/ghost", json={"confirm": "ghost"}, headers=PLATFORM).status_code == 404
    assert world.client.delete("/api/platform/orgs/acme", json={"confirm": "nope"}, headers=PLATFORM).status_code == 400


def test_the_operator_can_delete_an_organization(world):
    token = world.activate("acme")
    world.client.post("/api/notes", json={"title": "t", "text": "x"}, headers=bearer(token))
    assert world.client.delete("/api/platform/orgs/acme", json={"confirm": "acme"}, headers=PLATFORM).get_json() == {"deleted": "acme"}
    assert world.client.get("/api/state", headers=bearer(token)).status_code == 403
    assert world.client.get("/api/platform/orgs", headers=PLATFORM).get_json() == []


def test_single_organization_mode_has_no_platform_or_multi_tenant_endpoints(tmp_path):
    client = create_app(str(tmp_path / "db.json"), settings=Settings()).test_client()
    assert client.get("/api/platform/orgs", headers=PLATFORM).status_code == 404
    assert client.post("/api/org/activate", json={"key": "x"}).status_code == 404
    for method, path in (("get", "/api/org/settings"), ("put", "/api/org/settings"), ("put", "/api/org/secrets"),
                         ("get", "/api/org/members"), ("post", "/api/org/members"), ("delete", "/api/org/members/a@b.co"),
                         ("delete", "/api/org")):
        assert getattr(client, method)(path, json={}).status_code == 404, path
    assert client.get("/api/org").get_json()["deploymentMode"] == "single_org"


# ---------- organization settings and secrets ----------
def test_admins_edit_their_organizations_settings(world):
    world.activate("acme", admins=["boss@acme.com"])
    world.activate("beta", admins=["boss@beta.com"])
    boss = world.browser("boss@acme.com")
    assert boss.put("/api/org/settings", json={"app_name": "Acme Vault", "accent_color": "#112233", "votes": 5}).get_json() == {"ok": True}
    shown = boss.get("/api/org/settings").get_json()
    assert shown["overrides"]["app_name"] == "Acme Vault" and shown["effective"]["votes"] == 5 and "rules_yaml" in shown["editable"]
    assert boss.get("/api/config/public").get_json()["appName"] == "Acme Vault"
    assert world.browser("boss@beta.com").get("/api/config/public").get_json()["appName"] == "Beta"  # another org is unaffected
    assert world.client.get("/api/config/public").get_json()["appName"] == "Insight Vault"  # so is the signed-out view


def test_invalid_organization_settings_are_rejected_with_reasons(world):
    world.activate("acme")
    boss = world.browser("boss@acme.com")
    for payload, expected in (({"port": 1}, "cannot be changed"), ({"votes": "x"}, "whole number"), ({"default_days": 31}, "REPORT_DEFAULT_DAYS"),
                              ({"rules_yaml": "categories: {}"}, "Invalid rules file")):
        response = boss.put("/api/org/settings", json=payload)
        assert response.status_code == 400 and expected in response.get_json()["error"] and response.get_json()["problems"]


def test_organization_rules_change_how_that_organizations_tickets_are_diagnosed(world):
    world.activate("acme")
    boss = world.browser("boss@acme.com")
    boss.put("/api/org/settings", json={"rules_yaml": "uncategorised: Misc\ncategories:\n  Printer trouble: [printer, toner]\n"})
    boss.post("/api/tickets", json={"id": "T1", "subject": "printer out of toner", "createdAt": "2026-06-14T00:00:00Z", "resolvedAt": "2026-06-14T01:00:00Z"})
    categories = [c["category"] for c in boss.get("/api/report?days=7").get_json()["categories"]]
    assert categories == ["Printer trouble"]


def test_each_organization_manages_its_own_llm_key_without_ever_reading_it_back(world):
    world.activate("acme")
    boss = world.browser("boss@acme.com")
    assert boss.get("/api/org/settings").get_json()["hasLlmKey"] is False
    assert boss.put("/api/org/secrets", json={"llm_api_key": " sk-secret "}).get_json() == {"llm_api_key": "set"}
    assert boss.get("/api/org/settings").get_json()["hasLlmKey"] is True
    assert "sk-secret" not in boss.get("/api/config").get_data(as_text=True) + boss.get("/api/org/settings").get_data(as_text=True)
    assert world.internals["tenants"].context("acme").settings.llm_api_key == "sk-secret"
    assert boss.put("/api/org/secrets", json={"llm_api_key": ""}).get_json() == {"llm_api_key": "not set"}
    assert boss.put("/api/org/secrets", json={}).status_code == 400 and boss.put("/api/org/secrets", json={"llm_api_key": 5}).status_code == 400


def test_secrets_cannot_be_stored_without_a_master_key(tmp_path, pair):
    world = World(tmp_path, pair)
    world.internals["tenants"].box._fernet = None  # as if SECRETS_KEY were removed
    world.activate("acme")
    response = world.browser("boss@acme.com").put("/api/org/secrets", json={"llm_api_key": "sk-1"})
    assert response.status_code == 503 and "SECRETS_KEY" in response.get_json()["error"]


# ---------- members ----------
def test_members_are_managed_by_organization_admins(world):
    world.activate("acme", admins=["boss@acme.com"])
    boss = world.browser("boss@acme.com")
    assert boss.post("/api/org/members", json={"email": "New@Acme.com", "role": "editor"}).get_json() == {"email": "new@acme.com", "role": "editor"}
    assert boss.get("/api/org/members").get_json() == [{"email": "boss@acme.com", "role": "admin"}, {"email": "new@acme.com", "role": "editor"}]
    newbie = world.browser("new@acme.com")  # invited people can now sign in
    assert newbie.get("/api/auth/me").get_json()["user"]["role"] == "editor" and newbie.get("/api/org/members").status_code == 403
    assert boss.post("/api/org/members", json={"email": "not-an-email", "role": "editor"}).status_code == 400
    assert boss.post("/api/org/members", json={"email": "x@acme.com", "role": "owner"}).status_code == 400
    assert boss.delete("/api/org/members/new@acme.com").get_json() == {"ok": True}
    assert boss.delete("/api/org/members/new@acme.com").status_code == 404
    last = boss.delete("/api/org/members/boss@acme.com")
    assert last.status_code == 409 and "at least one administrator" in last.get_json()["error"]


# ---------- information, export, deletion ----------
def test_org_info_reports_plan_usage_and_expiry(world):
    token = world.activate("acme", plan="growth", quotas={"tickets": 10, "seats": 4})
    info = world.client.get("/api/org", headers=bearer(token)).get_json()
    assert info["id"] == "acme" and info["name"] == "Acme" and info["plan"] == "growth" and info["status"] == "active"
    assert info["quotas"] == {"tickets": 10, "seats": 4} and info["usage"] == {"tickets": 0, "seats": 1, "tokensToday": 0, "connectors": 0}
    assert info["deploymentMode"] == "multi_tenant" and info["readOnly"] is False and info["expiresAt"].startswith("2027")


def test_an_admin_can_export_everything_and_delete_the_organization(world):
    token = world.activate("acme", admins=["boss@acme.com"])
    boss = world.browser("boss@acme.com")
    boss.post("/api/notes", json={"title": "mine", "text": "important"})
    export = boss.get("/api/org/export")
    assert export.headers["Content-Disposition"].endswith('insight-vault-export.json"')
    data = export.get_json()
    assert data["org"]["id"] == "acme" and data["notes"][0]["title"] == "mine" and data["members"][0]["email"] == "boss@acme.com"
    assert boss.delete("/api/org", json={"confirm": "wrong"}).status_code == 400
    assert boss.delete("/api/org", json={"confirm": "acme"}).get_json() == {"deleted": True}
    assert boss.get("/api/state").status_code == 401 and world.client.get("/api/state", headers=bearer(token)).status_code == 403


# ---------- the LLM is per organization too ----------
def test_ticket_classification_uses_the_calling_organizations_llm_and_cache(world):
    h = bearer(world.activate("acme"))
    world.client.post("/api/tickets", json={"id": "T1", "subject": "Strange behaviour", "description": "odd", "createdAt": "2026-06-14T00:00:00Z",
                                            "resolvedAt": "2026-06-14T01:00:00Z"}, headers=h)
    assert world.client.get("/api/report?days=7", headers=h).get_json()["categories"][0]["category"] == "Performance"
    assert len(world.llm.calls) == 3


# ---------- startup checks and licenses (single organization) ----------
def licensed(tmp_path, pair, claims_kw=None, **settings_kw):
    token = orgkeys.mint(pair[0], org_id="acme-corp", org_name="Acme Corp", mode="license", now=NOW, **(claims_kw or {}))
    settings = Settings(license_key=token, org_key_public_key=pair[1], **settings_kw)
    return create_app(str(tmp_path / "db.json"), settings=settings, clock=Clock())


def test_a_license_defines_the_organization_features_and_quotas(tmp_path, pair):
    client = licensed(tmp_path, pair, {"features": ("notes", "support"), "quotas": {"tickets": 1}, "plan": "enterprise"}).test_client()
    info = client.get("/api/org").get_json()
    assert info["id"] == "acme-corp" and info["name"] == "Acme Corp" and info["plan"] == "enterprise" and info["deploymentMode"] == "single_org"
    assert client.get("/api/search?q=x").status_code == 403 and client.get("/api/config/public").get_json()["appName"] == "Acme Corp"
    ticket = {"id": "A", "subject": "s", "createdAt": "2026-06-14T00:00:00Z", "resolvedAt": "2026-06-14T01:00:00Z"}
    assert client.post("/api/tickets", json=ticket).status_code == 201
    assert client.post("/api/tickets", json={**ticket, "id": "B"}).status_code == 402


def test_an_expired_license_still_starts_read_only(tmp_path, pair):
    app = licensed(tmp_path, pair, {"days": 1})
    clock = app.extensions["insight_vault"]["tenants"].clock
    client = app.test_client()
    assert client.post("/api/notes", json={"title": "t", "text": "x"}).status_code == 201
    clock.t = NOW + timedelta(days=5)
    assert client.get("/api/state").status_code == 200 and client.get("/api/org/export").status_code == 200
    assert client.post("/api/notes", json={"title": "t", "text": "x"}).status_code == 402


def test_bad_licenses_stop_startup_with_clear_messages(tmp_path, pair):
    other = orgkeys.generate_keypair()
    forged = orgkeys.mint(other[0], org_id="acme-corp", mode="license", now=NOW)
    with pytest.raises(ConfigError, match="LICENSE_KEY is not valid"):
        create_app(str(tmp_path / "a.json"), settings=Settings(license_key=forged, org_key_public_key=pair[1]))
    tenant = orgkeys.mint(pair[0], org_id="acme-corp", mode="tenant", now=NOW)
    with pytest.raises(ConfigError, match="must be a license key"):
        create_app(str(tmp_path / "b.json"), settings=Settings(license_key=tenant, org_key_public_key=pair[1]))
    right = orgkeys.mint(pair[0], org_id="acme-corp", mode="license", now=NOW)
    with pytest.raises(ConfigError, match="does not match the license"):
        create_app(str(tmp_path / "c.json"), settings=Settings(license_key=right, org_key_public_key=pair[1], org_id="someone-else"))


def test_a_license_may_name_the_organization_explicitly(tmp_path, pair):
    right = orgkeys.mint(pair[0], org_id="acme-corp", mode="license", now=NOW)
    app = create_app(str(tmp_path / "d.json"), settings=Settings(license_key=right, org_key_public_key=pair[1], org_id="acme-corp"))
    assert app.test_client().get("/api/org").get_json()["id"] == "acme-corp"


def test_license_administrators_are_added_to_the_installation(tmp_path, pair):
    idp = FakeOAuth()
    app = create_app(str(tmp_path / "e.json"), oauth=idp, settings=Settings(
        license_key=orgkeys.mint(pair[0], org_id="acme-corp", mode="license", admins=["cto@acme-corp.com"], now=NOW),
        org_key_public_key=pair[1], auth_mode="sso", secret_key="s" * 32, google_client_id="g", google_client_secret="s",
        public_url="http://localhost:3000"), clock=Clock())
    cto = app.test_client()
    sign_in(cto, idp, "cto@acme-corp.com")
    assert cto.get("/api/auth/me").get_json()["user"]["role"] == "admin" and cto.get("/api/org").get_json()["id"] == "acme-corp"

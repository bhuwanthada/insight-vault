import dataclasses

import pytest
from flask import Flask, redirect

from backend import app as app_module
from backend import sso
from backend.auth import AccessPolicy, Authenticator, has_role, is_public, local_user, required_role
from backend.config import Settings
from tests.conftest import FakeLLM

KEY_ADMIN, KEY_ADMIN2, KEY_EDITOR, KEY_VIEWER = "a" * 16, "b" * 16, "e" * 16, "v" * 16
APIKEY = dict(auth_mode="apikey", api_keys=(("boss", "admin", KEY_ADMIN), ("boss2", "admin", KEY_ADMIN2),
                                             ("bot", "editor", KEY_EDITOR), ("reader", "viewer", KEY_VIEWER)))
SSO = dict(
    auth_mode="sso", secret_key="s" * 32, public_url="http://localhost:3000",
    google_client_id="gid", google_client_secret="gsecret",
    microsoft_client_id="mid", microsoft_client_secret="msecret", microsoft_tenant="contoso.com",
    allowed_domains=("corp.com",), admin_emails=("boss@corp.com",), approver_emails=("lead@corp.com",),
    editor_emails=("ed@corp.com",),
)
HEAD = lambda key: {"Authorization": f"Bearer {key}"}


# ---------- pure helpers ----------
def test_required_role_matrix():
    assert required_role("GET", "/api/state") == "viewer" and required_role("OPTIONS", "/api/x") == "viewer"
    assert required_role("POST", "/api/notes") == "editor" and required_role("DELETE", "/api/notes/1") == "editor"
    assert required_role("POST", "/api/reports/abc/decision") == "approver"
    assert required_role("POST", "/api/reports/abc/submit") == "editor"
    for path in ("/api/config", "/api/audit", "/api/llm/test"):
        assert required_role("GET", path) == "admin"


def test_roles_and_public_paths():
    assert has_role({"role": "approver"}, "editor") and not has_role({"role": "viewer"}, "editor")
    assert has_role(local_user(), "admin")
    assert is_public("/api/health") and is_public("/api/auth/me") and is_public("/api/config/public")
    assert not is_public("/api/state") and not is_public("/api/config")


def test_access_policy_is_deny_by_default():
    p = AccessPolicy(Settings(**SSO))
    assert p.allowed("anyone@corp.com") and p.allowed("boss@corp.com") and not p.allowed("x@other.com") and not p.allowed("")
    assert AccessPolicy(Settings(auth_mode="sso")).allowed("a@b.com") is False
    assert AccessPolicy(Settings(allowed_emails=("solo@x.com",))).allowed("solo@x.com")
    assert [p.role(e) for e in ("boss@corp.com", "lead@corp.com", "ed@corp.com", "new@corp.com")] == ["admin", "approver", "editor", "viewer"]


def test_authenticator_modes():
    assert Authenticator(Settings()).authenticate({}, {})["role"] == "admin"  # none: single local user
    keys = Authenticator(Settings(**APIKEY))
    assert keys.authenticate(HEAD(KEY_EDITOR), {})["name"] == "bot"
    assert keys.authenticate({"X-API-Key": KEY_VIEWER}, {})["role"] == "viewer"
    assert keys.authenticate({}, {}) is None and keys.authenticate(HEAD("wrong" * 4), {}) is None
    assert keys.authenticate({}, {"user": {"email": "boss@corp.com"}}) is None  # sessions are ignored outside sso mode
    sso_auth = Authenticator(Settings(**SSO))
    stored = {"email": "lead@corp.com", "name": "Lead", "provider": "google", "role": "admin"}  # a stale role in the cookie is ignored
    assert sso_auth.authenticate({}, {"user": stored})["role"] == "approver"
    assert sso_auth.authenticate({}, {"user": {"email": "gone@other.com", "name": "G", "provider": "google"}}) is None
    assert sso_auth.authenticate({}, {}) is None


# ---------- API keys over HTTP ----------
@pytest.fixture
def keyed(make_client):
    return make_client(settings=Settings(**APIKEY), llm=FakeLLM(None))


def test_requests_without_valid_keys_are_rejected(keyed):
    assert keyed.get("/api/state").status_code == 401
    assert keyed.get("/api/state", headers=HEAD("nope" * 5)).get_json() == {"error": "authentication required"}
    assert keyed.get("/api/health").status_code == 200  # liveness stays public
    assert keyed.get("/api/config/public").status_code == 200


def test_roles_are_enforced_per_endpoint(keyed):
    assert keyed.get("/api/state", headers=HEAD(KEY_VIEWER)).status_code == 200
    denied = keyed.post("/api/notes", json={"title": "t", "text": "x"}, headers=HEAD(KEY_VIEWER))
    assert denied.status_code == 403 and "requires the editor role" in denied.get_json()["error"]
    assert keyed.post("/api/notes", json={"title": "t", "text": "x"}, headers=HEAD(KEY_EDITOR)).status_code == 201
    assert keyed.get("/api/config", headers=HEAD(KEY_EDITOR)).status_code == 403
    assert keyed.get("/api/config", headers=HEAD(KEY_ADMIN)).status_code == 200
    assert keyed.post("/api/llm/test", headers=HEAD(KEY_EDITOR)).status_code == 403
    assert keyed.post("/api/llm/test", headers=HEAD(KEY_ADMIN)).get_json()["ok"] is True
    me = keyed.get("/api/auth/me", headers=HEAD(KEY_EDITOR)).get_json()
    assert me["authenticated"] and me["user"]["provider"] == "apikey" and me["mode"] == "apikey"
    assert keyed.get("/api/auth/me").get_json()["authenticated"] is False


def make_doc(client, headers):
    client.post("/api/tickets/sample", headers=headers)
    return client.post("/api/reports", json={"days": 30}, headers=headers).get_json()


def test_approver_role_and_self_approval_rule(keyed):
    doc = make_doc(keyed, HEAD(KEY_ADMIN))
    assert doc["createdBy"] == "key:boss" and doc["events"][0]["actor"] == "key:boss"
    keyed.post(f"/api/reports/{doc['id']}/submit", headers=HEAD(KEY_ADMIN))
    decide = lambda key, **body: keyed.post(f"/api/reports/{doc['id']}/decision", json={"decision": "approve", **body}, headers=HEAD(key))
    assert decide(KEY_EDITOR).status_code == 403  # editors cannot approve
    own = decide(KEY_ADMIN)
    assert own.status_code == 403 and "you created" in own.get_json()["error"]
    ok = decide(KEY_ADMIN2)  # a different person; reviewer defaults to the signed-in name
    assert ok.status_code == 200 and ok.get_json()["events"][-1]["detail"] == "boss2" and ok.get_json()["status"] == "approved"


def test_self_approval_can_be_allowed(make_client):
    c = make_client(settings=Settings(**APIKEY, allow_self_approval=True))
    doc = make_doc(c, HEAD(KEY_ADMIN))
    c.post(f"/api/reports/{doc['id']}/submit", headers=HEAD(KEY_ADMIN))
    assert c.post(f"/api/reports/{doc['id']}/decision", json={"decision": "reject", "reviewer": "Me", "comment": "x"}, headers=HEAD(KEY_ADMIN)).status_code == 200


# ---------- SSO ----------
class FakeClient:
    def __init__(self, token=None, error=None):
        self.token, self.error = token, error

    def authorize_redirect(self, uri):
        return redirect("https://idp.example/authorize?redirect_uri=" + uri)

    def authorize_access_token(self):
        if self.error:
            raise self.error
        return self.token


class FakeOAuth:
    def __init__(self):
        self.clients = {"google": FakeClient(), "microsoft": FakeClient()}

    def create_client(self, name):
        return self.clients[name]

    def will_return(self, provider, **userinfo):
        self.clients[provider].token = {"userinfo": userinfo}
        self.clients[provider].error = None


@pytest.fixture
def idp():
    return FakeOAuth()


@pytest.fixture
def sso_app(make_client, idp):
    return make_client(settings=Settings(**SSO), llm=FakeLLM(None), oauth=idp)


def sign_in(client, idp, email, name="Person", provider="google", verified=True):
    info = {"email": email, "name": name}
    if provider == "google":
        info["email_verified"] = verified
    idp.will_return(provider, **info)
    return client.get(f"/api/auth/callback/{provider}")


def test_sso_requires_login_and_lists_providers(sso_app):
    assert sso_app.get("/api/state").status_code == 401
    info = sso_app.get("/api/auth/providers").get_json()
    assert info == {"mode": "sso", "providers": [{"id": "google", "label": "Google"}, {"id": "microsoft", "label": "Microsoft"}]}
    assert sso_app.get("/api/auth/me").get_json() == {"authenticated": False, "user": None, "mode": "sso"}


def test_login_redirects_to_the_identity_provider(sso_app):
    resp = sso_app.get("/api/auth/login/google")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("redirect_uri=http://localhost:3000/api/auth/callback/google")
    assert sso_app.get("/api/auth/login/github").status_code == 404
    assert sso_app.get("/api/auth/callback/github").status_code == 404


def test_successful_sign_in_creates_a_session_with_the_right_role(sso_app, idp):
    resp = sign_in(sso_app, idp, "Lead@Corp.com", name="Lena Lead")
    assert resp.status_code == 302 and resp.headers["Location"] == "http://localhost:3000/"
    me = sso_app.get("/api/auth/me").get_json()
    assert me["authenticated"] and me["user"]["email"] == "lead@corp.com" and me["user"]["role"] == "approver" and me["user"]["name"] == "Lena Lead"
    assert sso_app.get("/api/state").status_code == 200
    sso_app.post("/api/auth/logout")
    assert sso_app.get("/api/state").status_code == 401


def test_new_colleagues_default_to_viewer(sso_app, idp):
    sign_in(sso_app, idp, "newbie@corp.com")
    assert sso_app.get("/api/state").status_code == 200
    assert sso_app.post("/api/notes", json={"title": "t", "text": "x"}).status_code == 403


def test_sign_in_denials(sso_app, idp):
    assert sign_in(sso_app, idp, "stranger@other.com").headers["Location"].endswith("?login_error=denied")
    assert sign_in(sso_app, idp, "x@corp.com", verified=False).headers["Location"].endswith("?login_error=denied")
    idp.will_return("google")  # no e-mail claim at all
    assert sso_app.get("/api/auth/callback/google").headers["Location"].endswith("?login_error=denied")
    idp.will_return("microsoft", name="No Email")
    assert sso_app.get("/api/auth/callback/microsoft").headers["Location"].endswith("?login_error=denied")
    idp.clients["google"].token = {}
    assert sso_app.get("/api/auth/callback/google").headers["Location"].endswith("?login_error=denied")
    assert sso_app.get("/api/auth/me").get_json()["authenticated"] is False


def test_microsoft_sign_in_works_without_verified_claim(sso_app, idp):
    assert sign_in(sso_app, idp, "ed@corp.com", provider="microsoft").headers["Location"] == "http://localhost:3000/"
    assert sso_app.get("/api/auth/me").get_json()["user"]["role"] == "editor"


def test_provider_errors_do_not_leak_details(sso_app, idp):
    idp.clients["google"].error = RuntimeError("secret internals")
    resp = sso_app.get("/api/auth/callback/google")
    assert resp.headers["Location"].endswith("?login_error=failed") and "secret" not in resp.headers["Location"]


def test_access_removed_from_env_takes_effect_immediately(sso_app):
    with sso_app.session_transaction() as sess:
        sess["user"] = {"email": "fired@other.com", "name": "F", "provider": "google"}
    assert sso_app.get("/api/state").status_code == 401


def test_cross_site_writes_are_blocked_for_cookie_sessions(sso_app, idp):
    sign_in(sso_app, idp, "ed@corp.com")
    body = {"title": "t", "text": "x"}
    assert sso_app.post("/api/notes", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    assert sso_app.post("/api/notes", json=body, headers={"Origin": "http://localhost:3000"}).status_code == 201
    assert sso_app.post("/api/notes", json=body).status_code == 201  # non-browser clients send no Origin


def test_api_keys_still_work_alongside_sso_and_skip_the_origin_check(make_client, idp):
    c = make_client(settings=Settings(**SSO, api_keys=(("ci", "editor", KEY_EDITOR),)), oauth=idp)
    resp = c.post("/api/notes", json={"title": "t", "text": "x"}, headers={**HEAD(KEY_EDITOR), "Origin": "http://anything.example"})
    assert resp.status_code == 201


def test_sso_approval_requires_a_second_person(sso_app, idp):
    lead = sso_app
    sign_in(lead, idp, "lead@corp.com", name="Lena")
    lead.post("/api/tickets/sample")
    doc = lead.post("/api/reports", json={"days": 30}).get_json()
    lead.post(f"/api/reports/{doc['id']}/submit")
    assert lead.post(f"/api/reports/{doc['id']}/decision", json={"decision": "approve"}).status_code == 403
    boss = lead.application.test_client()
    sign_in(boss, idp, "boss@corp.com", name="Bo Boss")
    done = boss.post(f"/api/reports/{doc['id']}/decision", json={"decision": "approve", "comment": "ship it"})
    assert done.get_json()["events"][-1]["detail"] == "Bo Boss: ship it"
    assert boss.post(f"/api/reports/{doc['id']}/decision", json={"decision": "approve"}).status_code == 409
    log = [e["action"] for e in boss.get("/api/audit").get_json()]
    assert "login" in log and f"POST /api/reports/{doc['id']}/decision" in log


def test_audit_records_logins_denials_failures_and_logouts(sso_app, idp):
    sign_in(sso_app, idp, "stranger@other.com")
    idp.clients["google"].error = RuntimeError("x")
    sso_app.get("/api/auth/callback/google")
    sign_in(sso_app, idp, "boss@corp.com")
    sso_app.post("/api/auth/logout")
    sso_app.post("/api/auth/logout")  # logging out twice is harmless and not audited
    sign_in(sso_app, idp, "boss@corp.com")
    entries = sso_app.get("/api/audit?limit=50").get_json()
    assert [e["action"] for e in entries][::-1] == ["login_denied", "login_failed", "login", "logout", "login"]
    assert entries[-1]["actor"] == "stranger@other.com"


def test_real_authlib_registration_covers_configured_providers_only():
    oauth = sso.build_oauth(Flask(__name__), Settings(**SSO))
    assert oauth.create_client("google").client_id == "gid" and oauth.create_client("microsoft").client_id == "mid"
    assert oauth.create_client("linkedin") is None


def test_sso_app_builds_real_oauth_when_none_is_injected(make_client):
    c = make_client(settings=Settings(**SSO))
    assert c.get("/api/auth/providers").get_json()["providers"][0]["id"] == "google"


def test_login_endpoints_are_disabled_outside_sso_mode(make_client):
    c = make_client()
    assert c.get("/api/auth/login/google").status_code == 404
    assert c.get("/api/auth/callback/google").status_code == 404
    assert c.get("/api/auth/providers").get_json() == {"mode": "none", "providers": []}
    me = c.get("/api/auth/me").get_json()
    assert me["authenticated"] and me["user"]["role"] == "admin" and me["mode"] == "none"


# ---------- limits, CORS, headers ----------
def test_rate_limit_per_client_and_exempt_health(make_client):
    c = make_client(settings=Settings(rate_limit=3))
    assert [c.get("/api/state").status_code for _ in range(4)] == [200, 200, 200, 429]
    limited = c.get("/api/state")
    assert limited.get_json() == {"error": "too many requests; slow down"} and limited.headers["Retry-After"] == "60"
    assert c.get("/api/health").status_code == 200 and c.get("/api/ready").status_code == 200


def test_llm_endpoints_have_their_own_stricter_limit(make_client):
    c = make_client(settings=Settings(rate_limit_llm=1))
    assert c.post("/api/insights/suggest", json={}).status_code == 200
    assert c.post("/api/insights/suggest", json={}).status_code == 429
    assert [c.get("/api/report").status_code for _ in range(3)] == [200, 200, 200]  # reads do not count


def test_proxy_headers_are_trusted_only_when_configured(make_client):
    trusted = make_client(settings=Settings(rate_limit=2, trust_proxy=1))
    hit = lambda ip: trusted.get("/api/state", headers={"X-Forwarded-For": ip}).status_code
    assert [hit("1.1.1.1"), hit("1.1.1.1"), hit("1.1.1.1"), hit("2.2.2.2")] == [200, 200, 429, 200]


def test_cors_allows_only_configured_origins(make_client):
    c = make_client(settings=Settings(cors_origins=("http://app.example",)))
    pre = c.options("/api/notes", headers={"Origin": "http://app.example"})
    assert pre.status_code == 204 and pre.headers["Access-Control-Allow-Origin"] == "http://app.example"
    assert pre.headers["Access-Control-Allow-Credentials"] == "true" and "POST" in pre.headers["Access-Control-Allow-Methods"]
    assert "Access-Control-Allow-Origin" not in c.get("/api/state", headers={"Origin": "http://evil.example"}).headers
    assert "Access-Control-Allow-Origin" not in c.get("/api/state").headers


def test_security_headers_and_secure_cookie_settings(make_client):
    c = make_client(settings=Settings(cookie_secure=True))
    h = c.get("/api/state").headers
    assert h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"] == "no-referrer" and h["Cache-Control"] == "no-store"
    assert h["Strict-Transport-Security"].startswith("max-age=") and h["X-Content-Type-Options"] == "nosniff"
    plain = make_client().get("/api/state").headers
    assert "Strict-Transport-Security" not in plain
    assert "Cache-Control" not in make_client().get("/not-an-api-path").headers


def test_body_size_limit_comes_from_settings(make_client):
    c = make_client(settings=Settings(max_body_mb=1))
    assert c.post("/api/notes", json={"title": "t", "text": "x" * (2 * 1024 * 1024)}).status_code == 413


# ---------- audit trail, config, operations ----------
def test_audit_log_records_successful_writes_only_and_is_trimmed(make_client):
    c = make_client(settings=Settings(audit_retention=2))
    c.post("/api/notes", json={"title": "bad"})  # 400: not audited
    for i in range(3):
        c.post("/api/notes", json={"title": f"n{i}", "text": "x"})
    entries = c.get("/api/audit").get_json()
    assert len(entries) == 2 and entries[0]["actor"] == "local" and entries[0]["action"] == "POST /api/notes"
    assert len(c.get("/api/audit?limit=1").get_json()) == 1 and len(c.get("/api/audit?limit=abc").get_json()) == 2
    assert len(c.get("/api/audit?limit=0").get_json()) == 2


def test_public_and_admin_config_endpoints(make_client):
    s = Settings(app_name="Acme", accent_color="#112233", default_theme="dark", features=("notes",), windows=(3, 9),
                 default_days=9, locale="de-DE", llm_api_key="topsecret", secret_key="x" * 40)
    c = make_client(settings=s)
    public = c.get("/api/config/public").get_json()
    assert public == {"appName": "Acme", "accentColor": "#112233", "defaultTheme": "dark", "features": ["notes"],
                      "windows": [3, 9], "defaultDays": 9, "locale": "de-DE", "authMode": "none"}
    admin = c.get("/api/config")
    assert admin.get_json()["LLM_API_KEY"] == "set" and b"topsecret" not in admin.data and b"x" * 40 not in admin.data


def test_ready_reports_store_problems(make_client, monkeypatch):
    c = make_client()
    assert c.get("/api/ready").get_json() == {"status": "ready"}
    monkeypatch.setattr(app_module.Store, "save", lambda self, db: (_ for _ in ()).throw(OSError("disk full")))
    bad = c.get("/api/ready")
    assert bad.status_code == 503 and "disk full" in bad.get_json()["error"]


def test_metrics_and_usage_endpoints(make_client):
    llm = FakeLLM(lambda *a: {"category": "Performance"})
    c = make_client(llm=llm)
    c.get("/api/state")
    c.post("/api/notes", json={"title": "bad"})
    text = c.get("/api/metrics").get_data(as_text=True)
    assert 'http_requests_total{method="GET",status="2xx"}' in text and 'status="4xx"' in text
    assert "llm_calls_total 0" in text and "cache_hits_total" in text and "cache_entries 0" in text
    usage = c.get("/api/usage").get_json()
    assert usage["tokens"] == 0 and usage["cap"] == 0 and usage["session"] == llm.stats


def test_dataclass_settings_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().port = 1


# ---------- configurable windows, rules, template ----------
def test_report_windows_and_default_come_from_settings(make_client):
    c = make_client(settings=Settings(windows=(3, 10), default_days=10))
    c.post("/api/tickets/sample")
    assert c.get("/api/report").get_json()["days"] == 10 and c.get("/api/report?days=3").status_code == 200
    bad = c.get("/api/report?days=7")
    assert bad.status_code == 400 and "[3, 10]" in bad.get_json()["error"]
    assert c.post("/api/reports", json={}).get_json()["days"] == 10
    assert c.post("/api/reports", json={"days": 7}).status_code == 400


def test_custom_rules_and_report_template(make_client, tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text("uncategorised: Misc\ncategories:\n  Everything: [login, password]\nactions:\n  - {label: Fixed it, pattern: reset}\n", encoding="utf-8")
    template = tmp_path / "tpl.md"
    template.write_text("REPORT $title | $total tickets | $summary\n$categories\n", encoding="utf-8")
    c = make_client(settings=Settings(rules_file=str(rules), report_template=str(template)))
    c.post("/api/tickets/sample")
    report = c.get("/api/report?days=30").get_json()
    assert {x["category"] for x in report["categories"]} == {"Everything", "Misc"}  # unmatched tickets land in the custom fallback
    doc = c.post("/api/reports", json={"days": 30, "title": "Custom"}).get_json()
    assert doc["markdown"].startswith("REPORT Custom | 21 tickets |") and "### Everything" in doc["markdown"]


TICKET = {"subject": "slow login", "description": "", "resolution": "", "createdAt": "2026-06-14T00:00:00Z", "resolvedAt": "2026-06-14T01:00:00Z"}


def test_rule_min_hits_is_configurable(make_client):
    strict = make_client(settings=Settings(rule_min_hits=3))
    strict.post("/api/tickets", json=TICKET)
    assert strict.get("/api/report?days=7").get_json()["lowConfidence"] == 1  # needs 3 keyword hits; only 2 found


def test_rule_min_margin_is_configurable(make_client):
    relaxed = make_client(settings=Settings(rule_min_hits=1, rule_min_margin=1))
    relaxed.post("/api/tickets", json={**TICKET, "subject": "slow loading lag"})
    assert relaxed.get("/api/report?days=7").get_json()["lowConfidence"] == 0

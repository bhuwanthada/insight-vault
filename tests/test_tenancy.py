from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from backend import orgkeys, schema, secrets_box
from backend.config import Settings
from backend.db import StoreFactory, make_engine
from backend.directory import Directory
from backend.secrets_box import SecretBox
from backend.store import JsonStore
from backend.tenancy import (OrgContext, QuotaExceeded, Tenants, UnknownOrg, apply_claims, apply_overrides, coerce,
                             ensure_room, validate_overrides)
from tests.conftest import FakeLLM

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


@pytest.fixture(scope="module")
def pair():
    return orgkeys.generate_keypair()


def claims_for(pair, **kw):
    params = dict(org_id="acme", org_name="Acme Inc", now=NOW)
    params.update(kw)
    return orgkeys.verify(orgkeys.mint(pair[0], **params), pair[1], now=NOW)


# ---------- quotas ----------
def test_ensure_room_treats_missing_and_zero_as_unlimited():
    ensure_room({}, "tickets", 10**9)
    ensure_room({"tickets": 0}, "tickets", 10**9)
    ensure_room({"tickets": 5}, "tickets", 4)  # 4 + 1 fits
    with pytest.raises(QuotaExceeded, match=r"tickets \(limit 5\)") as info:
        ensure_room({"tickets": 5}, "tickets", 4, adding=2)
    assert info.value.name == "tickets" and info.value.limit == 5


# ---------- coercion and overrides ----------
BASE = Settings()


@pytest.mark.parametrize(
    "name,value,expected",
    [("pii_masking", True, True), ("votes", 5, 5), ("temperature", 1, 1.0), ("temperature", 0.5, 0.5),
     ("windows", [3, 9], (3, 9)), ("llm_fallbacks", ["openrouter:x"], ("openrouter:x",)), ("app_name", "  Acme  ", "Acme")],
)
def test_coerce_accepts_correctly_typed_values(name, value, expected):
    assert coerce(BASE, name, value) == expected


@pytest.mark.parametrize(
    "name,value,message",
    [("pii_masking", "yes", "true or false"), ("votes", True, "whole number"), ("votes", "3", "whole number"),
     ("temperature", "hot", "a number"), ("temperature", True, "a number"), ("windows", ["7"], "whole numbers"),
     ("windows", "7", "whole numbers"), ("llm_fallbacks", [1], "text values"), ("app_name", 5, "must be text")],
)
def test_coerce_explains_type_errors(name, value, message):
    with pytest.raises(ValueError, match=message):
        coerce(BASE, name, value)


def test_apply_overrides_only_allows_organization_level_settings():
    merged = apply_overrides(BASE, {"app_name": "Acme", "votes": 5, "rules_yaml": "categories: {A: [x]}"})
    assert merged.app_name == "Acme" and merged.votes == 5
    for forbidden in ("database_url", "secret_key", "platform_admin_keys", "auth_mode", "llm_api_key"):
        with pytest.raises(ValueError, match="cannot be changed per organization"):
            apply_overrides(BASE, {forbidden: "x"})


def test_validate_overrides_reports_every_kind_of_problem():
    assert validate_overrides(BASE, {"app_name": "Acme", "rules_yaml": "categories: {A: [x]}", "report_template_text": "# $title"}) == []
    assert "cannot be changed" in validate_overrides(BASE, {"port": 1})[0]
    assert "whole number" in validate_overrides(BASE, {"votes": "many"})[0]
    assert any("REPORT_DEFAULT_DAYS" in p for p in validate_overrides(BASE, {"default_days": 31}))  # cross-field validation
    assert any("ACCENT_COLOR" in p for p in validate_overrides(BASE, {"accent_color": "red"}))
    assert any("Invalid rules file" in p for p in validate_overrides(BASE, {"rules_yaml": "categories: {}"}))
    assert any("at most" in p for p in validate_overrides(BASE, {"report_template_text": "x" * 50_001}))
    assert any("at most" in p for p in validate_overrides(BASE, {"report_template_text": 5}))
    assert validate_overrides(BASE, {"rules_yaml": "   "}) == []  # blank means "use the default"


# ---------- claims ----------
def test_apply_claims_narrows_features_and_merges_people(pair):
    s = Settings(allowed_domains=("old.com",), admin_emails=("root@x.com",))
    claims = claims_for(pair, features=("notes", "support"), domains=["acme.com"], admins=["boss@acme.com"])
    out = apply_claims(s, claims)
    assert out.features == ("notes", "support") and out.app_name == "Acme Inc"
    assert out.allowed_domains == ("acme.com", "old.com") and out.admin_emails == ("boss@acme.com", "root@x.com")


def test_apply_claims_keeps_an_explicit_app_name(pair):
    assert apply_claims(Settings(app_name="Custom"), claims_for(pair)).app_name == "Custom"


@pytest.mark.parametrize("env_cap,claim_cap,expected", [(0, 0, 0), (0, 500, 500), (1000, 0, 1000), (1000, 500, 500), (300, 500, 300)])
def test_apply_claims_token_cap_is_the_stricter_of_the_two(pair, env_cap, claim_cap, expected):
    quotas = {"llm_tokens_per_day": claim_cap} if claim_cap else {}
    assert apply_claims(Settings(daily_token_cap=env_cap), claims_for(pair, quotas=quotas)).daily_token_cap == expected


# ---------- directory ----------
@pytest.fixture
def directory(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'd.db'}")
    schema.migrate(engine, NOW_ISO)
    return Directory(engine)


def test_activation_creates_then_refreshes_and_seeds_admins(directory, pair):
    first = claims_for(pair, admins=["boss@acme.com", "cto@acme.com"], plan="growth")
    org, created = directory.activate(first, NOW_ISO)
    assert created and org["id"] == "acme" and org["plan"] == "growth" and org["status"] == "active" and org["claims"]["admins"]
    assert directory.list_members("acme") == [{"email": "boss@acme.com", "role": "admin"}, {"email": "cto@acme.com", "role": "admin"}]
    directory.set_member("acme", "boss@acme.com", "viewer", NOW_ISO)  # someone changed a role after activation
    renewed, created = directory.activate(claims_for(pair, admins=["boss@acme.com"], plan="enterprise"), NOW_ISO)
    assert not created and renewed["plan"] == "enterprise"
    assert {"email": "boss@acme.com", "role": "viewer"} in directory.list_members("acme")  # renewal never resets a role


def test_org_updates_and_queries(directory, pair):
    assert directory.get_org("nope") is None and directory.list_orgs() == []
    directory.activate(claims_for(pair), NOW_ISO)
    directory.update_org("acme", settings={"app_name": "X"}, secrets_enc="blob", status="suspended")
    org = directory.get_org("acme")
    assert org["settings"] == {"app_name": "X"} and org["secrets_enc"] == "blob" and org["status"] == "suspended"
    assert [o["id"] for o in directory.list_orgs()] == ["acme"]


def test_memberships_only_count_active_organizations(directory, pair):
    directory.activate(claims_for(pair, org_id="acme", admins=["x@y.com"]), NOW_ISO)
    directory.activate(claims_for(pair, org_id="beta", admins=["x@y.com"]), "2026-06-16T00:00:00+00:00")
    assert [m["org"] for m in directory.memberships_for_email("x@y.com")] == ["acme", "beta"]
    directory.update_org("beta", status="suspended")
    assert [m["org"] for m in directory.memberships_for_email("x@y.com")] == ["acme"]
    assert directory.memberships_for_email("nobody@y.com") == []


def test_member_management(directory, pair):
    directory.activate(claims_for(pair), NOW_ISO)
    directory.set_member("acme", "a@acme.com", "editor", NOW_ISO)
    directory.set_member("acme", "a@acme.com", "approver", NOW_ISO)  # updates in place
    assert directory.list_members("acme") == [{"email": "a@acme.com", "role": "approver"}] and directory.count_members("acme") == 1
    assert directory.remove_member("acme", "a@acme.com") is True and directory.remove_member("acme", "a@acme.com") is False


def test_key_revocation_is_idempotent(directory):
    directory.revoke_key("abc", "leaked", NOW_ISO)
    directory.revoke_key("abc", "again", NOW_ISO)
    directory.revoke_key("def", "", NOW_ISO)
    assert directory.revoked_ids() == {"abc", "def"}


def test_deleting_an_org_removes_every_row_it_owns_and_nothing_else(directory, pair):
    engine = directory.engine
    for org in ("acme", "beta"):
        directory.activate(claims_for(pair, org_id=org, admins=[f"a@{org}.com"]), NOW_ISO)
        store = StoreFactory(engine).for_org(org)
        store.save({"notes": [{"id": "n1"}]})
        store.upsert_tickets([{"id": "T1", "resolvedAt": NOW_ISO}])
        store.append_audit({"at": "t", "actor": "a", "action": "x", "detail": ""}, 10)
        store.kv_set("usage", {"d": 1})
        with engine.begin() as conn:
            conn.execute(schema.connectors.insert().values(id=f"c-{org}", org_id=org, kind="csv", name="n", created_at=NOW_ISO))
    directory.delete_org("acme")
    with engine.connect() as conn:
        for table in (schema.orgs, schema.memberships, schema.docs, schema.tickets, schema.audit, schema.kv, schema.connectors):
            owner = table.c.id if table is schema.orgs else table.c.org_id
            assert conn.execute(sa.select(sa.func.count()).select_from(table).where(owner == "acme")).scalar() == 0, table.name
            assert conn.execute(sa.select(sa.func.count()).select_from(table).where(owner == "beta")).scalar() >= 1, table.name


# ---------- tenants: single organization ----------
def single(tmp_path, claims=None, settings=None, llm=None):
    s = settings or Settings()
    return Tenants(s, StoreFactory(json_path=str(tmp_path / "db.json")), None, SecretBox(""), lambda: NOW, claims, llm)


def test_single_org_without_a_license(tmp_path):
    tenants = single(tmp_path)
    ctx = tenants.context("default")
    assert isinstance(ctx, OrgContext) and ctx.plan == "self-hosted" and ctx.quotas == {} and ctx.exp is None
    assert ctx.status == "active" and ctx.name == "Insight Vault" and not ctx.read_only(10**12)
    assert tenants.context("default") is ctx and not tenants.multi and tenants.default_org_id == "default"
    with pytest.raises(UnknownOrg):
        tenants.context("other")


def test_single_org_license_defines_features_quotas_and_expiry(tmp_path, pair):
    claims = claims_for(pair, mode="license", plan="enterprise", features=("notes", "support"), quotas={"tickets": 9}, days=10)
    ctx = single(tmp_path, claims).context("default")
    assert ctx.plan == "enterprise" and ctx.settings.features == ("notes", "support") and ctx.quotas == {"tickets": 9}
    assert ctx.name == "Acme Inc" and ctx.exp == claims["exp"]
    assert not ctx.read_only(claims["exp"] - 1) and ctx.read_only(claims["exp"])


def test_invalidate_forces_a_rebuild(tmp_path):
    tenants = single(tmp_path)
    first = tenants.context("default")
    tenants.invalidate("default")
    assert tenants.context("default") is not first


def test_org_name_prefers_the_explicit_setting(tmp_path):
    assert single(tmp_path, settings=Settings(org_name="Home Lab")).context("default").name == "Home Lab"


# ---------- tenants: multi-tenant ----------
@pytest.fixture
def multi(tmp_path, pair):
    engine = make_engine(f"sqlite:///{tmp_path / 'mt.db'}")
    schema.migrate(engine, NOW_ISO)
    directory, box = Directory(engine), SecretBox(secrets_box.generate_key())
    settings = Settings(deployment_mode="multi_tenant", llm_provider="openrouter", llm_model="platform/model", llm_api_key="PLATFORM-KEY")
    llm = FakeLLM(None)
    tenants = Tenants(settings, StoreFactory(engine), directory, box, lambda: NOW, None, llm)
    directory.activate(claims_for(pair, org_id="acme", quotas={"tickets": 3}, features=("notes", "insights")), NOW_ISO)
    return tenants, directory, box


def test_multi_tenant_context_layers_platform_org_and_key(multi):
    tenants, directory, _ = multi
    directory.update_org("acme", settings={"app_name": "Acme Vault", "votes": 7})
    ctx = tenants.context("acme")
    assert tenants.multi and ctx.org_id == "acme" and ctx.settings.app_name == "Acme Vault" and ctx.settings.votes == 7
    assert ctx.settings.features == ("notes", "insights") and ctx.quotas == {"tickets": 3} and ctx.exp > 0
    assert ctx.settings.llm_api_key == "PLATFORM-KEY"  # only the model/behaviour changed: the platform key is still used
    with pytest.raises(UnknownOrg):
        tenants.context("ghost")


def test_each_organization_gets_its_own_store_and_services(multi, pair):
    tenants, directory, _ = multi
    directory.activate(claims_for(pair, org_id="beta"), NOW_ISO)
    a, b = tenants.context("acme"), tenants.context("beta")
    assert a.store is not b.store and a.services is not b.services and a.store.org_id == "acme" and b.store.org_id == "beta"


def test_an_organization_with_its_own_endpoint_never_gets_the_platform_key(multi):
    tenants, directory, box = multi
    directory.update_org("acme", settings={"llm_base_url": "https://llm.acme.example/v1"})
    assert tenants.context("acme").settings.llm_api_key == ""  # otherwise the platform key could be sent to an org-chosen server
    tenants.invalidate("acme")
    directory.update_org("acme", settings={"llm_provider": "groq", "llm_model": "m"})
    assert tenants.context("acme").settings.llm_api_key == ""
    tenants.invalidate("acme")
    directory.update_org("acme", secrets_enc=box.encrypt({"llm_api_key": "ORG-KEY"}))  # bringing its own key works
    ctx = tenants.context("acme")
    assert ctx.settings.llm_api_key == "ORG-KEY" and ctx.settings.llm_provider == "groq"


def test_an_organizations_own_key_is_used_even_without_endpoint_overrides(multi):
    tenants, directory, box = multi
    directory.update_org("acme", secrets_enc=box.encrypt({"llm_api_key": "ORG-KEY"}))
    assert tenants.context("acme").settings.llm_api_key == "ORG-KEY"


def test_unreadable_secrets_switch_the_llm_off_for_that_organization_only(multi, pair, caplog):
    tenants, directory, _ = multi
    directory.update_org("acme", secrets_enc=SecretBox(secrets_box.generate_key()).encrypt({"llm_api_key": "x"}))  # another master key
    ctx = tenants.context("acme")
    assert ctx.settings.llm_provider == "none" and "disabled" in caplog.text


def test_organization_rules_and_template_override_the_defaults(multi):
    tenants, directory, _ = multi
    directory.update_org("acme", settings={"rules_yaml": "categories: {Billing: [invoice]}", "report_template_text": "ONLY $title"})
    ctx = tenants.context("acme")
    assert ctx.services.rules.names == ["Billing"] and ctx.services.template == "ONLY $title"

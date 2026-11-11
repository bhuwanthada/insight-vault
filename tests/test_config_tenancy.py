import pytest

from backend.config import ConfigError, Settings
from backend.secrets_box import generate_key


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"DEPLOYMENT_MODE": "hybrid"}, "DEPLOYMENT_MODE='hybrid' must be one of"),
        ({"DEPLOYMENT_MODE": "multi_tenant", "AUTH_MODE": "none"}, "requires AUTH_MODE=sso or apikey"),
        ({"SECRETS_KEY": "not-a-key"}, "SECRETS_KEY must be one or more"),
        ({"LICENSE_REQUIRED": "true"}, "LICENSE_REQUIRED=true requires LICENSE_KEY"),
        ({"LICENSE_KEY": "a.b.c"}, "LICENSE_KEY requires ORG_KEY_PUBLIC_KEY"),
        ({"PLATFORM_ADMIN_KEYS": "ops:short"}, "must look like name:key"),
        ({"PLATFORM_ADMIN_KEYS": ":" + "k" * 20}, "must look like name:key"),
        ({"DB_POOL_SIZE": "0"}, "DB_POOL_SIZE=0 must be between"),
    ],
)
def test_tenancy_settings_are_validated(env, expected):
    with pytest.raises(ConfigError) as info:
        Settings.from_env(env)
    assert expected in str(info.value)


def test_tenancy_settings_are_parsed_and_secrets_are_masked():
    s = Settings.from_env({
        "DEPLOYMENT_MODE": "multi_tenant", "DATABASE_URL": "postgresql://u:p@h/db", "DB_POOL_SIZE": "4",
        "SECRETS_KEY": generate_key(), "ORG_KEY_PUBLIC_KEY_FILE": "pub.pem", "AUTH_MODE": "apikey",
        "API_KEYS": "ci:editor:" + "k" * 16, "PLATFORM_ADMIN_KEYS": "ops:" + "p" * 16 + ",ops2:" + "q" * 16,
        "REDIS_URL": "redis://cache:6379/0", "ORG_ID": "acme", "ORG_NAME": "Acme", "LLM_JSON_MODE": "true",
    })
    assert s.deployment_mode == "multi_tenant" and s.db_pool_size == 4
    assert s.platform_admin_keys == (("ops", "p" * 16), ("ops2", "q" * 16))
    assert s.org_id == "acme" and s.org_name == "Acme" and s.llm_json_mode and s.redis_url.startswith("redis://")
    view = s.masked()
    for secret in ("DATABASE_URL", "SECRETS_KEY", "PLATFORM_ADMIN_KEYS", "REDIS_URL"):
        assert view[secret] == "set"
    assert "p" * 16 not in str(view)


def test_provider_specific_key_variables_are_used_when_llm_api_key_is_empty():
    assert Settings.from_env({"LLM_PROVIDER": "groq", "LLM_MODEL": "m", "GROQ_API_KEY": "gk"}).llm_api_key == "gk"
    assert Settings.from_env({"LLM_PROVIDER": "openrouter", "LLM_MODEL": "m", "OPENROUTER_API_KEY": "ok"}).llm_api_key == "ok"
    explicit = {"LLM_PROVIDER": "groq", "LLM_MODEL": "m", "GROQ_API_KEY": "gk", "LLM_API_KEY": "explicit"}
    assert Settings.from_env(explicit).llm_api_key == "explicit"
    assert Settings.from_env({"LLM_PROVIDER": "litellm", "LLM_MODEL": "m", "GROQ_API_KEY": "gk"}).llm_api_key == ""


def test_multi_tenant_sso_needs_no_global_allow_list_because_members_come_from_the_database():
    base = {"AUTH_MODE": "sso", "SECRET_KEY": "s" * 32, "GOOGLE_CLIENT_ID": "g", "GOOGLE_CLIENT_SECRET": "s"}
    with pytest.raises(ConfigError, match="requires ALLOWED_EMAILS or ALLOWED_DOMAINS"):
        Settings.from_env(base)  # a single company still needs one: access is denied by default
    multi = {**base, "DEPLOYMENT_MODE": "multi_tenant", "DATABASE_URL": "sqlite:///x.db", "SECRETS_KEY": generate_key(), "ORG_KEY_PUBLIC_KEY": "pem"}
    assert Settings.from_env(multi).deployment_mode == "multi_tenant"

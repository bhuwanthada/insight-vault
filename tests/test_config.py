import pytest

from backend.config import ConfigError, Settings, env_name

GOOD_SSO = {
    "AUTH_MODE": "sso", "SECRET_KEY": "s" * 32, "GOOGLE_CLIENT_ID": "gid", "GOOGLE_CLIENT_SECRET": "gsecret",
    "ALLOWED_DOMAINS": "corp.com",
}


def test_defaults_need_no_environment():
    s = Settings.from_env({})
    assert (s.llm_provider, s.llm_enabled, s.auth_mode, s.port, s.default_days) == ("none", False, "none", 5001, 30)
    assert s.features == ("notes", "insights", "search", "support", "ai_suggestions", "integrations")


def test_reads_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("APP_NAME", "Acme Insights")
    assert Settings.from_env().app_name == "Acme Insights"


def test_model_alone_implies_litellm_for_backward_compatibility():
    s = Settings.from_env({"LLM_MODEL": "openrouter/x/y"})
    assert s.llm_provider == "litellm" and s.llm_enabled


def test_full_environment_is_parsed():
    env = {
        "LLM_PROVIDER": "OpenRouter", "LLM_MODEL": "vendor/model", "LLM_API_KEY": "k", "LLM_BASE_URL": "http://x/v1/",
        "LLM_FALLBACKS": "litellm:a/b, openai_compatible:local", "LLM_MODEL_CLASSIFY": "cheap", "LLM_MODEL_INSIGHTS": "big",
        "LLM_MODEL_SUMMARY": "mid", "LLM_TEMPERATURE": "0.5", "LLM_VOTE_TEMPERATURE": "0.9", "LLM_MAX_TOKENS": "256",
        "LLM_TIMEOUT": "5", "LLM_MAX_RETRIES": "1", "LLM_MAX_CONCURRENCY": "2", "LLM_VOTES": "5",
        "LLM_BREAKER_THRESHOLD": "3", "LLM_BREAKER_RESET": "10", "LLM_DAILY_TOKEN_CAP": "1000", "LLM_PII_MASKING": "yes",
        "PROMPT_DIR": "p", "OUTPUT_LANGUAGE": "French", "OPENROUTER_REFERER": "https://ref", "OPENROUTER_TITLE": "T",
        "CACHE_TTL": "10", "CACHE_SIZE": "7", "RULES_FILE": "r.yaml", "REPORT_TEMPLATE": "t.md", "RULE_MIN_HITS": "3",
        "RULE_MIN_MARGIN": "1", "REPORT_WINDOWS": "1,5,9", "REPORT_DEFAULT_DAYS": "5", "HOST": "0.0.0.0", "PORT": "8080",
        "THREADS": "4", "LOG_LEVEL": "debug", "LOG_FORMAT": "json", "DATA_FILE": "d.json", "MAX_BODY_MB": "2",
        "CORS_ORIGINS": "http://a.example, http://b.example", "TRUST_PROXY": "1", "RATE_LIMIT_PER_MINUTE": "60",
        "RATE_LIMIT_LLM_PER_MINUTE": "5", "AUDIT_RETENTION": "500", "AUTH_MODE": "apikey",
        "API_KEYS": "bot:editor:" + "k" * 16, "SESSION_HOURS": "2", "COOKIE_SECURE": "1", "PUBLIC_URL": "https://app.example/",
        "ALLOWED_EMAILS": "A@x.com", "ADMIN_EMAILS": "Boss@x.com", "APPROVER_EMAILS": "lead@x.com", "EDITOR_EMAILS": "ed@x.com",
        "DEFAULT_ROLE": "editor", "ALLOW_SELF_APPROVAL": "true", "OIDC_NAME": "Corp", "APP_NAME": "Acme",
        "ACCENT_COLOR": "#112233", "DEFAULT_THEME": "dark", "FEATURES": "notes,support", "LOCALE": "en-GB",
    }
    s = Settings.from_env(env)
    assert s.llm_provider == "openrouter" and s.llm_base_url == "http://x/v1" and s.llm_fallbacks == ("litellm:a/b", "openai_compatible:local")
    assert (s.temperature, s.max_tokens, s.votes, s.daily_token_cap, s.pii_masking) == (0.5, 256, 5, 1000, True)
    assert s.windows == (1, 5, 9) and s.default_days == 5 and s.log_level == "DEBUG" and s.cors_origins == ("http://a.example", "http://b.example")
    assert s.api_keys == (("bot", "editor", "k" * 16),) and s.public_url == "https://app.example"
    assert s.allowed_emails == ("a@x.com",) and s.admin_emails == ("boss@x.com",) and s.features == ("notes", "support")
    assert s.cookie_secure and s.allow_self_approval and s.default_role == "editor" and s.accent_color == "#112233"


def test_booleans_accept_common_spellings():
    assert Settings.from_env({"COOKIE_SECURE": "off"}).cookie_secure is False
    assert Settings.from_env({"COOKIE_SECURE": "On"}).cookie_secure is True


def test_masked_view_never_contains_secrets():
    s = Settings.from_env({"LLM_MODEL": "m", "LLM_API_KEY": "topsecret", "SECRET_KEY": "x" * 40,
                           "API_KEYS": "bot:editor:" + "k" * 16, "CORS_ORIGINS": "http://a.example"})
    view = s.masked()
    assert view["LLM_API_KEY"] == "set" and view["SECRET_KEY"] == "set" and view["API_KEYS"] == "set"
    assert view["GOOGLE_CLIENT_SECRET"] == "not set" and view["CORS_ORIGINS"] == ["http://a.example"]
    assert view["LLM_MAX_CONCURRENCY"] == 4 and view["RATE_LIMIT_PER_MINUTE"] == 0 and "max_concurrency" not in view
    assert "topsecret" not in str(view) and "kkkk" not in str(view)


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"LLM_PROVIDER": "skynet"}, "LLM_PROVIDER='skynet' must be one of"),
        ({"LLM_PROVIDER": "litellm"}, "LLM_MODEL is required"),
        ({"LLM_PROVIDER": "openai_compatible", "LLM_MODEL": "m"}, "LLM_BASE_URL is required"),
        ({"LLM_MODEL": "m", "LLM_FALLBACKS": "bogus"}, "must look like provider:model"),
        ({"LLM_MODEL": "m", "LLM_FALLBACKS": "none:x"}, "must look like provider:model"),
        ({"LLM_TIMEOUT": "fast"}, "LLM_TIMEOUT='fast' is not a valid float"),
        ({"LLM_VOTES": "0"}, "LLM_VOTES=0 must be between 1 and 15"),
        ({"LLM_PII_MASKING": "maybe"}, "LLM_PII_MASKING='maybe' must be true or false"),
        ({"REPORT_WINDOWS": "7,x"}, "REPORT_WINDOWS='7,x' must be comma-separated"),
        ({"REPORT_WINDOWS": "30,7"}, "REPORT_WINDOWS must be ascending"),
        ({"REPORT_WINDOWS": ",", "REPORT_DEFAULT_DAYS": "30"}, "REPORT_WINDOWS must be ascending"),
        ({"REPORT_DEFAULT_DAYS": "31"}, "REPORT_DEFAULT_DAYS (31) must be one of"),
        ({"ACCENT_COLOR": "red"}, "ACCENT_COLOR must be a hex color"),
        ({"FEATURES": "notes,teleport"}, "FEATURES has unknown entries"),
        ({"FEATURES": "search,support"}, "FEATURES must include notes"),
        ({"AUTH_MODE": "apikey"}, "AUTH_MODE=apikey requires API_KEYS"),
        ({"API_KEYS": "bot:wizard:" + "k" * 16}, "must look like name:role:key"),
        ({"API_KEYS": "bot:editor:short"}, "must look like name:role:key"),
        ({"DEFAULT_THEME": "neon"}, "DEFAULT_THEME='neon' must be one of"),
        ({"AUTH_MODE": "sso"}, "requires SECRET_KEY of at least 32"),
        ({"AUTH_MODE": "sso", "SECRET_KEY": "s" * 32}, "requires at least one provider"),
        ({**GOOD_SSO, "ALLOWED_DOMAINS": ""}, "requires ALLOWED_EMAILS or ALLOWED_DOMAINS"),
        ({**GOOD_SSO, "MICROSOFT_CLIENT_ID": "m", "MICROSOFT_CLIENT_SECRET": "s", "MICROSOFT_TENANT": "common"}, "not common/organizations/consumers"),
        ({**GOOD_SSO, "MICROSOFT_CLIENT_ID": "m", "MICROSOFT_CLIENT_SECRET": "s"}, "MICROSOFT_TENANT is required"),
    ],
)
def test_invalid_configuration_is_rejected_with_a_clear_message(env, expected):
    with pytest.raises(ConfigError, match="Invalid configuration") as info:
        Settings.from_env(env)
    assert expected in str(info.value)


def test_all_problems_are_reported_together():
    with pytest.raises(ConfigError) as info:
        Settings.from_env({"LLM_TIMEOUT": "x", "LLM_VOTES": "0", "AUTH_MODE": "apikey"})
    assert str(info.value).count("\n  - ") == 3


def test_sso_providers_list_only_fully_configured_ones():
    env = {**GOOD_SSO, "LINKEDIN_CLIENT_ID": "l", "LINKEDIN_CLIENT_SECRET": "ls", "MICROSOFT_CLIENT_ID": "m",
           "MICROSOFT_CLIENT_SECRET": "ms", "MICROSOFT_TENANT": "contoso.com", "OIDC_NAME": "Okta",
           "OIDC_CLIENT_ID": "o", "OIDC_CLIENT_SECRET": "os", "OIDC_METADATA_URL": "https://idp/.well-known/openid-configuration"}
    providers = Settings.from_env(env).sso_providers()
    assert [p[0] for p in providers] == ["google", "linkedin", "microsoft", "oidc"]
    assert "contoso.com" in providers[2][4] and providers[3][1] == "Okta"
    assert Settings.from_env({"GOOGLE_CLIENT_ID": "only-id"}).sso_providers() == []


# ---------- .env.example stays truthful ----------
ENV_EXAMPLE = __import__("pathlib").Path(__file__).resolve().parent.parent / ".env.example"


def test_env_example_parses_and_equals_the_defaults():
    from dotenv import dotenv_values

    values = {k: v or "" for k, v in dotenv_values(ENV_EXAMPLE).items()}
    assert Settings.from_env(values) == Settings.from_env({})  # every documented default really is the default


def test_every_setting_the_code_reads_is_documented_in_env_example():
    import re

    from dotenv import dotenv_values

    source = (ENV_EXAMPLE.parent / "backend" / "config.py").read_text(encoding="utf-8")
    read = set(re.findall(r'r\.\w+\("([A-Z][A-Z0-9_]+)"', source))
    assert read, "the pattern should find the settings"
    assert read <= set(dotenv_values(ENV_EXAMPLE)), sorted(read - set(dotenv_values(ENV_EXAMPLE)))


def test_every_setting_maps_to_a_documented_environment_variable():
    from dataclasses import fields

    from dotenv import dotenv_values

    documented = set(dotenv_values(ENV_EXAMPLE))
    missing = [f.name for f in fields(Settings) if env_name(f.name) not in documented]
    assert not missing, missing

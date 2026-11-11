"""Runtime settings, read from environment variables (optionally via a .env file).

Every problem is collected and reported together, so a bad configuration stops startup with one
clear message instead of failing later at an unexpected moment.
"""
import os
import re
from dataclasses import dataclass, fields

from . import secrets_box

LLM_PROVIDERS = ("litellm", "openrouter", "groq", "openai_compatible", "none")
DEPLOYMENT_MODES = ("single_org", "multi_tenant")
ROLES = ("viewer", "editor", "approver", "admin")
AUTH_MODES = ("none", "apikey", "sso")
THEMES = ("system", "light", "dark")
LOG_FORMATS = ("text", "json")
FEATURES = ("notes", "insights", "search", "support", "ai_suggestions", "integrations")
SECRET_FIELDS = {"llm_api_key", "secret_key", "api_keys", "google_client_secret", "linkedin_client_secret",
                 "microsoft_client_secret", "oidc_client_secret", "secrets_key", "platform_admin_keys", "database_url",
                 "redis_url", "license_key", "org_key_public_key"}
# A setting's environment variable is its field name upper-cased, except for these.
ENV_OVERRIDES = {
    "temperature": "LLM_TEMPERATURE", "vote_temperature": "LLM_VOTE_TEMPERATURE", "max_tokens": "LLM_MAX_TOKENS",
    "timeout": "LLM_TIMEOUT", "max_retries": "LLM_MAX_RETRIES", "max_concurrency": "LLM_MAX_CONCURRENCY",
    "votes": "LLM_VOTES", "breaker_threshold": "LLM_BREAKER_THRESHOLD", "breaker_reset": "LLM_BREAKER_RESET",
    "daily_token_cap": "LLM_DAILY_TOKEN_CAP", "pii_masking": "LLM_PII_MASKING", "windows": "REPORT_WINDOWS",
    "default_days": "REPORT_DEFAULT_DAYS", "rate_limit": "RATE_LIMIT_PER_MINUTE", "rate_limit_llm": "RATE_LIMIT_LLM_PER_MINUTE",
}
PERSONAL_TENANTS = ("common", "organizations", "consumers")  # multi-tenant values are unsafe with email allow-lists


def env_name(field):
    return ENV_OVERRIDES.get(field, field.upper())


class ConfigError(ValueError):
    """One or more configuration values are invalid."""


@dataclass(frozen=True)
class Settings:
    # --- deployment and tenancy ---
    deployment_mode: str = "single_org"
    database_url: str = ""  # empty = a single JSON file (local use); otherwise PostgreSQL (or SQLite for development)
    db_pool_size: int = 10
    org_id: str = "default"  # the organization id in single_org mode
    org_name: str = ""
    license_key: str = ""  # single_org: a signed key from the vendor that defines this installation
    license_required: bool = False
    org_key_public_key: str = ""  # PEM public key that verifies org keys (the vendor keeps the private half)
    org_key_public_key_file: str = ""
    secrets_key: str = ""  # master key that encrypts stored secrets (per-org LLM keys, connector credentials)
    platform_admin_keys: tuple = ()  # ((name, key), ...) for the SaaS operator API
    redis_url: str = ""  # shared cache, locks and rate limits across several processes
    sync_initial_days: int = 90  # how far back the first sync of a connector reaches
    sync_max_pages: int = 50  # pages fetched per run; a longer sync continues on the next run
    connector_extra_hosts: tuple = ()  # extra host suffixes connectors may contact (custom vendor domains)
    worker_poll_seconds: int = 15
    # --- LLM ---
    llm_provider: str = "none"
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_fallbacks: tuple = ()  # entries like "openrouter:vendor/model"
    llm_model_classify: str = ""
    llm_model_insights: str = ""
    llm_model_summary: str = ""
    temperature: float = 0.2
    vote_temperature: float = 0.7
    max_tokens: int = 1024
    timeout: float = 30.0
    max_retries: int = 2
    max_concurrency: int = 4
    votes: int = 3
    breaker_threshold: int = 5
    breaker_reset: float = 30.0
    daily_token_cap: int = 0  # 0 = unlimited
    pii_masking: bool = False
    llm_json_mode: bool = False  # ask the provider for strict JSON output (supported by many, not all, models)
    prompt_dir: str = ""
    output_language: str = "English"
    openrouter_referer: str = ""
    openrouter_title: str = "Insight Vault"
    cache_ttl: float = 3600.0
    cache_size: int = 512
    # --- classification and reporting ---
    rules_file: str = ""
    report_template: str = ""
    rule_min_hits: int = 2
    rule_min_margin: int = 2
    windows: tuple = (7, 14, 30, 60, 90)
    default_days: int = 30
    # --- server ---
    host: str = "127.0.0.1"
    port: int = 5001
    threads: int = 8
    log_level: str = "INFO"
    log_format: str = "text"
    data_file: str = ""
    max_body_mb: int = 10
    cors_origins: tuple = ()
    trust_proxy: int = 0
    rate_limit: int = 0  # requests per minute per client, 0 = off
    rate_limit_llm: int = 0  # same, for endpoints that call the LLM
    audit_retention: int = 5000
    # --- authentication ---
    auth_mode: str = "none"
    api_keys: tuple = ()  # ((name, role, key), ...)
    secret_key: str = ""
    session_hours: int = 12
    cookie_secure: bool = False
    public_url: str = "http://localhost:3000"
    allowed_emails: tuple = ()
    allowed_domains: tuple = ()
    admin_emails: tuple = ()
    approver_emails: tuple = ()
    editor_emails: tuple = ()
    default_role: str = "viewer"
    allow_self_approval: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant: str = ""
    oidc_name: str = "Company SSO"
    oidc_metadata_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # --- UI ---
    app_name: str = "Insight Vault"
    accent_color: str = "#4f46e5"
    default_theme: str = "system"
    features: tuple = FEATURES
    locale: str = "en-US"

    @property
    def llm_enabled(self):
        return self.llm_provider != "none" and bool(self.llm_model)

    def masked(self):
        """Settings safe to show an administrator, keyed by environment variable name; secrets show 'set' / 'not set'."""
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                value = "set" if value else "not set"
            elif isinstance(value, tuple):
                value = list(value)
            out[env_name(f.name)] = value
        return out

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        reader = _Reader(env)
        r = reader
        explicit_provider = r.text("LLM_PROVIDER").lower()
        model = r.text("LLM_MODEL")
        provider = r.choice("LLM_PROVIDER", explicit_provider or ("litellm" if model else "none"), LLM_PROVIDERS)
        provider_key = {"groq": "GROQ_API_KEY", "openrouter": "OPENROUTER_API_KEY"}.get(provider)
        settings = cls(
            deployment_mode=r.choice("DEPLOYMENT_MODE", cls.deployment_mode, DEPLOYMENT_MODES),
            database_url=r.text("DATABASE_URL"),
            db_pool_size=r.number("DB_POOL_SIZE", cls.db_pool_size, int, 1, 200),
            org_id=r.text("ORG_ID", cls.org_id),
            org_name=r.text("ORG_NAME"),
            license_key=r.text("LICENSE_KEY"),
            license_required=r.boolean("LICENSE_REQUIRED", cls.license_required),
            org_key_public_key=r.text("ORG_KEY_PUBLIC_KEY").replace("\\n", "\n"),  # allow "\n" escapes in a one-line .env value
            org_key_public_key_file=r.text("ORG_KEY_PUBLIC_KEY_FILE"),
            secrets_key=r.text("SECRETS_KEY"),
            platform_admin_keys=tuple(r.pairs("PLATFORM_ADMIN_KEYS")),
            redis_url=r.text("REDIS_URL"),
            sync_initial_days=r.number("SYNC_INITIAL_DAYS", cls.sync_initial_days, int, 1, 3650),
            sync_max_pages=r.number("SYNC_MAX_PAGES", cls.sync_max_pages, int, 1, 1000),
            connector_extra_hosts=tuple(r.items("CONNECTOR_EXTRA_HOSTS")),
            worker_poll_seconds=r.number("WORKER_POLL_SECONDS", cls.worker_poll_seconds, int, 1, 3600),
            llm_provider=provider,
            llm_model=model,
            llm_api_key=r.text("LLM_API_KEY") or (r.text(provider_key) if provider_key else ""),
            llm_base_url=r.text("LLM_BASE_URL").rstrip("/"),
            llm_fallbacks=tuple(r.items("LLM_FALLBACKS", lower=False)),
            llm_model_classify=r.text("LLM_MODEL_CLASSIFY"),
            llm_model_insights=r.text("LLM_MODEL_INSIGHTS"),
            llm_model_summary=r.text("LLM_MODEL_SUMMARY"),
            temperature=r.number("LLM_TEMPERATURE", cls.temperature, float, 0, 2),
            vote_temperature=r.number("LLM_VOTE_TEMPERATURE", cls.vote_temperature, float, 0, 2),
            max_tokens=r.number("LLM_MAX_TOKENS", cls.max_tokens, int, 1, 200000),
            timeout=r.number("LLM_TIMEOUT", cls.timeout, float, 0.1, 600),
            max_retries=r.number("LLM_MAX_RETRIES", cls.max_retries, int, 0, 10),
            max_concurrency=r.number("LLM_MAX_CONCURRENCY", cls.max_concurrency, int, 1, 256),
            votes=r.number("LLM_VOTES", cls.votes, int, 1, 15),
            breaker_threshold=r.number("LLM_BREAKER_THRESHOLD", cls.breaker_threshold, int, 1, 1000),
            breaker_reset=r.number("LLM_BREAKER_RESET", cls.breaker_reset, float, 0, 86400),
            daily_token_cap=r.number("LLM_DAILY_TOKEN_CAP", cls.daily_token_cap, int, 0, 10**12),
            pii_masking=r.boolean("LLM_PII_MASKING", cls.pii_masking),
            llm_json_mode=r.boolean("LLM_JSON_MODE", cls.llm_json_mode),
            prompt_dir=r.text("PROMPT_DIR"),
            output_language=r.text("OUTPUT_LANGUAGE", cls.output_language),
            openrouter_referer=r.text("OPENROUTER_REFERER"),
            openrouter_title=r.text("OPENROUTER_TITLE", cls.openrouter_title),
            cache_ttl=r.number("CACHE_TTL", cls.cache_ttl, float, 0, 10**7),
            cache_size=r.number("CACHE_SIZE", cls.cache_size, int, 1, 10**6),
            rules_file=r.text("RULES_FILE"),
            report_template=r.text("REPORT_TEMPLATE"),
            rule_min_hits=r.number("RULE_MIN_HITS", cls.rule_min_hits, int, 1, 100),
            rule_min_margin=r.number("RULE_MIN_MARGIN", cls.rule_min_margin, int, 1, 100),
            windows=tuple(r.int_items("REPORT_WINDOWS", cls.windows)),
            default_days=r.number("REPORT_DEFAULT_DAYS", cls.default_days, int, 1, 3650),
            host=r.text("HOST", cls.host),
            port=r.number("PORT", cls.port, int, 1, 65535),
            threads=r.number("THREADS", cls.threads, int, 1, 512),
            log_level=r.text("LOG_LEVEL", cls.log_level).upper(),
            log_format=r.choice("LOG_FORMAT", cls.log_format, LOG_FORMATS),
            data_file=r.text("DATA_FILE"),
            max_body_mb=r.number("MAX_BODY_MB", cls.max_body_mb, int, 1, 1024),
            cors_origins=tuple(r.items("CORS_ORIGINS", lower=False)),
            trust_proxy=r.number("TRUST_PROXY", cls.trust_proxy, int, 0, 10),
            rate_limit=r.number("RATE_LIMIT_PER_MINUTE", cls.rate_limit, int, 0, 10**6),
            rate_limit_llm=r.number("RATE_LIMIT_LLM_PER_MINUTE", cls.rate_limit_llm, int, 0, 10**6),
            audit_retention=r.number("AUDIT_RETENTION", cls.audit_retention, int, 100, 10**7),
            auth_mode=r.choice("AUTH_MODE", cls.auth_mode, AUTH_MODES),
            api_keys=tuple(r.api_keys("API_KEYS")),
            secret_key=r.text("SECRET_KEY"),
            session_hours=r.number("SESSION_HOURS", cls.session_hours, int, 1, 24 * 90),
            cookie_secure=r.boolean("COOKIE_SECURE", cls.cookie_secure),
            public_url=r.text("PUBLIC_URL", cls.public_url).rstrip("/"),
            allowed_emails=tuple(r.items("ALLOWED_EMAILS")),
            allowed_domains=tuple(r.items("ALLOWED_DOMAINS")),
            admin_emails=tuple(r.items("ADMIN_EMAILS")),
            approver_emails=tuple(r.items("APPROVER_EMAILS")),
            editor_emails=tuple(r.items("EDITOR_EMAILS")),
            default_role=r.choice("DEFAULT_ROLE", cls.default_role, ROLES),
            allow_self_approval=r.boolean("ALLOW_SELF_APPROVAL", cls.allow_self_approval),
            google_client_id=r.text("GOOGLE_CLIENT_ID"),
            google_client_secret=r.text("GOOGLE_CLIENT_SECRET"),
            linkedin_client_id=r.text("LINKEDIN_CLIENT_ID"),
            linkedin_client_secret=r.text("LINKEDIN_CLIENT_SECRET"),
            microsoft_client_id=r.text("MICROSOFT_CLIENT_ID"),
            microsoft_client_secret=r.text("MICROSOFT_CLIENT_SECRET"),
            microsoft_tenant=r.text("MICROSOFT_TENANT"),
            oidc_name=r.text("OIDC_NAME", cls.oidc_name),
            oidc_metadata_url=r.text("OIDC_METADATA_URL"),
            oidc_client_id=r.text("OIDC_CLIENT_ID"),
            oidc_client_secret=r.text("OIDC_CLIENT_SECRET"),
            app_name=r.text("APP_NAME", cls.app_name),
            accent_color=r.text("ACCENT_COLOR", cls.accent_color),
            default_theme=r.choice("DEFAULT_THEME", cls.default_theme, THEMES),
            features=tuple(r.items("FEATURES", default=FEATURES)),
            locale=r.text("LOCALE", cls.locale),
        )
        problems = reader.errors + settings.problems()
        if problems:
            raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))
        return settings

    def sso_providers(self):
        """Identity providers that have credentials configured, as (id, label, client_id, secret, metadata_url)."""
        found = []
        if self.google_client_id and self.google_client_secret:
            found.append(("google", "Google", self.google_client_id, self.google_client_secret,
                          "https://accounts.google.com/.well-known/openid-configuration"))
        if self.linkedin_client_id and self.linkedin_client_secret:
            found.append(("linkedin", "LinkedIn", self.linkedin_client_id, self.linkedin_client_secret,
                          "https://www.linkedin.com/oauth/.well-known/openid-configuration"))
        if self.microsoft_client_id and self.microsoft_client_secret and self.microsoft_tenant:
            found.append(("microsoft", "Microsoft", self.microsoft_client_id, self.microsoft_client_secret,
                          f"https://login.microsoftonline.com/{self.microsoft_tenant}/v2.0/.well-known/openid-configuration"))
        if self.oidc_client_id and self.oidc_client_secret and self.oidc_metadata_url:
            found.append(("oidc", self.oidc_name, self.oidc_client_id, self.oidc_client_secret, self.oidc_metadata_url))
        return found

    def problems(self):
        """Cross-field validation. Returns a list of human-readable problems."""
        out = []
        if self.llm_provider != "none" and not self.llm_model:
            out.append(f"LLM_MODEL is required when LLM_PROVIDER={self.llm_provider}")
        if self.llm_provider == "openai_compatible" and not self.llm_base_url:
            out.append("LLM_BASE_URL is required when LLM_PROVIDER=openai_compatible")
        for entry in self.llm_fallbacks:
            provider, _, model = entry.partition(":")
            if provider not in LLM_PROVIDERS or provider == "none" or not model:
                out.append(f"LLM_FALLBACKS entry '{entry}' must look like provider:model (provider is one of "
                           f"{', '.join(p for p in LLM_PROVIDERS if p != 'none')})")
        if self.deployment_mode == "multi_tenant":
            for label, present in (("DATABASE_URL", self.database_url), ("SECRETS_KEY", self.secrets_key),
                                   ("ORG_KEY_PUBLIC_KEY or ORG_KEY_PUBLIC_KEY_FILE", self.org_key_public_key or self.org_key_public_key_file)):
                if not present:
                    out.append(f"DEPLOYMENT_MODE=multi_tenant requires {label}")
            if self.auth_mode == "none":
                out.append("DEPLOYMENT_MODE=multi_tenant requires AUTH_MODE=sso or apikey (organizations must sign in)")
        if self.secrets_key and not secrets_box.valid_keys(self.secrets_key):
            out.append('SECRETS_KEY must be one or more comma-separated keys made with: python -m backend.admin secrets-key')
        if self.license_required and not self.license_key:
            out.append("LICENSE_REQUIRED=true requires LICENSE_KEY")
        if self.license_key and not (self.org_key_public_key or self.org_key_public_key_file):
            out.append("LICENSE_KEY requires ORG_KEY_PUBLIC_KEY or ORG_KEY_PUBLIC_KEY_FILE to verify it")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", self.accent_color):
            out.append("ACCENT_COLOR must be a hex color like #4f46e5")
        bad_features = [f for f in self.features if f not in FEATURES]
        if bad_features:
            out.append(f"FEATURES has unknown entries {bad_features}; allowed: {', '.join(FEATURES)}")
        if "notes" not in self.features:
            out.append("FEATURES must include notes (the other tabs link into notes)")
        if list(self.windows) != sorted(set(self.windows)) or not self.windows:
            out.append("REPORT_WINDOWS must be ascending, unique whole numbers of days")
        if self.default_days not in self.windows:
            out.append(f"REPORT_DEFAULT_DAYS ({self.default_days}) must be one of REPORT_WINDOWS {list(self.windows)}")
        if self.auth_mode == "apikey" and not self.api_keys:
            out.append("AUTH_MODE=apikey requires API_KEYS")
        if self.auth_mode == "sso":
            if len(self.secret_key) < 32:
                out.append("AUTH_MODE=sso requires SECRET_KEY of at least 32 characters")
            if not self.sso_providers():
                out.append("AUTH_MODE=sso requires at least one provider with a client id and secret")
            if not self.allowed_emails and not self.allowed_domains:
                out.append("AUTH_MODE=sso requires ALLOWED_EMAILS or ALLOWED_DOMAINS (access is denied by default)")
            if self.microsoft_client_id and self.microsoft_tenant.lower() in PERSONAL_TENANTS:
                out.append("MICROSOFT_TENANT must be your own tenant id or domain, not common/organizations/consumers")
            if self.microsoft_client_id and not self.microsoft_tenant:
                out.append("MICROSOFT_TENANT is required when MICROSOFT_CLIENT_ID is set")
        return out


class _Reader:
    """Typed access to an environment mapping that records problems instead of raising immediately."""

    def __init__(self, env):
        self.env, self.errors = env, []

    def text(self, key, default=""):
        return str(self.env.get(key, default) or default).strip()

    def number(self, key, default, kind, low, high):
        raw = self.env.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            value = kind(str(raw).strip())
        except ValueError:
            self.errors.append(f"{key}='{raw}' is not a valid {kind.__name__}")
            return default
        if not low <= value <= high:
            self.errors.append(f"{key}={value} must be between {low} and {high}")
            return default
        return value

    def boolean(self, key, default):
        raw = str(self.env.get(key, "")).strip().lower()
        if raw == "":
            return default
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
        self.errors.append(f"{key}='{raw}' must be true or false")
        return default

    def choice(self, key, default, options):
        value = self.text(key, default).lower()
        if value not in options:
            self.errors.append(f"{key}='{value}' must be one of: {', '.join(options)}")
            return default
        return value

    def items(self, key, lower=True, default=()):
        raw = self.text(key)
        if not raw:
            return list(default)
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return [p.lower() for p in parts] if lower else parts

    def int_items(self, key, default):
        raw = self.text(key)
        if not raw:
            return list(default)
        try:
            return [int(p) for p in raw.split(",") if p.strip()]
        except ValueError:
            self.errors.append(f"{key}='{raw}' must be comma-separated whole numbers")
            return list(default)

    def pairs(self, key):
        parsed = []
        for entry in self.items(key, lower=False):
            name, _, secret = entry.partition(":")
            if not name or len(secret) < 16:
                self.errors.append(f"{key} entry '{name or entry}' must look like name:key (key at least 16 characters)")
                continue
            parsed.append((name, secret))
        return parsed

    def api_keys(self, key):
        parsed = []
        for entry in self.items(key, lower=False):
            name, _, rest = entry.partition(":")
            role, _, secret = rest.partition(":")
            if not name or role not in ROLES or len(secret) < 16:
                self.errors.append(f"{key} entry '{name or entry}' must look like name:role:key (role one of "
                                   f"{', '.join(ROLES)}; key at least 16 characters)")
                continue
            parsed.append((name, role, secret))
        return parsed

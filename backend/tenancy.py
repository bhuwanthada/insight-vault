"""Organizations at runtime: each one gets its own settings, store and services.

single_org     one organization, configured by .env (and by a license key if you have one)
multi_tenant   many organizations from the directory; each is configured by its own key sections,
               its own editable settings and its own encrypted secrets, layered over the platform defaults.

Layering (later wins):  platform .env  ->  organization settings  ->  the sections of the organization's key.
"""
import logging
from dataclasses import dataclass, replace

from .config import FEATURES, ConfigError, Settings
from .resilience import SingleFlight, TTLCache
from .rules import load_rules_text
from .secrets_box import SecretsUnavailable
from .services import Services, build_services

log = logging.getLogger("insight_vault.tenancy")

# Settings an organization administrator may change for their own organization.
OVERRIDABLE = frozenset({
    "app_name", "accent_color", "default_theme", "locale", "windows", "default_days", "features",
    "llm_provider", "llm_model", "llm_base_url", "llm_fallbacks", "llm_model_classify", "llm_model_insights",
    "llm_model_summary", "temperature", "vote_temperature", "max_tokens", "votes", "pii_masking", "llm_json_mode",
    "output_language", "allow_self_approval", "rule_min_hits", "rule_min_margin",
})
TEXT_OVERRIDES = frozenset({"rules_yaml", "report_template_text"})
OWN_ENDPOINT_FIELDS = ("llm_provider", "llm_base_url")  # choosing these means the organization brings its own credentials
MAX_TEXT = 50_000


class UnknownOrg(LookupError):
    """No such organization."""


class QuotaExceeded(Exception):
    def __init__(self, name, limit):
        super().__init__(f"plan limit reached: {name} (limit {limit})")
        self.name, self.limit = name, limit


def ensure_room(quotas, name, current, adding=1):
    """Raise QuotaExceeded if adding would pass the limit. A missing or 0 limit means unlimited."""
    limit = quotas.get(name, 0)
    if limit and current + adding > limit:
        raise QuotaExceeded(name, limit)


@dataclass
class OrgContext:
    org_id: str
    name: str
    plan: str
    settings: Settings
    store: object
    services: Services
    claims: dict
    quotas: dict
    status: str
    exp: object  # unix seconds of the governing key's expiry, or None

    def read_only(self, now_ts):
        """After the key expires the organization can still read and export its data, but not change it."""
        return self.exp is not None and now_ts >= self.exp


def coerce(platform, name, value):
    """Convert a JSON value to the type of the setting, or raise ValueError saying what was expected."""
    default = getattr(platform, name)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number")
        return float(value)
    if isinstance(default, tuple):
        numbers = name == "windows"
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int if numbers else str) for v in value):
            raise ValueError(f"{name} must be a list of {'whole numbers' if numbers else 'text values'}")
        return tuple(value)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    return value.strip()


def apply_overrides(platform, overrides):
    """Settings with an organization's overrides applied. Raises ValueError for unknown or badly typed fields."""
    changes = {}
    for name, value in overrides.items():
        if name in TEXT_OVERRIDES:
            continue
        if name not in OVERRIDABLE:
            raise ValueError(f"{name} cannot be changed per organization")
        changes[name] = coerce(platform, name, value)
    return replace(platform, **changes)


def validate_overrides(platform, overrides):
    """Every problem with a proposed set of organization settings, as readable messages (empty = valid)."""
    problems = []
    try:
        merged = apply_overrides(platform, overrides)
    except ValueError as exc:
        return [str(exc)]
    inherited = set(platform.problems())  # the platform's own configuration is not the organization's fault
    problems += [p for p in merged.problems() if p not in inherited]
    for name in TEXT_OVERRIDES & set(overrides):
        text = overrides[name]
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            problems.append(f"{name} must be text of at most {MAX_TEXT} characters")
    if isinstance(overrides.get("rules_yaml"), str) and overrides["rules_yaml"].strip():
        try:
            load_rules_text(overrides["rules_yaml"], "your organization rules")
        except ConfigError as exc:
            problems.append(str(exc))
    return problems


def apply_claims(settings, claims):
    """Apply the sections of an organization key: features, quotas, domains and administrators."""
    cap, claimed = settings.daily_token_cap, claims["quotas"].get("llm_tokens_per_day", 0)
    if claimed:
        cap = min(cap, claimed) if cap else claimed
    default_name = Settings.app_name
    return replace(
        settings,
        app_name=claims["org_name"] if settings.app_name == default_name else settings.app_name,
        features=tuple(f for f in FEATURES if f in settings.features and f in claims["features"]),
        daily_token_cap=cap,
        allowed_domains=tuple(sorted(set(settings.allowed_domains) | set(claims["domains"]))),
        admin_emails=tuple(sorted(set(settings.admin_emails) | set(claims["admins"]))),
    )


class Tenants:
    def __init__(self, platform, factory, directory, box, clock, license_claims=None, llm=None, ttl=30.0):
        self.platform, self.factory, self.directory, self.box = platform, factory, directory, box
        self.clock, self.license_claims, self.llm = clock, license_claims, llm
        self._cache, self._flight = TTLCache(256, ttl), SingleFlight()

    @property
    def multi(self):
        return self.platform.deployment_mode == "multi_tenant"

    @property
    def default_org_id(self):
        return self.platform.org_id

    def invalidate(self, org_id):
        self._cache.set(org_id, None)

    def context(self, org_id):
        cached = self._cache.get(org_id)
        if cached is not None:
            return cached
        return self._flight.do(org_id, lambda: self._build_and_cache(org_id))

    def _build_and_cache(self, org_id):
        ctx = self._build_multi(org_id) if self.multi else self._build_single(org_id)
        self._cache.set(org_id, ctx)
        return ctx

    def _services(self, settings, store, overrides):
        return build_services(settings, store, llm=self.llm, rules_text=overrides.get("rules_yaml"),
                              template_text=overrides.get("report_template_text"))

    def _build_single(self, org_id):
        if org_id != self.platform.org_id:
            raise UnknownOrg(org_id)
        claims = self.license_claims
        settings = apply_claims(self.platform, claims) if claims else self.platform
        store = self.factory.for_org(org_id)
        name = settings.org_name or settings.app_name
        return OrgContext(org_id, name, claims["plan"] if claims else "self-hosted", settings, store,
                          self._services(settings, store, {}), claims or {}, claims["quotas"] if claims else {},
                          "active", claims["exp"] if claims else None)

    def _build_multi(self, org_id):
        org = self.directory.get_org(org_id)
        if org is None:
            raise UnknownOrg(org_id)
        claims, overrides = org["claims"], org["settings"]
        settings = apply_claims(apply_overrides(self.platform, overrides), claims)
        settings = self._with_llm_credentials(settings, org, overrides)
        store = self.factory.for_org(org_id)
        return OrgContext(org_id, org["name"], org["plan"], settings, store, self._services(settings, store, overrides),
                          claims, claims["quotas"], org["status"], claims["exp"])

    def _with_llm_credentials(self, settings, org, overrides):
        """Choose the LLM credentials for an organization.

        An organization that picks its own provider or endpoint must bring its own key: the platform key is never
        sent to an endpoint the organization chose. If its stored key cannot be read, the LLM is switched off.
        """
        try:
            own_key = self.box.decrypt(org["secrets_enc"]).get("llm_api_key", "")
        except SecretsUnavailable as exc:
            log.warning("organization %s: %s; the LLM is disabled for it", org["id"], exc)
            return replace(settings, llm_provider="none")
        if own_key:
            return replace(settings, llm_api_key=own_key)
        if any(name in overrides for name in OWN_ENDPOINT_FIELDS):
            return replace(settings, llm_api_key="")
        return settings

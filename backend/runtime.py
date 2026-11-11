"""The shared wiring of the application: storage, organizations, keys and the per-organization contexts.

Used by the web API (``create_app``) and by the background worker, so both see exactly the same world."""
from dataclasses import dataclass, replace

from . import orgkeys
from .config import ConfigError
from .db import StoreFactory, make_engine
from .directory import Directory
from .report import load_template
from .rules import load_rules
from .secrets_box import SecretBox
from .tenancy import Tenants
from . import schema


@dataclass
class Runtime:
    settings: object
    engine: object
    factory: object
    directory: object
    box: object
    verifier: object
    tenants: object
    license_claims: object

    @property
    def multi(self):
        return self.settings.deployment_mode == "multi_tenant"


def verify_license(settings, verifier):
    """Verify LICENSE_KEY at startup. Returns its sections, or None when there is no license.

    An expired license still starts the app, read-only, so a customer can always export their data."""
    if not settings.license_key:
        return None
    try:
        claims = verifier.verify(settings.license_key, allow_expired=True)
    except orgkeys.OrgKeyError as exc:
        raise ConfigError(f"LICENSE_KEY is not valid: {exc}") from exc
    if claims["mode"] != "license":
        raise ConfigError("LICENSE_KEY must be a license key (mode=license); tenant keys are for SaaS organizations")
    if settings.org_id not in ("default", claims["org_id"]):
        raise ConfigError(f"ORG_ID ({settings.org_id}) does not match the license's organization ({claims['org_id']})")
    return claims


def build_runtime(settings, clock, data_file="", llm=None, engine=None):
    if engine is None and settings.database_url:
        engine = make_engine(settings.database_url, settings.db_pool_size)
    if engine is not None:
        schema.migrate(engine, clock().isoformat())
    multi = settings.deployment_mode == "multi_tenant"
    factory = StoreFactory(engine, json_path=data_file or settings.data_file)
    directory = Directory(engine) if multi else None
    box = SecretBox(settings.secrets_key)
    public_pem = orgkeys.load_public_key(settings)
    verifier = orgkeys.KeyVerifier(public_pem, directory.revoked_ids if directory else (lambda: set()), clock) if public_pem else None
    license_claims = verify_license(settings, verifier)
    if license_claims:
        settings = replace(settings, org_id=license_claims["org_id"])  # the license defines the organization
    load_rules(settings.rules_file), load_template(settings.report_template)  # fail at startup, not on the first request
    tenants = Tenants(settings, factory, directory, box, clock, license_claims, llm)
    if not multi:
        tenants.context(settings.org_id)  # builds the one organization now, so bad configuration stops startup
    return Runtime(settings, engine, factory, directory, box, verifier, tenants, license_claims)

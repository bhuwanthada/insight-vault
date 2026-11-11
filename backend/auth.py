"""Who is calling, which organization they belong to, and what they may do.

Modes (AUTH_MODE): ``none`` (single local user, everything allowed), ``apikey`` (bearer keys) and
``sso`` (Google, LinkedIn, Microsoft or a generic OIDC provider, plus optional API keys for scripts).
Access is denied by default in sso mode: an address must be allow-listed, a member of an organization,
or belong to an allowed domain.

Credentials a script can send (``Authorization: Bearer`` or ``X-API-Key``):
* a signed **organization key**, which carries the organization and a role, or
* a static key from API_KEYS (single-organization mode only).
"""
import hmac
import re

from .orgkeys import OrgKeyError, looks_like_key

ROLE_ORDER = {"viewer": 1, "editor": 2, "approver": 3, "admin": 4}
PUBLIC_PATHS = ("/api/health", "/api/ready", "/api/config/public", "/api/org/activate")
PUBLIC_PREFIXES = ("/api/auth/",)
_DECISION = re.compile(r"/api/reports/[^/]+/decision")

# URL prefix -> the feature that must be enabled for the organization (first match wins)
FEATURE_GATES = (
    ("/api/insights/suggest", "ai_suggestions"),
    ("/api/insights", "insights"),
    ("/api/search", "search"),
    ("/api/connectors", "integrations"),
    ("/api/tickets/import", "integrations"),
    ("/api/report", "support"),
    ("/api/reports", "support"),
    ("/api/tickets", "support"),
)


def is_public(path):
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def required_feature(path):
    for prefix, feature in FEATURE_GATES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            return feature
    return None


def required_role(method, path):
    """The minimum role for a request. Reading needs viewer, changing data needs editor."""
    if path in ("/api/config", "/api/llm/test", "/api/org/settings", "/api/org/secrets", "/api/org/members", "/api/org/export") \
            or path.startswith(("/api/audit", "/api/connectors")) or path.startswith("/api/org/members/") \
            or (path == "/api/org" and method == "DELETE"):
        return "admin"
    if method == "POST" and _DECISION.fullmatch(path):
        return "approver"
    if method == "POST" and path == "/api/org/switch":
        return "viewer"  # choosing which of your organizations to look at changes nothing for anyone else
    return "viewer" if method in ("GET", "HEAD", "OPTIONS") else "editor"


def has_role(user, needed):
    return ROLE_ORDER[user["role"]] >= ROLE_ORDER[needed]


def local_user(org_id="default"):
    return {"email": "local", "name": "Local user", "role": "admin", "provider": "none", "org": org_id}


class AccessPolicy:
    """Single-organization rules from .env: who may sign in and which role they get."""

    def __init__(self, settings):
        self.s = settings
        self.listed = set(settings.allowed_emails) | set(settings.admin_emails) | set(settings.approver_emails) | set(settings.editor_emails)

    def allowed(self, email):
        domain = email.rpartition("@")[2]
        return email in self.listed or (bool(domain) and domain in self.s.allowed_domains)

    def role(self, email):
        if email in self.s.admin_emails:
            return "admin"
        if email in self.s.approver_emails:
            return "approver"
        if email in self.s.editor_emails:
            return "editor"
        return self.s.default_role


class EnvResolver:
    """single_org: the .env lists decide who may sign in."""

    def __init__(self, settings, org_id):
        self.policy, self.org_id = AccessPolicy(settings), org_id

    def memberships(self, email):
        return [{"org": self.org_id, "role": self.policy.role(email)}] if self.policy.allowed(email) else []


class DirectoryResolver:
    """multi_tenant: the membership table decides which organizations an e-mail belongs to."""

    def __init__(self, directory):
        self.directory = directory

    def memberships(self, email):
        return self.directory.memberships_for_email(email)


class Authenticator:
    def __init__(self, settings, resolver, key_verifier=None, default_org=None):
        self.s, self.resolver, self.keys, self.default_org = settings, resolver, key_verifier, default_org

    def _from_credential(self, headers):
        supplied = headers.get("X-API-Key") or headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not supplied:
            return None
        if self.keys is not None and looks_like_key(supplied):
            try:
                claims = self.keys.verify(supplied)
            except OrgKeyError:
                return None
            return {"email": "key:" + claims["jti"][:8], "name": f"{claims['org_name']} key", "role": claims["role"],
                    "provider": "orgkey", "org": claims["org_id"]}
        if self.default_org is None:  # static keys belong to the one organization of a single-org installation
            return None
        for name, role, key in self.s.api_keys:
            if hmac.compare_digest(supplied.encode(), key.encode()):
                return {"email": f"key:{name}", "name": name, "role": role, "provider": "apikey", "org": self.default_org}
        return None

    def _from_session(self, session):
        stored = session.get("user")
        if not stored:
            return None
        memberships = self.resolver.memberships(stored["email"])  # access removed in settings applies on the next request
        if not memberships:
            return None
        chosen = next((m for m in memberships if m["org"] == session.get("org")), memberships[0])
        return {**{k: stored[k] for k in ("email", "name", "provider")}, "role": chosen["role"], "org": chosen["org"],
                "orgs": [m["org"] for m in memberships]}

    def authenticate(self, headers, session):
        """Return the calling user (with their organization), or None when the request is not authenticated."""
        if self.s.auth_mode == "none":
            return local_user(self.default_org or "default")
        user = self._from_credential(headers)
        if user is None and self.s.auth_mode == "sso":
            user = self._from_session(session)
        return user

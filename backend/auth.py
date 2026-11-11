"""Who is calling, and what may they do.

Modes (AUTH_MODE): ``none`` (single local user, everything allowed), ``apikey`` (bearer keys) and
``sso`` (Google, LinkedIn, Microsoft or a generic OIDC provider, plus optional API keys for scripts).
Access is denied by default in sso mode: an address must be allow-listed or belong to an allowed domain.
"""
import hmac
import re

ROLE_ORDER = {"viewer": 1, "editor": 2, "approver": 3, "admin": 4}
PUBLIC_PATHS = ("/api/health", "/api/ready", "/api/config/public")
PUBLIC_PREFIXES = ("/api/auth/",)
_DECISION = re.compile(r"/api/reports/[^/]+/decision")


def is_public(path):
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def required_role(method, path):
    """The minimum role for a request. Reading needs viewer, changing data needs editor."""
    if path in ("/api/config", "/api/llm/test") or path.startswith("/api/audit"):
        return "admin"
    if method == "POST" and _DECISION.fullmatch(path):
        return "approver"
    return "viewer" if method in ("GET", "HEAD", "OPTIONS") else "editor"


def has_role(user, needed):
    return ROLE_ORDER[user["role"]] >= ROLE_ORDER[needed]


def local_user():
    return {"email": "local", "name": "Local user", "role": "admin", "provider": "none"}


class AccessPolicy:
    """Decides whether an e-mail may sign in and which role it gets."""

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


class Authenticator:
    def __init__(self, settings):
        self.s = settings
        self.policy = AccessPolicy(settings)

    def _from_api_key(self, headers):
        supplied = headers.get("X-API-Key") or headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not supplied:
            return None
        for name, role, key in self.s.api_keys:
            if hmac.compare_digest(supplied.encode(), key.encode()):
                return {"email": f"key:{name}", "name": name, "role": role, "provider": "apikey"}
        return None

    def _from_session(self, session):
        stored = session.get("user")
        if not stored:
            return None
        email = stored["email"]
        if not self.policy.allowed(email):  # access removed in .env takes effect on the next request
            return None
        return {**stored, "role": self.policy.role(email)}

    def authenticate(self, headers, session):
        """Return the calling user, or None when the request is not authenticated."""
        if self.s.auth_mode == "none":
            return local_user()
        user = self._from_api_key(headers)
        if user is None and self.s.auth_mode == "sso":
            user = self._from_session(session)
        return user

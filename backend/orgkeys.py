"""Signed organization keys: one key defines how the app runs for one organization.

A key is a standard signed token (ES256 JWS). The vendor signs it with a private key that never
leaves the vendor; the app only holds the public key, so it can verify keys offline and nobody can
forge or edit one. A key carries these *sections* (claims):

    sub / org      organization id and display name
    plan           free-form plan label (starter, growth, enterprise, ...)
    role           role granted when the key is used as an API credential
    features       which parts of the product are enabled
    quotas         limits: tickets, llm_tokens_per_day, seats, connectors (0 = unlimited)
    domains        e-mail domains that belong to the organization
    admins         e-mail addresses that become administrators when the organization is activated
    mode           "license" (a single-company installation) or "tenant" (a SaaS organization)
    jti, iat, exp  key id, issue time, expiry

A signed key is readable by anyone who holds it. It must never contain a password or API key.
"""
import base64
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from joserfc import jwt
from joserfc.errors import ExpiredTokenError, JoseError
from joserfc.jwk import ECKey

from .config import FEATURES, ROLES

ISSUER = "insight-vault"
ALGORITHM = "ES256"
QUOTA_NAMES = ("tickets", "llm_tokens_per_day", "seats", "connectors")
MODES = ("license", "tenant")
_ORG_ID = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")


class OrgKeyError(ValueError):
    """The key is missing, malformed, forged, expired or revoked."""


def generate_keypair():
    """Return (private_pem, public_pem). Keep the private half offline; ship only the public half."""
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private_pem.decode(), public_pem.decode()


def _clean_quotas(quotas):
    out = {}
    for name, value in (quotas or {}).items():
        if name not in QUOTA_NAMES:
            raise OrgKeyError(f"unknown quota '{name}' (allowed: {', '.join(QUOTA_NAMES)})")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise OrgKeyError(f"quota '{name}' must be a whole number, 0 or more")
        out[name] = value
    return out


def _validated(claims):
    """Check and normalise the sections of a key. Raises OrgKeyError with a precise reason."""
    org = claims.get("org") if isinstance(claims.get("org"), dict) else {}
    org_id = str(claims.get("sub", ""))
    if not _ORG_ID.fullmatch(org_id):
        raise OrgKeyError("organization id must be 2-63 characters: lowercase letters, digits, '-' or '_'")
    if claims.get("role") not in ROLES:
        raise OrgKeyError(f"role must be one of: {', '.join(ROLES)}")
    if claims.get("mode") not in MODES:
        raise OrgKeyError(f"mode must be one of: {', '.join(MODES)}")
    features = claims.get("features")
    if not isinstance(features, list) or not features or any(f not in FEATURES for f in features) or "notes" not in features:
        raise OrgKeyError(f"features must include notes and only use: {', '.join(FEATURES)}")
    return {
        "org_id": org_id,
        "org_name": str(org.get("name") or org_id),
        "plan": str(claims.get("plan") or "starter")[:32],
        "role": claims["role"],
        "mode": claims["mode"],
        "features": list(dict.fromkeys(features)),
        "quotas": _clean_quotas(claims.get("quotas")),
        "domains": sorted({str(d).strip().lower() for d in claims.get("domains") or [] if str(d).strip()}),
        "admins": sorted({str(a).strip().lower() for a in claims.get("admins") or [] if str(a).strip()}),
        "jti": str(claims.get("jti", "")),
        "iat": int(claims.get("iat", 0)),
        "exp": int(claims["exp"]),
    }


def mint(private_pem, *, org_id, org_name="", plan="starter", role="editor", mode="tenant", features=FEATURES,
         quotas=None, domains=(), admins=(), days=365, now=None):
    """Create a signed key. Raises OrgKeyError if any section is invalid."""
    now = now or datetime.now(timezone.utc)
    claims = {
        "iss": ISSUER, "sub": org_id, "jti": uuid.uuid4().hex, "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=days)).timestamp()),
        "org": {"name": org_name or org_id}, "plan": plan, "role": role, "mode": mode,
        "features": list(features), "quotas": quotas or {}, "domains": list(domains), "admins": list(admins),
    }
    _validated(claims)  # never sign something the app would reject
    key = ECKey.import_key(private_pem)
    return jwt.encode({"alg": ALGORITHM, "typ": "JWT"}, claims, key, algorithms=[ALGORITHM])  # only ES256: no algorithm confusion


def verify(token, public_pem, *, now=None, revoked=(), allow_expired=False):
    """Return the validated sections of a key, or raise OrgKeyError.

    ``allow_expired`` is for license keys at startup: an expired license starts the app read-only instead of refusing."""
    now = now or datetime.now(timezone.utc)
    try:
        decoded = jwt.decode(token.strip(), ECKey.import_key(public_pem), algorithms=[ALGORITHM])
        try:
            jwt.JWTClaimsRegistry(now=int(now.timestamp()), exp={"essential": True}).validate(decoded.claims)
        except ExpiredTokenError:
            if not allow_expired:  # every other claim problem still rejects the key
                raise
    except (JoseError, ValueError, TypeError) as exc:  # forged, expired, malformed, or an unusable public key
        raise OrgKeyError(f"invalid key: {exc}") from exc
    claims = dict(decoded.claims)
    if claims.get("iss") != ISSUER:
        raise OrgKeyError("invalid key: wrong issuer")
    sections = _validated(claims)
    if sections["jti"] in set(revoked):
        raise OrgKeyError("this key has been revoked")
    return sections


def inspect(token):
    """Decode a key WITHOUT verifying it (for debugging only: never trust the result)."""
    try:
        payload = token.strip().split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise OrgKeyError("unreadable key: not a signed key") from exc


def looks_like_key(text):
    """Cheap check used to tell an org key from a plain API key: three dot-separated base64url parts."""
    return bool(re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", text or ""))


def load_public_key(settings):
    """The PEM used to verify keys: inline value, or the contents of the configured file. '' if none."""
    if settings.org_key_public_key:
        return settings.org_key_public_key
    if settings.org_key_public_key_file:
        with open(settings.org_key_public_key_file, encoding="utf-8") as handle:
            return handle.read()
    return ""


class KeyVerifier:
    """Verifies org keys with the configured public key and the current revocation list (cached briefly)."""

    def __init__(self, public_pem, revoked_provider=lambda: set(), clock=lambda: datetime.now(timezone.utc), cache_seconds=15.0):
        self.public_pem, self._revoked, self._clock = public_pem, revoked_provider, clock
        self._cache_seconds, self._cached, self._cached_at = cache_seconds, set(), None

    def revoked(self):
        now = self._clock().timestamp()
        if self._cached_at is None or now - self._cached_at >= self._cache_seconds:
            self._cached, self._cached_at = set(self._revoked()), now
        return self._cached

    def refresh(self):
        """Forget the cached revocation list (called right after a revocation in this process)."""
        self._cached_at = None

    def verify(self, token, allow_expired=False):
        return verify(token, self.public_pem, now=self._clock(), revoked=self.revoked(), allow_expired=allow_expired)

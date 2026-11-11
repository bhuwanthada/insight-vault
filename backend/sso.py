"""Single sign-on with OpenID Connect (authorization code flow with PKCE) via Authlib.

Supported out of the box: Google, LinkedIn, Microsoft (single tenant) and one generic OIDC provider.
The browser is sent to the provider, comes back to /api/auth/callback/<provider>, and receives a
signed, HTTP-only session cookie. Roles are recomputed from .env on every request.
"""
import logging

from authlib.integrations.flask_client import OAuth
from flask import jsonify, redirect, request, session

log = logging.getLogger("insight_vault.sso")
VERIFIED_EMAIL_REQUIRED = {"google", "linkedin", "oidc"}  # Microsoft e-mails are not verified claims; see config checks


def build_oauth(app, settings):
    oauth = OAuth(app)
    for provider_id, _label, client_id, client_secret, metadata_url in settings.sso_providers():
        oauth.register(
            name=provider_id,
            client_id=client_id,
            client_secret=client_secret,
            server_metadata_url=metadata_url,
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
    return oauth


def register_routes(app, settings, authenticator, oauth, audit):
    labels = {pid: label for pid, label, *_ in settings.sso_providers()}
    home = settings.public_url + "/"

    def fail(reason):
        return redirect(f"{home}?login_error={reason}")

    @app.get("/api/auth/providers")
    def providers():
        shown = [{"id": pid, "label": label} for pid, label in labels.items()] if settings.auth_mode == "sso" else []
        return jsonify({"mode": settings.auth_mode, "providers": shown})

    @app.get("/api/auth/me")
    def me():
        user = authenticator.authenticate(request.headers, session)
        return jsonify({"authenticated": user is not None, "user": user, "mode": settings.auth_mode})

    @app.get("/api/auth/login/<provider>")
    def login(provider):
        if provider not in labels or settings.auth_mode != "sso":
            return jsonify({"error": "unknown sign-in provider"}), 404
        return oauth.create_client(provider).authorize_redirect(f"{settings.public_url}/api/auth/callback/{provider}")

    @app.get("/api/auth/callback/<provider>")
    def callback(provider):
        if provider not in labels or settings.auth_mode != "sso":
            return jsonify({"error": "unknown sign-in provider"}), 404
        try:
            token = oauth.create_client(provider).authorize_access_token()
        except Exception:  # state mismatch, denied consent, network or token errors: never leak details to the browser
            log.warning("sign-in with %s failed", provider, exc_info=True)
            audit("anonymous", "login_failed", provider)
            return fail("failed")
        info = token.get("userinfo") or {}
        email = str(info.get("email", "")).strip().lower()
        if provider in VERIFIED_EMAIL_REQUIRED and str(info.get("email_verified")).lower() != "true":
            audit(email or "anonymous", "login_denied", f"{provider}: e-mail not verified")
            return fail("denied")
        if not email or not authenticator.policy.allowed(email):
            audit(email or "anonymous", "login_denied", f"{provider}: not allowed")
            return fail("denied")
        session.clear()  # fresh session on login (prevents session fixation)
        session["user"] = {"email": email, "name": str(info.get("name") or email), "provider": provider}
        session.permanent = True
        audit(email, "login", provider)
        return redirect(home)

    @app.post("/api/auth/logout")
    def logout():
        user = session.get("user")
        session.clear()
        if user:
            audit(user["email"], "logout", user["provider"])
        return jsonify({"ok": True})

"""Organization endpoints: activation by key, info, settings, secrets, members, export, deletion,
and the platform operator API (multi-tenant mode)."""
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import Response, g, jsonify, request, session

from .auth import ROLE_ORDER
from .orgkeys import OrgKeyError
from .secrets_box import SecretsUnavailable
from .tenancy import OVERRIDABLE, TEXT_OVERRIDES, QuotaExceeded, ensure_room, validate_overrides

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass
class OrgEnv:
    """Everything the organization endpoints need, assembled once by create_app."""
    settings: object
    tenants: object
    directory: object
    verifier: object
    box: object
    audit: object  # audit(actor, action, detail="", org_id=None)
    clock: object
    repo: object = None  # the connector registry, when a database is configured


def _error(message, status):
    return jsonify({"error": message}), status


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def platform_guard(env):
    """Authenticate the SaaS operator for /api/platform/*. Returns an error response, or None when allowed."""
    if not env.tenants.multi:
        return _error("not found", 404)
    supplied = request.headers.get("X-Platform-Key", "")
    for name, key in env.settings.platform_admin_keys:
        if hmac.compare_digest(supplied.encode(), key.encode()):
            g.platform_actor = f"platform:{name}"
            return None
    return _error("platform key required", 401)


def register(app, env):
    multi = env.tenants.multi

    def body():
        return request.get_json(silent=True) or {}

    def only_multi():
        return None if multi else _error("available only in multi-tenant mode", 404)

    # ------------------------------------------------------------ onboarding
    @app.post("/api/org/activate")
    def activate():
        if not multi:
            return _error("available only in multi-tenant mode", 404)
        try:
            claims = env.verifier.verify(str(body().get("key", "")))
        except OrgKeyError as exc:
            return _error(str(exc), 400)
        if claims["mode"] != "tenant":
            return _error("this is a license key; activating an organization needs a tenant key", 400)
        org, created = env.directory.activate(claims, env.clock().isoformat())
        env.tenants.invalidate(org["id"])
        env.audit("activation", "org_activated" if created else "org_key_renewed", claims["jti"], org_id=org["id"])
        return jsonify({"org": {"id": org["id"], "name": org["name"], "plan": org["plan"], "features": claims["features"],
                               "quotas": claims["quotas"], "expiresAt": _iso(claims["exp"])},
                        "created": created, "admins": claims["admins"]}), 201 if created else 200

    # ------------------------------------------------------------ information
    @app.get("/api/org")
    def info():
        ctx = g.ctx
        now_ts = int(env.clock().timestamp())
        return jsonify({
            "id": ctx.org_id, "name": ctx.name, "plan": ctx.plan, "status": ctx.status,
            "deploymentMode": env.settings.deployment_mode, "features": list(ctx.settings.features), "quotas": ctx.quotas,
            "usage": {
                "tickets": ctx.store.ticket_count(),
                "seats": env.directory.count_members(ctx.org_id) if multi else None,
                "tokensToday": ctx.services.usage.snapshot()["tokens"],
                "connectors": env.repo.count(ctx.org_id) if env.repo else 0,
            },
            "expiresAt": _iso(ctx.exp), "readOnly": ctx.read_only(now_ts),
        })

    @app.get("/api/org/export")
    def export_everything():
        ctx = g.ctx
        data = {
            "org": {"id": ctx.org_id, "name": ctx.name, "plan": ctx.plan},
            "exportedAt": env.clock().isoformat(),
            **ctx.store.load(),
            "tickets": ctx.store.tickets(),
            "audit": ctx.store.audit(1_000_000),
            "members": env.directory.list_members(ctx.org_id) if multi else [],
            "settings": env.directory.get_org(ctx.org_id)["settings"] if multi else {},
        }
        return Response(json.dumps(data, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": 'attachment; filename="insight-vault-export.json"'})

    # ------------------------------------------------------------ settings and secrets (multi-tenant)
    @app.get("/api/org/settings")
    def get_settings():
        refusal = only_multi()
        if refusal:
            return refusal
        org = env.directory.get_org(g.ctx.org_id)
        effective = {name: (list(v) if isinstance(v, tuple) else v)
                     for name, v in ((n, getattr(g.ctx.settings, n)) for n in sorted(OVERRIDABLE))}
        return jsonify({"overrides": org["settings"], "effective": effective, "hasLlmKey": bool(org["secrets_enc"]),
                        "editable": sorted(OVERRIDABLE | TEXT_OVERRIDES)})

    @app.put("/api/org/settings")
    def put_settings():
        refusal = only_multi()
        if refusal:
            return refusal
        overrides = body()
        problems = validate_overrides(env.settings, overrides)
        if problems:
            return jsonify({"error": "; ".join(problems), "problems": problems}), 400
        env.directory.update_org(g.ctx.org_id, settings=overrides)
        env.tenants.invalidate(g.ctx.org_id)
        return jsonify({"ok": True})

    @app.put("/api/org/secrets")
    def put_secrets():
        refusal = only_multi()
        if refusal:
            return refusal
        data = body()
        if "llm_api_key" not in data or not isinstance(data["llm_api_key"], str):
            return _error("llm_api_key (text) is required; send an empty string to remove it", 400)
        org = env.directory.get_org(g.ctx.org_id)
        try:
            secrets_now = env.box.decrypt(org["secrets_enc"])
            if data["llm_api_key"].strip():
                secrets_now["llm_api_key"] = data["llm_api_key"].strip()
            else:
                secrets_now.pop("llm_api_key", None)
            env.directory.update_org(g.ctx.org_id, secrets_enc=env.box.encrypt(secrets_now))
        except SecretsUnavailable as exc:
            return _error(str(exc), 503)
        env.tenants.invalidate(g.ctx.org_id)
        return jsonify({"llm_api_key": "set" if secrets_now.get("llm_api_key") else "not set"})

    # ------------------------------------------------------------ members (multi-tenant)
    @app.get("/api/org/members")
    def list_members():
        refusal = only_multi()
        return refusal or jsonify(env.directory.list_members(g.ctx.org_id))

    @app.post("/api/org/members")
    def add_member():
        refusal = only_multi()
        if refusal:
            return refusal
        data = body()
        email, role = str(data.get("email", "")).strip().lower(), data.get("role")
        if not _EMAIL.fullmatch(email):
            return _error("a valid e-mail address is required", 400)
        if role not in ROLE_ORDER:
            return _error(f"role must be one of: {', '.join(ROLE_ORDER)}", 400)
        known = {m["email"] for m in env.directory.list_members(g.ctx.org_id)}
        try:
            if email not in known:
                ensure_room(g.ctx.quotas, "seats", len(known))
        except QuotaExceeded as exc:
            return _error(str(exc), 402)
        env.directory.set_member(g.ctx.org_id, email, role, env.clock().isoformat())
        return jsonify({"email": email, "role": role}), 201

    @app.delete("/api/org/members/<email>")
    def remove_member(email):
        refusal = only_multi()
        if refusal:
            return refusal
        email = email.lower()
        members = env.directory.list_members(g.ctx.org_id)
        target = next((m for m in members if m["email"] == email), None)
        if target is None:
            return _error("member not found", 404)
        if target["role"] == "admin" and sum(m["role"] == "admin" for m in members) == 1:
            return _error("an organization needs at least one administrator", 409)
        env.directory.remove_member(g.ctx.org_id, email)
        return jsonify({"ok": True})

    @app.post("/api/org/switch")
    def switch_org():
        """Choose which of your organizations the browser session acts for."""
        wanted = str(body().get("org", ""))
        if wanted not in g.user.get("orgs", []):
            return _error("you are not a member of that organization", 403)
        session["org"] = wanted
        return jsonify({"ok": True, "org": wanted})

    # ------------------------------------------------------------ deletion
    @app.delete("/api/org")
    def delete_org():
        refusal = only_multi()
        if refusal:
            return refusal
        org_id = g.ctx.org_id
        if body().get("confirm") != org_id:
            return _error("send {\"confirm\": \"<organization id>\"} to permanently delete all of this organization's data", 400)
        env.audit(g.user["email"], "org_deleted", org_id, org_id=org_id)
        env.directory.delete_org(org_id)
        env.tenants.invalidate(org_id)
        session.clear()
        return jsonify({"deleted": True})

    # ------------------------------------------------------------ platform operator API
    @app.get("/api/platform/orgs")
    def platform_orgs():
        return jsonify([{"id": o["id"], "name": o["name"], "plan": o["plan"], "status": o["status"],
                         "keyExpiresAt": _iso(int(o["key_exp"])) if o["key_exp"] else None,
                         "members": env.directory.count_members(o["id"])} for o in env.directory.list_orgs()])

    @app.post("/api/platform/orgs/<org_id>/status")
    def platform_status(org_id):
        status = body().get("status")
        if status not in ("active", "suspended"):
            return _error("status must be 'active' or 'suspended'", 400)
        if env.directory.get_org(org_id) is None:
            return _error("organization not found", 404)
        env.directory.update_org(org_id, status=status)
        env.tenants.invalidate(org_id)
        env.audit(g.platform_actor, f"org_{status}", org_id, org_id=org_id)
        return jsonify({"id": org_id, "status": status})

    @app.post("/api/platform/keys/revoke")
    def platform_revoke():
        data = body()
        jti = str(data.get("jti", "")).strip()
        if not jti:
            return _error("jti (the key id) is required", 400)
        env.directory.revoke_key(jti, str(data.get("reason", "")), env.clock().isoformat())
        env.verifier.refresh()  # takes effect immediately here; other processes pick it up within seconds
        return jsonify({"revoked": jti})

    @app.delete("/api/platform/orgs/<org_id>")
    def platform_delete(org_id):
        if env.directory.get_org(org_id) is None:
            return _error("organization not found", 404)
        if body().get("confirm") != org_id:
            return _error("send {\"confirm\": \"<organization id>\"} to permanently delete the organization", 400)
        env.directory.delete_org(org_id)
        env.tenants.invalidate(org_id)
        return jsonify({"deleted": org_id})

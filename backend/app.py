"""Insight Vault JSON API (Flask)."""
import logging
import os
import secrets
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from flask import Flask, Response, g, jsonify, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.local import LocalProxy
from werkzeug.middleware.proxy_fix import ProxyFix

from . import orgkeys, orgs, schema, sso, support
from .analysis import analyze
from .auth import Authenticator, DirectoryResolver, EnvResolver, has_role, is_public, required_feature, required_role
from .config import ConfigError, Settings
from .db import StoreFactory, make_engine
from .directory import Directory
from .limits import RateLimiter
from .observability import Metrics
from .report import load_template
from .rules import load_rules
from .secrets_box import SecretBox
from .tenancy import QuotaExceeded, Tenants, UnknownOrg

log = logging.getLogger("insight_vault")

DEFAULT_DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "db.json")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
LLM_PATHS = ("/api/insights/suggest", "/api/report", "/api/reports", "/api/llm/test")  # endpoints that may call the LLM
KEY_PROVIDERS = ("apikey", "orgkey")  # script credentials are not browser sessions, so no cross-site check applies


def _uid():
    return secrets.token_hex(6)


def _utcnow():
    return datetime.now(timezone.utc)


def _now():
    return _utcnow().isoformat()


def _clean_tags(tags):
    seen = []
    for tag in tags or []:
        tag = str(tag).strip().lower()
        if tag and tag not in seen:
            seen.append(tag)
    return seen


def _error(message, status):
    return jsonify({"error": message}), status


def export_markdown(db):
    highlights = {h["id"]: h for h in db["highlights"]}
    titles = {n["id"]: n["title"] for n in db["notes"]}
    lines = ["# Insight Vault export", ""]
    for insight in db["insights"]:
        lines += [f"## {insight['title']}", "", insight["description"], ""]
        for hid in insight["highlightIds"]:
            h = highlights.get(hid)
            if h:
                quote = " ".join(h["text"].split())
                lines += [f"> {quote}", f"> - *{titles.get(h['noteId'], 'unknown')}* [{', '.join(h['tags'])}]", ""]
    return "\n".join(lines) + "\n"


def _license(settings, verifier):
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


def create_app(data_file=None, settings=None, llm=None, clock=_utcnow, oauth=None, engine=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=settings.max_body_mb * 1024 * 1024,
        SECRET_KEY=settings.secret_key or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=settings.session_hours),
    )
    if settings.trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=settings.trust_proxy, x_proto=settings.trust_proxy, x_host=settings.trust_proxy)

    # ---- storage, organizations and credentials
    if engine is None and settings.database_url:
        engine = make_engine(settings.database_url, settings.db_pool_size)
    if engine is not None:
        schema.migrate(engine, clock().isoformat())
    multi = settings.deployment_mode == "multi_tenant"
    factory = StoreFactory(engine, json_path=data_file or settings.data_file or DEFAULT_DATA_FILE)
    directory = Directory(engine) if multi else None
    box = SecretBox(settings.secrets_key)
    public_pem = orgkeys.load_public_key(settings)
    verifier = orgkeys.KeyVerifier(public_pem, directory.revoked_ids if directory else (lambda: set()), clock) if public_pem else None
    license_claims = _license(settings, verifier)
    if license_claims:
        settings = replace(settings, org_id=license_claims["org_id"])  # the license defines the organization
    load_rules(settings.rules_file), load_template(settings.report_template)  # fail at startup, not on the first request
    tenants = Tenants(settings, factory, directory, box, clock, license_claims, llm)
    if not multi:
        tenants.context(settings.org_id)  # builds the one organization now, so bad configuration stops startup
    app.extensions["insight_vault"] = {"tenants": tenants, "directory": directory, "factory": factory, "verifier": verifier}
    resolver = DirectoryResolver(directory) if multi else EnvResolver(tenants.context(settings.org_id).settings, settings.org_id)  # includes the license's domains and admins
    authenticator = Authenticator(settings, resolver, verifier, default_org=None if multi else settings.org_id)
    limiter, llm_limiter = RateLimiter(settings.rate_limit), RateLimiter(settings.rate_limit_llm)
    allowed_origins = {settings.public_url, *settings.cors_origins}
    platform_metrics = Metrics()

    def audit(actor, action, detail="", org_id=None):
        """Record who did what, in the audit trail of the organization it happened in."""
        org_id = org_id or (g.ctx.org_id if g.ctx else (None if multi else settings.org_id))
        if org_id is None:  # e.g. a sign-in attempt that belongs to no organization
            log.info("audit (no organization): %s %s %s", actor, action, detail)
            return
        factory.for_org(org_id).append_audit(
            {"at": clock().isoformat(), "actor": actor, "action": action, "detail": detail}, settings.audit_retention)

    # What the routes below call "store", "services" and "cfg" always means the CURRENT request's organization.
    store = LocalProxy(lambda: g.ctx.store)
    services = LocalProxy(lambda: g.ctx.services)
    cfg = LocalProxy(lambda: g.ctx.settings)

    @app.before_request
    def guard():
        g.request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        g.started = time.monotonic()
        g.user, g.ctx = None, None
        path = request.path
        if not path.startswith("/api/"):
            return None
        if request.method == "OPTIONS":  # CORS preflight; headers are added in after_request
            return Response(status=204)
        if path not in ("/api/health", "/api/ready"):
            if not limiter.allow(request.remote_addr)[0] or (
                path in LLM_PATHS and request.method != "GET" and not llm_limiter.allow(request.remote_addr)[0]
            ):
                response = _error("too many requests; slow down", 429)
                response[0].headers["Retry-After"] = "60"
                return response
        if path.startswith("/api/platform/"):
            return orgs.platform_guard(env)
        if is_public(path):
            return None
        user = authenticator.authenticate(request.headers, session)
        if user is None:
            return _error("authentication required", 401)
        try:
            ctx = tenants.context(user["org"])
        except UnknownOrg:
            return _error("organization not found", 403)
        if ctx.status != "active":
            return _error("this organization is suspended", 403)
        origin = request.headers.get("Origin")
        if settings.auth_mode == "sso" and user["provider"] not in KEY_PROVIDERS and request.method in UNSAFE_METHODS and origin and origin not in allowed_origins:
            return _error("cross-site request blocked", 403)
        needed = required_role(request.method, path)
        if not has_role(user, needed):
            return _error(f"this action requires the {needed} role", 403)
        feature = required_feature(path)
        if feature and feature not in ctx.settings.features:
            return _error(f"'{feature}' is not included in this organization's plan", 403)
        if request.method in UNSAFE_METHODS and ctx.read_only(int(clock().timestamp())):
            return _error("the license for this organization has expired; it is read-only until renewed", 402)
        g.user, g.ctx = user, ctx
        return None

    @app.after_request
    def finish(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        origin = request.headers.get("Origin")
        if origin in settings.cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key, X-Platform-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response.headers["Vary"] = "Origin"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            (g.ctx.services.metrics if g.ctx else platform_metrics).inc(
                "http_requests_total", method=request.method, status=f"{response.status_code // 100}xx")
            log.info("request", extra={"method": request.method, "path": request.path, "status": response.status_code,
                                       "ms": round((time.monotonic() - g.started) * 1000), "user": (g.user or {}).get("email", "-")})
            if request.method in UNSAFE_METHODS and response.status_code < 400 and g.user and not request.path.startswith("/api/auth/"):
                audit(g.user["email"], f"{request.method} {request.path}", str(response.status_code))
        return response

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/ready")
    def ready():
        try:
            factory.for_org(settings.org_id).check()  # proves the data store is reachable
        except (OSError, sa.exc.SQLAlchemyError) as exc:
            return _error(f"data store unavailable: {exc}", 503)
        return jsonify({"status": "ready"})

    @app.get("/api/config/public")
    def public_config():
        shown = settings if multi else tenants.context(settings.org_id).settings
        user = authenticator.authenticate(request.headers, session)
        if user is not None:
            try:
                shown = tenants.context(user["org"]).settings
            except UnknownOrg:
                pass  # a stale session: show the platform defaults
        return jsonify({
            "appName": shown.app_name, "accentColor": shown.accent_color, "defaultTheme": shown.default_theme,
            "features": list(shown.features), "windows": list(shown.windows), "defaultDays": shown.default_days,
            "locale": shown.locale, "authMode": settings.auth_mode, "deploymentMode": settings.deployment_mode,
        })

    @app.get("/api/config")
    def admin_config():
        return jsonify(cfg.masked())

    @app.get("/api/usage")
    def usage():
        return jsonify({**services.usage.snapshot(), "session": dict(services.llm.stats)})

    @app.get("/api/metrics")
    def metrics():
        extra = {f"llm_{k}_total": v for k, v in services.llm.stats.items()}
        extra.update(cache_hits_total=services.cache.hits, cache_misses_total=services.cache.misses, cache_entries=len(services.cache))
        return Response(services.metrics.render(extra), mimetype="text/plain")

    @app.get("/api/audit")
    def audit_log():
        limit = request.args.get("limit", "100")
        return jsonify(store.audit(int(limit) if limit.isdigit() and int(limit) > 0 else 100))

    @app.get("/api/llm/status")
    def llm_status():
        return jsonify({
            "enabled": services.llm.enabled, "provider": cfg.llm_provider, "model": cfg.llm_model or None,
            "fallbacks": list(cfg.llm_fallbacks), "circuits": services.llm.circuits(),
            "piiMasking": cfg.pii_masking, "usage": services.usage.snapshot(),
        })

    @app.post("/api/llm/test")
    def llm_test():
        return jsonify(services.llm.ping())

    sso.register_routes(app, settings, authenticator, oauth or (sso.build_oauth(app, settings) if settings.auth_mode == "sso" else None), audit)
    env = orgs.OrgEnv(settings, tenants, directory, verifier, box, audit, clock)
    orgs.register(app, env)

    def body():
        return request.get_json(silent=True) or {}

    @app.get("/api/state")
    def state():
        db = store.load(only=("notes", "highlights", "insights"))
        notes = [{k: v for k, v in n.items() if k != "text"} | {"length": len(n["text"])} for n in db["notes"]]
        return jsonify({"notes": notes, "highlights": db["highlights"], "insights": db["insights"]})

    @app.post("/api/notes")
    def add_note():
        data = body()
        if not data.get("title") or not data.get("text"):
            return _error("title and text are required", 400)
        with store.lock:
            db = store.load(only=('notes',))
            note = {
                "id": _uid(),
                "title": str(data["title"])[:200],
                "source": data.get("source") or "interview",
                "text": str(data["text"]),
                "createdAt": _now(),
            }
            db["notes"].append(note)
            store.save(db)
        return jsonify(note), 201

    def find(items, item_id):
        return next((i for i in items if i["id"] == item_id), None)

    @app.get("/api/notes/<note_id>")
    def get_note(note_id):
        note = find(store.load(only=('notes',))["notes"], note_id)
        return jsonify(note) if note else _error("note not found", 404)

    @app.get("/api/notes/<note_id>/analysis")
    def note_analysis(note_id):
        note = find(store.load(only=('notes',))["notes"], note_id)
        return jsonify(analyze(note["text"])) if note else _error("note not found", 404)

    @app.delete("/api/notes/<note_id>")
    def delete_note(note_id):
        with store.lock:
            db = store.load(only=('notes', 'highlights', 'insights'))
            if not find(db["notes"], note_id):
                return _error("note not found", 404)
            gone = {h["id"] for h in db["highlights"] if h["noteId"] == note_id}
            db["notes"] = [n for n in db["notes"] if n["id"] != note_id]
            db["highlights"] = [h for h in db["highlights"] if h["noteId"] != note_id]
            for insight in db["insights"]:
                insight["highlightIds"] = [i for i in insight["highlightIds"] if i not in gone]
            store.save(db)
        return jsonify({"ok": True})

    @app.post("/api/highlights")
    def add_highlight():
        data = body()
        with store.lock:
            db = store.load(only=('notes', 'highlights'))
            note = find(db["notes"], data.get("noteId"))
            if not note:
                return _error("note not found", 404)
            start, end = data.get("start"), data.get("end")
            valid = all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end))
            if not valid or start < 0 or end <= start or end > len(note["text"]):
                return _error("invalid range", 400)
            highlight = {
                "id": _uid(),
                "noteId": note["id"],
                "start": start,
                "end": end,
                "text": note["text"][start:end],
                "tags": _clean_tags(data.get("tags")),
                "createdAt": _now(),
            }
            db["highlights"].append(highlight)
            store.save(db)
        return jsonify(highlight), 201

    @app.patch("/api/highlights/<highlight_id>")
    def retag_highlight(highlight_id):
        with store.lock:
            db = store.load(only=('highlights',))
            highlight = find(db["highlights"], highlight_id)
            if not highlight:
                return _error("highlight not found", 404)
            highlight["tags"] = _clean_tags(body().get("tags"))
            store.save(db)
        return jsonify(highlight)

    @app.delete("/api/highlights/<highlight_id>")
    def delete_highlight(highlight_id):
        with store.lock:
            db = store.load(only=('highlights', 'insights'))
            if not find(db["highlights"], highlight_id):
                return _error("highlight not found", 404)
            db["highlights"] = [h for h in db["highlights"] if h["id"] != highlight_id]
            for insight in db["insights"]:
                insight["highlightIds"] = [i for i in insight["highlightIds"] if i != highlight_id]
            store.save(db)
        return jsonify({"ok": True})

    @app.post("/api/insights")
    def add_insight():
        data = body()
        if not data.get("title"):
            return _error("title is required", 400)
        with store.lock:
            db = store.load(only=('highlights', 'insights'))
            known = {h["id"] for h in db["highlights"]}
            insight = {
                "id": _uid(),
                "title": str(data["title"]),
                "description": data.get("description") or "",
                "highlightIds": [i for i in data.get("highlightIds") or [] if i in known],
                "createdAt": _now(),
            }
            db["insights"].append(insight)
            store.save(db)
        return jsonify(insight), 201

    @app.post("/api/insights/suggest")
    def suggest_insights():
        db = store.load(only=("highlights",))
        wanted = body().get("highlightIds")
        highlights = [h for h in db["highlights"] if wanted is None or h["id"] in wanted]
        return jsonify(services.suggester.suggest(highlights))

    @app.delete("/api/insights/<insight_id>")
    def delete_insight(insight_id):
        with store.lock:
            db = store.load(only=('insights',))
            if not find(db["insights"], insight_id):
                return _error("insight not found", 404)
            db["insights"] = [i for i in db["insights"] if i["id"] != insight_id]
            store.save(db)
        return jsonify({"ok": True})

    @app.get("/api/search")
    def search():
        query = request.args.get("q", "").lower().strip()
        if not query:
            return jsonify([])
        db = store.load(only=("notes", "highlights"))
        results = []
        for note in db["notes"]:
            index = note["text"].lower().find(query)
            if index >= 0 or query in note["title"].lower():
                snippet = note["text"][max(0, index - 40) : index + 100] if index >= 0 else note["text"][:100]
                results.append({"type": "note", "noteId": note["id"], "title": note["title"], "snippet": snippet})
        for h in db["highlights"]:
            if query in h["text"].lower() or any(query in t for t in h["tags"]):
                results.append(
                    {"type": "highlight", "noteId": h["noteId"], "title": ", ".join(h["tags"]) or "highlight", "snippet": h["text"]}
                )
        return jsonify(results)

    @app.get("/api/export")
    def export():
        return Response(export_markdown(store.load(only=("notes", "highlights", "insights"))), mimetype="text/markdown")

    @app.errorhandler(Exception)
    def handle_error(exc):
        if isinstance(exc, HTTPException):
            return _error(exc.name.lower(), exc.code)
        if isinstance(exc, QuotaExceeded):
            return _error(str(exc), 402)
        log.exception("unhandled error on %s %s", request.method, request.path)
        return _error("internal server error", 500)

    support.register(app, store, services, clock, _uid, _now)
    return app


def main():
    from dotenv import load_dotenv
    from waitress import serve  # production WSGI server (the Flask dev server is not meant for production)

    from .observability import configure_logging

    load_dotenv(os.environ.get("ENV_FILE") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    settings = Settings.from_env()
    configure_logging(settings.log_level, settings.log_format)
    log.info("Insight Vault API on http://%s:%d (mode=%s, auth=%s, llm=%s)", settings.host, settings.port, settings.deployment_mode,
             settings.auth_mode, f"{settings.llm_provider}:{settings.llm_model}" if settings.llm_enabled else "off")
    serve(create_app(settings=settings), host=settings.host, port=settings.port, threads=settings.threads)

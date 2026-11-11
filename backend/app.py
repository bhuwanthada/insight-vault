"""Insight Vault JSON API (Flask)."""
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, g, jsonify, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from . import sso, support
from .analysis import analyze
from .auth import Authenticator, has_role, is_public, required_role
from .classify import Classifier
from .config import Settings
from .insights_ai import InsightSuggester
from .limits import RateLimiter
from .llm import LLMClient, UsageTracker
from .observability import Metrics
from .prompts import Prompts
from .report import load_template
from .resilience import SingleFlight, TTLCache
from .rules import load_rules
from .store import Store

log = logging.getLogger("insight_vault")

DEFAULT_DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "db.json")
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
LLM_PATHS = ("/api/insights/suggest", "/api/report", "/api/reports", "/api/llm/test")  # endpoints that may call the LLM


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


@dataclass
class Services:
    settings: Settings
    llm: LLMClient
    cache: TTLCache
    flight: SingleFlight
    classifier: Classifier
    suggester: InsightSuggester
    rules: object
    prompts: Prompts
    template: str
    usage: UsageTracker
    metrics: Metrics


def build_services(settings, store, llm=None):
    rules = load_rules(settings.rules_file)
    prompts = Prompts(settings.prompt_dir, settings.output_language)
    usage = UsageTracker(store, settings.daily_token_cap)
    llm = llm or LLMClient(settings, usage=usage)
    cache, flight = TTLCache(settings.cache_size, settings.cache_ttl), SingleFlight()
    return Services(
        settings, llm, cache, flight,
        Classifier(llm, cache, flight, settings, rules, prompts),
        InsightSuggester(llm, cache, flight, prompts),
        rules, prompts, load_template(settings.report_template), usage, Metrics(),
    )


def create_app(data_file=None, settings=None, llm=None, clock=_utcnow, oauth=None):
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
    store = Store(data_file or settings.data_file or DEFAULT_DATA_FILE)
    services = build_services(settings, store, llm)
    authenticator = Authenticator(settings)
    limiter, llm_limiter = RateLimiter(settings.rate_limit), RateLimiter(settings.rate_limit_llm)
    allowed_origins = {settings.public_url, *settings.cors_origins}

    def audit(actor, action, detail=""):
        with store.lock:
            db = store.load()
            db["audit"].append({"at": _now(), "actor": actor, "action": action, "detail": detail})
            del db["audit"][: -settings.audit_retention]
            store.save(db)

    @app.before_request
    def guard():
        g.request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        g.started = time.monotonic()
        g.user = None
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
        if is_public(path):
            return None
        user = authenticator.authenticate(request.headers, session)
        if user is None:
            return _error("authentication required", 401)
        origin = request.headers.get("Origin")
        if settings.auth_mode == "sso" and user["provider"] != "apikey" and request.method in UNSAFE_METHODS and origin and origin not in allowed_origins:
            return _error("cross-site request blocked", 403)
        needed = required_role(request.method, path)
        if not has_role(user, needed):
            return _error(f"this action requires the {needed} role", 403)
        g.user = user
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
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
            response.headers["Vary"] = "Origin"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            services.metrics.inc("http_requests_total", method=request.method, status=f"{response.status_code // 100}xx")
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
            store.save(store.load())  # proves the data store is readable and writable
        except OSError as exc:
            return _error(f"data store unavailable: {exc}", 503)
        return jsonify({"status": "ready"})

    @app.get("/api/config/public")
    def public_config():
        return jsonify({
            "appName": settings.app_name, "accentColor": settings.accent_color, "defaultTheme": settings.default_theme,
            "features": list(settings.features), "windows": list(settings.windows), "defaultDays": settings.default_days,
            "locale": settings.locale, "authMode": settings.auth_mode,
        })

    @app.get("/api/config")
    def admin_config():
        return jsonify(settings.masked())

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
        return jsonify(store.load()["audit"][-(int(limit) if limit.isdigit() and int(limit) > 0 else 100):][::-1])

    @app.get("/api/llm/status")
    def llm_status():
        return jsonify({
            "enabled": services.llm.enabled, "provider": settings.llm_provider, "model": settings.llm_model or None,
            "fallbacks": list(settings.llm_fallbacks), "circuits": services.llm.circuits(),
            "piiMasking": settings.pii_masking, "usage": services.usage.snapshot(),
        })

    @app.post("/api/llm/test")
    def llm_test():
        return jsonify(services.llm.ping())

    sso.register_routes(app, settings, authenticator, oauth or (sso.build_oauth(app, settings) if settings.auth_mode == "sso" else None), audit)

    def body():
        return request.get_json(silent=True) or {}

    @app.get("/api/state")
    def state():
        db = store.load()
        notes = [{k: v for k, v in n.items() if k != "text"} | {"length": len(n["text"])} for n in db["notes"]]
        return jsonify({"notes": notes, "highlights": db["highlights"], "insights": db["insights"]})

    @app.post("/api/notes")
    def add_note():
        data = body()
        if not data.get("title") or not data.get("text"):
            return _error("title and text are required", 400)
        with store.lock:
            db = store.load()
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
        note = find(store.load()["notes"], note_id)
        return jsonify(note) if note else _error("note not found", 404)

    @app.get("/api/notes/<note_id>/analysis")
    def note_analysis(note_id):
        note = find(store.load()["notes"], note_id)
        return jsonify(analyze(note["text"])) if note else _error("note not found", 404)

    @app.delete("/api/notes/<note_id>")
    def delete_note(note_id):
        with store.lock:
            db = store.load()
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
            db = store.load()
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
            db = store.load()
            highlight = find(db["highlights"], highlight_id)
            if not highlight:
                return _error("highlight not found", 404)
            highlight["tags"] = _clean_tags(body().get("tags"))
            store.save(db)
        return jsonify(highlight)

    @app.delete("/api/highlights/<highlight_id>")
    def delete_highlight(highlight_id):
        with store.lock:
            db = store.load()
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
            db = store.load()
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
        db = store.load()
        wanted = body().get("highlightIds")
        highlights = [h for h in db["highlights"] if wanted is None or h["id"] in wanted]
        return jsonify(services.suggester.suggest(highlights))

    @app.delete("/api/insights/<insight_id>")
    def delete_insight(insight_id):
        with store.lock:
            db = store.load()
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
        db = store.load()
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
        return Response(export_markdown(store.load()), mimetype="text/markdown")

    @app.errorhandler(Exception)
    def handle_error(exc):
        if isinstance(exc, HTTPException):
            return _error(exc.name.lower(), exc.code)
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
    log.info("Insight Vault API on http://%s:%d (auth=%s, llm=%s)", settings.host, settings.port, settings.auth_mode,
             f"{settings.llm_provider}:{settings.llm_model}" if settings.llm_enabled else "off")
    serve(create_app(settings=settings), host=settings.host, port=settings.port, threads=settings.threads)

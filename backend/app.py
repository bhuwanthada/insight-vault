"""Insight Vault JSON API (Flask)."""
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException

from . import support
from .analysis import analyze
from .classify import Classifier
from .config import Settings
from .insights_ai import InsightSuggester
from .llm import LLMClient
from .resilience import SingleFlight, TTLCache
from .store import Store

log = logging.getLogger("insight_vault")

DEFAULT_DATA_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "db.json")


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


def build_services(settings, llm=None):
    llm = llm or LLMClient(settings)
    cache, flight = TTLCache(settings.cache_size, settings.cache_ttl), SingleFlight()
    return Services(settings, llm, cache, flight, Classifier(llm, cache, flight, settings), InsightSuggester(llm, cache, flight))


def create_app(data_file=None, settings=None, llm=None, clock=_utcnow):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    store = Store(data_file or os.environ.get("DATA_FILE") or DEFAULT_DATA_FILE)
    services = build_services(settings or Settings.from_env(), llm)

    @app.before_request
    def tag_request():
        g.request_id = request.headers.get("X-Request-ID") or secrets.token_hex(8)

    @app.after_request
    def add_headers(response):
        response.headers["X-Request-ID"] = g.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/llm/status")
    def llm_status():
        return jsonify({"enabled": services.llm.enabled, "model": services.settings.model or None, "circuitOpen": not services.llm.breaker.allow()})

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
    from waitress import serve  # production WSGI server (the Flask dev server is not meant for production)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("PORT", "5001"))
    log.info("Insight Vault API listening on http://127.0.0.1:%d", port)
    serve(create_app(), host="127.0.0.1", port=port, threads=8)

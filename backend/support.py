"""Support tickets, the "last N days" resolution report, and a mock approval workflow."""
import hashlib
import json
import os
from datetime import timedelta

from flask import g, jsonify, request

from .llm import LLMUnavailable
from .classify import extract_fixes
from .report import build_report, parse_time, render_markdown

SAMPLE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples", "support_tickets.json")


def normalise_ticket(raw, uid):
    """Validate and normalise one ticket. Raises ValueError with a user-facing message."""
    if not isinstance(raw, dict):
        raise ValueError("each ticket must be an object")
    subject = str(raw.get("subject", "")).strip()
    if not subject:
        raise ValueError("ticket subject is required")
    try:
        created, resolved = parse_time(str(raw.get("createdAt"))), parse_time(str(raw.get("resolvedAt")))
    except ValueError:
        raise ValueError("createdAt and resolvedAt must be ISO-8601 timestamps") from None
    if resolved < created:
        raise ValueError("resolvedAt must not be before createdAt")
    tools = raw.get("tools_used", [])
    return {
        "id": str(raw.get("id") or "T-" + uid()),
        "subject": subject,
        "description": str(raw.get("description", "")),
        "resolution": str(raw.get("resolution", "")),
        "agent": str(raw.get("agent") or "unknown"),
        "tools_used": [str(t) for t in tools] if isinstance(tools, list) else [],
        "status": str(raw.get("status") or "resolved"),
        "createdAt": created.isoformat(),
        "resolvedAt": resolved.isoformat(),
    }


def register(app, store, services, clock, uid, now_iso):
    def error(message, status):
        return jsonify({"error": message}), status

    def payload():
        return request.get_json(silent=True)

    windows = services.settings.windows

    def window_error():
        return error(f"days must be one of {list(windows)}", 400)

    def window_days():
        days = request.args.get("days", str(services.settings.default_days))
        return int(days) if days.isdigit() and int(days) in windows else None

    def actor():
        return g.user["email"]

    def upsert(tickets):
        """Insert-or-replace by id, so re-posting a ticket never duplicates it."""
        with store.lock:
            db = store.load()
            by_id = {t["id"]: t for t in db["tickets"]}
            by_id.update({t["id"]: t for t in tickets})
            db["tickets"] = list(by_id.values())
            store.save(db)

    def report_for(days):
        tickets = store.load()["tickets"]
        return build_report(tickets, days, clock(), services.classifier.classify,
                            lambda ticket: extract_fixes(ticket, services.rules))

    def narrative(report):
        if not services.llm.enabled:
            return report["summary"], "rules"
        key = hashlib.sha256(
            f"narrative-v2|{services.llm.fingerprint('summary')}|{json.dumps(report, sort_keys=True)}".encode()
        ).hexdigest()

        def produce():
            cached = services.cache.get(key)
            if cached is not None:
                return cached
            try:
                text = str(services.llm.complete_json("summary", services.prompts.get("summary"), json.dumps(report)).get("summary", "")).strip()
            except LLMUnavailable:
                return None
            if text:
                services.cache.set(key, text)
            return text or None

        text = services.flight.do(key, produce)
        return (text, "llm") if text else (report["summary"], "rules")

    @app.get("/api/tickets")
    def list_tickets():
        return jsonify(store.load()["tickets"])

    @app.post("/api/tickets")
    def add_tickets():
        data = payload()
        items = data if isinstance(data, list) else [data]
        try:
            tickets = [normalise_ticket(item, uid) for item in items]
        except ValueError as exc:
            return error(str(exc), 400)
        upsert(tickets)
        return jsonify({"saved": len(tickets)}), 201

    @app.post("/api/tickets/sample")
    def load_sample():
        with open(SAMPLE_FILE, encoding="utf-8") as handle:
            samples = json.load(handle)
        now = clock()
        tickets = []
        for s in samples:
            resolved = now - timedelta(days=s["daysAgo"])
            tickets.append(
                normalise_ticket(
                    {
                        **{k: v for k, v in s.items() if k not in ("daysAgo", "hoursToResolve")},
                        "createdAt": (resolved - timedelta(hours=s["hoursToResolve"])).isoformat(),
                        "resolvedAt": resolved.isoformat(),
                    },
                    uid,
                )
            )
        upsert(tickets)
        return jsonify({"loaded": len(tickets)}), 201

    @app.get("/api/report")
    def get_report():
        days = window_days()
        if days is None:
            return window_error()
        return jsonify(report_for(days))

    @app.get("/api/reports")
    def list_reports():
        return jsonify(store.load()["reports"])

    @app.post("/api/reports")
    def create_report():
        data = payload() or {}
        days = data.get("days", services.settings.default_days)
        if days not in windows:
            return window_error()
        report = report_for(days)
        summary, method = narrative(report)
        title = str(data.get("title") or f"Support resolution report: last {days} days")[:200]
        doc = {
            "id": uid(),
            "title": title,
            "days": days,
            "status": "draft",
            "summaryMethod": method,
            "markdown": render_markdown(title, report, summary, services.template),
            "createdAt": now_iso(),
            "createdBy": actor(),
            "events": [{"at": now_iso(), "type": "created", "actor": actor(), "detail": f"Draft generated ({method} summary)"}],
        }
        with store.lock:
            db = store.load()
            db["reports"].append(doc)
            store.save(db)
        return jsonify(doc), 201

    def transition(report_id, allowed_from, build):
        """Atomic compare-and-set on the report status, so two concurrent actions cannot both win."""
        with store.lock:
            db = store.load()
            doc = next((r for r in db["reports"] if r["id"] == report_id), None)
            if doc is None:
                return error("report not found", 404)
            if doc["status"] not in allowed_from:
                return error(f"report is '{doc['status']}'; allowed from: {', '.join(allowed_from)}", 409)
            refusal = build(doc)
            if refusal:
                return refusal
            store.save(db)
        return jsonify(doc)

    @app.post("/api/reports/<report_id>/submit")
    def submit_report(report_id):
        approver = str((payload() or {}).get("approver") or "approver@example.com").strip()

        def build(doc):
            doc["status"] = "pending_approval"
            doc["events"].append(
                {"at": now_iso(), "type": "approval_requested", "actor": actor(),
                 "detail": f"MOCK: approval request queued for {approver}; nothing was actually sent"}
            )

        return transition(report_id, ("draft", "rejected"), build)

    @app.post("/api/reports/<report_id>/decision")
    def decide_report(report_id):
        data = payload() or {}
        decision = data.get("decision")
        signed_in = services.settings.auth_mode != "none"
        reviewer = str(data.get("reviewer") or (g.user["name"] if signed_in else "")).strip()
        if decision not in ("approve", "reject"):
            return error("decision must be 'approve' or 'reject'", 400)
        if not reviewer:
            return error("reviewer is required", 400)
        comment = str(data.get("comment") or "")

        def build(doc):
            if signed_in and not services.settings.allow_self_approval and doc.get("createdBy") == actor():
                return error("you cannot approve or reject a report you created", 403)
            doc["status"] = "approved" if decision == "approve" else "rejected"
            doc["events"].append(
                {"at": now_iso(), "type": doc["status"], "actor": actor(), "detail": f"{reviewer}: {comment}" if comment else reviewer}
            )

        return transition(report_id, ("pending_approval",), build)

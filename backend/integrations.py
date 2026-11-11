"""Endpoints for connecting ticketing tools and importing tickets from CSV."""
from dataclasses import dataclass
from datetime import timedelta

from flask import g, jsonify, request

from .connectors import KINDS, ConnectorError, HttpClient
from .connectors.csvimport import parse_csv
from .connectors.repo import public
from .connectors.sync import LEASE_SECONDS
from .secrets_box import SecretsUnavailable
from .tenancy import ensure_room

MAPPING_KEYS = ("resolution_field", "tools_field", "tools_tag_prefix")
MIN_INTERVAL, MAX_INTERVAL, DEFAULT_INTERVAL = 5, 1440, 60


@dataclass
class IntegrationsEnv:
    settings: object
    repo: object  # None when no database is configured
    box: object
    clock: object
    service: object  # None when no database is configured


def _error(message, status):
    return jsonify({"error": message}), status


def _clean_mapping(raw):
    if not isinstance(raw, dict) or any(k not in MAPPING_KEYS for k in raw) or any(not isinstance(v, str) or len(v) > 100 for v in raw.values()):
        raise ValueError(f"mapping may only contain these text values: {', '.join(MAPPING_KEYS)}")
    return {k: v.strip() for k, v in raw.items() if v.strip()}


def _clean_fields(raw, declared):
    names = {f["name"] for f in declared}
    return {k: str(v).strip() for k, v in (raw if isinstance(raw, dict) else {}).items() if k in names and str(v).strip()}


def _interval(value):
    if value is None:
        return DEFAULT_INTERVAL
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ValueError(f"intervalMinutes must be a whole number from {MIN_INTERVAL} to {MAX_INTERVAL}")
    return value


def register(app, env):
    def body():
        return request.get_json(silent=True) or {}

    def need_db():
        return None if env.repo else _error("Integrations need a database: set DATABASE_URL (and SECRETS_KEY to store credentials)", 400)

    def instantiate(kind, config, secrets, mapping):
        return KINDS[kind](config, secrets, mapping, HttpClient(), env.clock(), env.settings.sync_initial_days, env.settings.connector_extra_hosts)

    def find(connector_id):
        return env.repo.get(g.ctx.org_id, connector_id)

    @app.get("/api/connectors")
    def list_connectors():
        refusal = need_db()
        if refusal:
            return refusal
        return jsonify({"kinds": [k.describe() for k in KINDS.values()],
                        "connectors": [public(row) for row in env.repo.list(g.ctx.org_id)]})

    @app.post("/api/connectors")
    def add_connector():
        refusal = need_db()
        if refusal:
            return refusal
        data = body()
        cls = KINDS.get(data.get("kind"))
        if cls is None:
            return _error(f"kind must be one of: {', '.join(KINDS)}", 400)
        described = cls.describe()
        config, secrets = _clean_fields(data.get("config"), described["config"]), _clean_fields(data.get("secrets"), described["secrets"])
        missing = cls.missing(config, secrets)
        if missing:
            return _error(f"missing: {', '.join(missing)}", 400)
        try:
            mapping, interval = _clean_mapping(data.get("mapping", {})), _interval(data.get("intervalMinutes"))
            instantiate(cls.KIND, config, secrets, mapping)  # validates hosts and settings before anything is stored
            encrypted = env.box.encrypt(secrets)
        except (ValueError, ConnectorError) as exc:
            return _error(str(exc), 400)
        except SecretsUnavailable as exc:
            return _error(str(exc), 503)
        ensure_room(g.ctx.quotas, "connectors", env.repo.count(g.ctx.org_id))
        name = str(data.get("name") or described["label"]).strip()[:100]
        row = env.repo.create(g.ctx.org_id, cls.KIND, name, config, encrypted, mapping, interval, data.get("enabled", True) is not False,
                              env.clock().isoformat())
        return jsonify(public(row)), 201

    @app.put("/api/connectors/<connector_id>")
    def update_connector(connector_id):
        refusal = need_db()
        if refusal:
            return refusal
        row = find(connector_id)
        if row is None:
            return _error("connector not found", 404)
        data, cls = body(), KINDS[row["kind"]]
        described = cls.describe()
        try:
            config = {**row["config"], **_clean_fields(data.get("config"), described["config"])}
            mapping = _clean_mapping(data["mapping"]) if "mapping" in data else row["mapping"]
            secrets = {**env.box.decrypt(row["secrets_enc"]), **_clean_fields(data.get("secrets"), described["secrets"])}
            interval = _interval(data["intervalMinutes"]) if "intervalMinutes" in data else row["interval_minutes"]
            instantiate(cls.KIND, config, secrets, mapping)
            fields = {"config": config, "mapping": mapping, "secrets_enc": env.box.encrypt(secrets), "interval_minutes": interval}
        except (ValueError, ConnectorError) as exc:
            return _error(str(exc), 400)
        except SecretsUnavailable as exc:
            return _error(str(exc), 503)
        if "name" in data:
            fields["name"] = str(data["name"]).strip()[:100] or row["name"]
        if "enabled" in data:
            fields["enabled"] = data["enabled"] is True
        if any(k in data for k in ("config", "secrets", "mapping")):
            fields["cursor"] = None  # pointing at a different place: start over rather than resume the old position
        env.repo.update(g.ctx.org_id, connector_id, **fields)
        return jsonify(public(find(connector_id)))

    @app.delete("/api/connectors/<connector_id>")
    def delete_connector(connector_id):
        refusal = need_db()
        if refusal:
            return refusal
        return jsonify({"ok": True}) if env.repo.delete(g.ctx.org_id, connector_id) else _error("connector not found", 404)

    @app.post("/api/connectors/<connector_id>/test")
    def test_connector(connector_id):
        refusal = need_db()
        if refusal:
            return refusal
        row = find(connector_id)
        if row is None:
            return _error("connector not found", 404)
        try:
            return jsonify({"ok": True, "detail": env.service.build(row).test()})
        except (ConnectorError, SecretsUnavailable) as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.post("/api/connectors/<connector_id>/sync")
    def sync_connector(connector_id):
        refusal = need_db()
        if refusal:
            return refusal
        if find(connector_id) is None:
            return _error("connector not found", 404)
        if not env.repo.lease(connector_id, env.clock(), LEASE_SECONDS):
            return _error("a sync of this connector is already running", 409)
        return jsonify(env.service.run(find(connector_id)))

    @app.post("/api/tickets/import")
    def import_csv():
        data = body()
        text = data.get("csv")
        if not isinstance(text, str) or not text.strip():
            return _error("csv (the file's text) is required", 400)
        try:
            tickets, skipped = parse_csv(text, data.get("mapping"), data.get("dateFormat"))
        except ConnectorError as exc:
            return _error(str(exc), 400)
        with g.ctx.store.lock:
            ids = [t["id"] for t in tickets]
            ensure_room(g.ctx.quotas, "tickets", g.ctx.store.ticket_count(), len(set(ids) - g.ctx.store.existing_ticket_ids(ids)))
            g.ctx.store.upsert_tickets(tickets)
        return jsonify({"imported": len(tickets), "skipped": len(skipped), "skippedRows": skipped[:20]}), 201

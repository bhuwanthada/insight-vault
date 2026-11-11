"""Database schema (SQLAlchemy Core) and a small versioned migration runner.

Every tenant-owned table carries ``org_id`` and every query in the stores filters by it. Add a new
migration by appending a function to MIGRATIONS; never edit a shipped one.
"""
import sqlalchemy as sa

metadata = sa.MetaData()

orgs = sa.Table(
    "orgs", metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("plan", sa.String(32), nullable=False, default="starter"),
    sa.Column("status", sa.String(16), nullable=False, default="active"),  # active | suspended
    sa.Column("claims", sa.Text, nullable=False, default="{}"),  # the sections of the latest valid org key
    sa.Column("settings", sa.Text, nullable=False, default="{}"),  # org-editable settings (branding, LLM choice, ...)
    sa.Column("secrets_enc", sa.Text, nullable=False, default=""),  # encrypted secrets (LLM key, ...)
    sa.Column("key_exp", sa.Text, nullable=True),  # ISO expiry of the latest key
    sa.Column("created_at", sa.Text, nullable=False),
)

memberships = sa.Table(
    "memberships", metadata,
    sa.Column("org_id", sa.String(64), primary_key=True),
    sa.Column("email", sa.String(320), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Index("ix_memberships_email", "email"),
)

revoked_keys = sa.Table(
    "revoked_keys", metadata,
    sa.Column("jti", sa.String(64), primary_key=True),
    sa.Column("reason", sa.Text, nullable=False, default=""),
    sa.Column("revoked_at", sa.Text, nullable=False),
)

docs = sa.Table(
    "docs", metadata,
    sa.Column("org_id", sa.String(64), primary_key=True),
    sa.Column("coll", sa.String(32), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("seq", sa.Integer, nullable=False),
    sa.Column("data", sa.Text, nullable=False),
    sa.Index("ix_docs_order", "org_id", "coll", "seq"),
)

tickets = sa.Table(
    "tickets", metadata,
    sa.Column("org_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(128), primary_key=True),
    sa.Column("resolved_at", sa.String(32), nullable=False),  # normalised UTC, sortable as text
    sa.Column("data", sa.Text, nullable=False),
    sa.Index("ix_tickets_resolved", "org_id", "resolved_at"),
)

audit = sa.Table(
    "audit", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("org_id", sa.String(64), nullable=False),
    sa.Column("at", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("detail", sa.Text, nullable=False),
    sa.Index("ix_audit_org", "org_id", "id"),
)

kv = sa.Table(
    "kv", metadata,
    sa.Column("org_id", sa.String(64), primary_key=True),
    sa.Column("k", sa.String(64), primary_key=True),
    sa.Column("data", sa.Text, nullable=False),
)

connectors = sa.Table(
    "connectors", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("org_id", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("config", sa.Text, nullable=False, default="{}"),
    sa.Column("secrets_enc", sa.Text, nullable=False, default=""),
    sa.Column("mapping", sa.Text, nullable=False, default="{}"),
    sa.Column("interval_minutes", sa.Integer, nullable=False, default=60),
    sa.Column("enabled", sa.Boolean, nullable=False, default=True),
    sa.Column("cursor", sa.Text, nullable=True),
    sa.Column("last_sync_at", sa.Text, nullable=True),
    sa.Column("last_status", sa.String(16), nullable=True),
    sa.Column("last_error", sa.Text, nullable=True),
    sa.Column("last_count", sa.Integer, nullable=False, default=0),
    sa.Column("next_run_at", sa.Text, nullable=True),
    sa.Column("lease_until", sa.Text, nullable=True),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Index("ix_connectors_org", "org_id"),
    sa.Index("ix_connectors_due", "next_run_at"),
)

migrations_table = sa.Table(
    "schema_migrations", metadata,
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("applied_at", sa.Text, nullable=False),
)


def take_advisory_lock(connection, dialect_name, key):
    """Serialise work across processes. PostgreSQL only; SQLite relies on the in-process lock."""
    if dialect_name == "postgresql":
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _baseline(connection):
    metadata.create_all(connection)


# Ordered, append-only. Each entry is (version, description, function(connection)).
MIGRATIONS = [(1, "baseline schema", _baseline)]


def migrate(engine, now_iso):
    """Apply every migration that has not run yet. Safe to call on every start. Returns applied versions."""
    applied = []
    with engine.begin() as conn:
        take_advisory_lock(conn, engine.dialect.name, "insight-vault-migrations")  # several workers may start at once
        migrations_table.create(conn, checkfirst=True)
        done = {row[0] for row in conn.execute(sa.select(migrations_table.c.version))}
        for version, _description, step in MIGRATIONS:
            if version not in done:
                step(conn)
                conn.execute(migrations_table.insert().values(version=version, applied_at=now_iso))
                applied.append(version)
    return applied

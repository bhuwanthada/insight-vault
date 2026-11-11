"""The organization directory: organizations, their members and revoked keys (multi-tenant mode)."""
import json

import sqlalchemy as sa

from . import schema


def _org_row(row):
    return {
        "id": row.id, "name": row.name, "plan": row.plan, "status": row.status, "claims": json.loads(row.claims),
        "settings": json.loads(row.settings), "secrets_enc": row.secrets_enc, "key_exp": row.key_exp,
        "created_at": row.created_at,
    }


class Directory:
    def __init__(self, engine):
        self.engine = engine

    # ---- organizations
    def get_org(self, org_id):
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(schema.orgs).where(schema.orgs.c.id == org_id)).first()
        return _org_row(row) if row else None

    def list_orgs(self):
        with self.engine.connect() as conn:
            return [_org_row(r) for r in conn.execute(sa.select(schema.orgs).order_by(schema.orgs.c.created_at, schema.orgs.c.id))]

    def activate(self, claims, now_iso):
        """Create the organization from a verified key, or refresh it from a newer key. Idempotent.

        Returns (org, created). The key's admins become administrators (existing members keep their role).
        """
        o, m = schema.orgs, schema.memberships
        values = {"name": claims["org_name"], "plan": claims["plan"], "claims": json.dumps(claims, sort_keys=True),
                  "key_exp": str(claims["exp"])}
        with self.engine.begin() as conn:
            exists = conn.execute(sa.select(o.c.id).where(o.c.id == claims["org_id"])).first()
            if exists:
                conn.execute(o.update().where(o.c.id == claims["org_id"]).values(**values))
            else:
                conn.execute(o.insert().values(id=claims["org_id"], status="active", settings="{}", secrets_enc="",
                                               created_at=now_iso, **values))
            for email in claims["admins"]:
                known = conn.execute(sa.select(m.c.email).where(m.c.org_id == claims["org_id"], m.c.email == email)).first()
                if not known:
                    conn.execute(m.insert().values(org_id=claims["org_id"], email=email, role="admin", created_at=now_iso))
        return self.get_org(claims["org_id"]), not exists

    def update_org(self, org_id, **fields):
        """Update settings (dict), secrets_enc (text) or status (text) of an organization."""
        values = {}
        if "settings" in fields:
            values["settings"] = json.dumps(fields["settings"], sort_keys=True)
        for name in ("secrets_enc", "status"):
            if name in fields:
                values[name] = fields[name]
        with self.engine.begin() as conn:
            conn.execute(schema.orgs.update().where(schema.orgs.c.id == org_id).values(**values))

    def delete_org(self, org_id):
        """Remove the organization and every row it owns, in every table."""
        with self.engine.begin() as conn:
            for table in (schema.docs, schema.tickets, schema.audit, schema.kv, schema.connectors, schema.memberships):
                conn.execute(table.delete().where(table.c.org_id == org_id))
            conn.execute(schema.orgs.delete().where(schema.orgs.c.id == org_id))

    # ---- members
    def memberships_for_email(self, email):
        """Active organizations this e-mail belongs to: [{'org': id, 'role': role}], oldest first."""
        m, o = schema.memberships, schema.orgs
        query = (sa.select(m.c.org_id, m.c.role).join(o, o.c.id == m.c.org_id)
                 .where(m.c.email == email, o.c.status == "active").order_by(m.c.created_at, m.c.org_id))
        with self.engine.connect() as conn:
            return [{"org": r[0], "role": r[1]} for r in conn.execute(query)]

    def list_members(self, org_id):
        m = schema.memberships
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(m.c.email, m.c.role).where(m.c.org_id == org_id).order_by(m.c.email))
            return [{"email": r[0], "role": r[1]} for r in rows]

    def set_member(self, org_id, email, role, now_iso):
        m = schema.memberships
        with self.engine.begin() as conn:
            changed = conn.execute(m.update().where(m.c.org_id == org_id, m.c.email == email).values(role=role)).rowcount
            if not changed:
                conn.execute(m.insert().values(org_id=org_id, email=email, role=role, created_at=now_iso))

    def remove_member(self, org_id, email):
        m = schema.memberships
        with self.engine.begin() as conn:
            return conn.execute(m.delete().where(m.c.org_id == org_id, m.c.email == email)).rowcount > 0

    def count_members(self, org_id):
        m = schema.memberships
        with self.engine.connect() as conn:
            return conn.execute(sa.select(sa.func.count()).select_from(m).where(m.c.org_id == org_id)).scalar()

    # ---- revoked keys
    def revoke_key(self, jti, reason, now_iso):
        r = schema.revoked_keys
        with self.engine.begin() as conn:
            if not conn.execute(sa.select(r.c.jti).where(r.c.jti == jti)).first():
                conn.execute(r.insert().values(jti=jti, reason=reason, revoked_at=now_iso))

    def revoked_ids(self):
        with self.engine.connect() as conn:
            return {row[0] for row in conn.execute(sa.select(schema.revoked_keys.c.jti))}

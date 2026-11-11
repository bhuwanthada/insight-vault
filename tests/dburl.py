"""Database URL for a test: a private SQLite file normally, or a real PostgreSQL when TEST_DATABASE_URL is set.

With PostgreSQL the schema is wiped the first time a test asks for a URL, so every test starts empty even
though they all share one database. (Several apps within one test share the URL on purpose.)"""
import os

import sqlalchemy as sa

POSTGRES_URL = os.environ.get("TEST_DATABASE_URL", "")
_seen = set()


def _wipe(url):
    from backend.db import normalise_url
    from backend.schema import metadata, migrations_table

    engine = sa.create_engine(normalise_url(url))
    with engine.begin() as conn:
        metadata.drop_all(conn)
        migrations_table.drop(conn, checkfirst=True)
    engine.dispose()


def db_url(tmp_path, name="test"):
    if not POSTGRES_URL:
        return f"sqlite:///{tmp_path / (name + '.db')}"
    if str(tmp_path) not in _seen:  # first request in this test: start from an empty database
        _seen.add(str(tmp_path))
        _wipe(POSTGRES_URL)
    return POSTGRES_URL

import io
import json
import runpy
import sys
import urllib.error

import pytest

from backend import admin, orgkeys
from backend.config import Settings
from backend.llm import HTTPProvider, RateLimited, Target


def run(*argv):
    out = io.StringIO()
    code = admin.main(list(argv), out=out)
    return code, out.getvalue()


@pytest.fixture
def keys(tmp_path):
    assert run("keygen", "--out", str(tmp_path))[0] == 0
    return tmp_path / "org-key-private.pem", tmp_path / "org-key-public.pem"


def test_keygen_writes_both_halves_and_explains_which_to_keep(tmp_path):
    code, text = run("keygen", "--out", str(tmp_path / "keys"))
    assert code == 0 and "KEEP OFFLINE" in text and "ORG_KEY_PUBLIC_KEY_FILE" in text
    assert (tmp_path / "keys" / "org-key-private.pem").read_text().startswith("-----BEGIN PRIVATE KEY")
    assert (tmp_path / "keys" / "org-key-public.pem").read_text().startswith("-----BEGIN PUBLIC KEY")


def test_mint_produces_a_key_the_app_accepts(keys):
    private, public = keys
    code, token = run("mint", "--private-key", str(private), "--org", "acme", "--name", "Acme Inc", "--plan", "growth", "--role", "admin",
                      "--features", "notes, support", "--quota", "tickets=500", "--quota", "seats=3", "--domain", "acme.com",
                      "--admin", "boss@acme.com", "--days", "30")
    claims = orgkeys.verify(token.strip(), public.read_text())
    assert code == 0 and claims["org_id"] == "acme" and claims["plan"] == "growth" and claims["features"] == ["notes", "support"]
    assert claims["quotas"] == {"tickets": 500, "seats": 3} and claims["admins"] == ["boss@acme.com"] and claims["role"] == "admin"


def test_mint_reports_invalid_sections_without_a_traceback(keys, capsys):
    code, _ = run("mint", "--private-key", str(keys[0]), "--org", "Bad Org!")
    assert code == 1 and "organization id" in capsys.readouterr().err
    assert run("mint", "--private-key", str(keys[0]) + ".missing", "--org", "acme")[0] == 1


def test_a_malformed_quota_is_refused_by_the_parser(keys, capsys):
    with pytest.raises(SystemExit):
        run("mint", "--private-key", str(keys[0]), "--org", "acme", "--quota", "tickets")
    assert "name=number" in capsys.readouterr().err


def test_inspect_can_verify_or_just_read(keys):
    private, public = keys
    token = run("mint", "--private-key", str(private), "--org", "acme")[1].strip()
    _, unverified = run("inspect", token)
    assert '"sub": "acme"' in unverified and "WARNING: not verified" in unverified
    code, verified = run("inspect", token, "--public-key", str(public))
    assert code == 0 and '"org_id": "acme"' in verified and "valid" in verified
    other = run("keygen", "--out", str(private.parent / "other"))[0]
    assert other == 0 and run("inspect", token, "--public-key", str(private.parent / "other" / "org-key-public.pem"))[0] == 1


def test_secrets_key_prints_a_usable_key():
    from backend.secrets_box import valid_keys

    code, text = run("secrets-key")
    assert code == 0 and valid_keys(text.strip())


def test_migrate_needs_a_database_and_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert run("migrate")[0] == 1 and "DATABASE_URL is not set" in capsys.readouterr().err
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    assert "applied migrations: [1]" in run("migrate")[1]
    assert "already up to date" in run("migrate")[1]


def test_the_module_runs_as_a_program(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backend.admin", "secrets-key"])
    with pytest.raises(SystemExit) as info:
        runpy.run_module("backend.admin", run_name="__main__")
    assert info.value.code == 0 and len(capsys.readouterr().out.strip()) == 44


def test_other_http_errors_are_plain_failures_not_rate_limits():
    err = urllib.error.HTTPError("http://h", 500, "boom", None, None)
    provider = HTTPProvider(Settings(), "http://h", opener=lambda r, timeout: (_ for _ in ()).throw(err))
    with pytest.raises(RuntimeError, match="HTTP 500") as info:
        provider.complete(Target("groq", "m"), [], 0, 1, 1)
    assert not isinstance(info.value, RateLimited)

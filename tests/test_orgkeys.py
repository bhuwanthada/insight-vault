from datetime import datetime, timedelta, timezone

import pytest

from backend import orgkeys, secrets_box
from backend.config import Settings
from backend.orgkeys import KeyVerifier, OrgKeyError
from backend.secrets_box import SecretBox, SecretsUnavailable

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pair():
    return orgkeys.generate_keypair()


def mint(pair, **kw):
    params = dict(org_id="acme", org_name="Acme Inc", now=NOW)
    params.update(kw)
    return orgkeys.mint(pair[0], **params)


# ---------- minting ----------
def test_keys_round_trip_with_every_section(pair):
    token = mint(pair, plan="growth", role="approver", mode="license", features=("notes", "support", "notes"),
                 quotas={"tickets": 500, "seats": 5}, domains=["Acme.com", " ", "acme.io"], admins=["Boss@Acme.com"], days=30)
    claims = orgkeys.verify(token, pair[1], now=NOW)
    assert claims["org_id"] == "acme" and claims["org_name"] == "Acme Inc" and claims["plan"] == "growth"
    assert claims["role"] == "approver" and claims["mode"] == "license" and claims["features"] == ["notes", "support"]
    assert claims["quotas"] == {"tickets": 500, "seats": 5} and claims["domains"] == ["acme.com", "acme.io"]
    assert claims["admins"] == ["boss@acme.com"] and claims["exp"] == int((NOW + timedelta(days=30)).timestamp())
    assert len(claims["jti"]) == 32 and claims["iat"] == int(NOW.timestamp())


def test_defaults_are_sensible(pair):
    claims = orgkeys.verify(mint(pair, org_name=""), pair[1], now=NOW)
    assert claims["org_name"] == "acme" and claims["plan"] == "starter" and claims["role"] == "editor"
    assert claims["mode"] == "tenant" and "integrations" in claims["features"] and claims["quotas"] == {}


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({"org_id": "A"}, "organization id"),
        ({"org_id": "has space"}, "organization id"),
        ({"role": "owner"}, "role must be one of"),
        ({"mode": "trial"}, "mode must be one of"),
        ({"features": ("support",)}, "must include notes"),
        ({"features": ("notes", "teleport")}, "only use"),
        ({"features": ()}, "must include notes"),
        ({"quotas": {"widgets": 1}}, "unknown quota"),
        ({"quotas": {"seats": -1}}, "whole number"),
        ({"quotas": {"seats": True}}, "whole number"),
        ({"quotas": {"seats": "9"}}, "whole number"),
    ],
)
def test_minting_refuses_to_sign_invalid_sections(pair, kw, expected):
    with pytest.raises(OrgKeyError, match=expected):
        mint(pair, **kw)


def test_mint_uses_the_clock_when_none_is_given(pair):
    claims = orgkeys.verify(orgkeys.mint(pair[0], org_id="acme", days=1), pair[1])
    assert claims["exp"] > datetime.now(timezone.utc).timestamp()


# ---------- verifying ----------
def test_a_key_signed_by_someone_else_is_rejected(pair):
    other = orgkeys.generate_keypair()
    with pytest.raises(OrgKeyError, match="bad_signature"):
        orgkeys.verify(mint(other), pair[1], now=NOW)


@pytest.mark.parametrize("bad", ["", "not.a.token", "garbage", "a.b.c"])
def test_malformed_keys_are_rejected(pair, bad):
    with pytest.raises(OrgKeyError, match="invalid key"):
        orgkeys.verify(bad, pair[1], now=NOW)


def test_unusable_public_key_is_rejected(pair):
    with pytest.raises(OrgKeyError, match="invalid key"):
        orgkeys.verify(mint(pair), "not a pem", now=NOW)


def test_expired_keys_are_rejected_unless_expiry_is_allowed(pair):
    token = mint(pair, days=1)
    later = NOW + timedelta(days=2)
    with pytest.raises(OrgKeyError, match="expired"):
        orgkeys.verify(token, pair[1], now=later)
    assert orgkeys.verify(token, pair[1], now=later, allow_expired=True)["org_id"] == "acme"  # licenses start read-only


def test_revoked_keys_are_rejected(pair):
    token = mint(pair)
    jti = orgkeys.verify(token, pair[1], now=NOW)["jti"]
    with pytest.raises(OrgKeyError, match="revoked"):
        orgkeys.verify(token, pair[1], now=NOW, revoked={jti})


def test_wrong_issuer_and_unknown_algorithms_are_rejected(pair):
    from joserfc import jwt
    from joserfc.jwk import ECKey, OctKey

    claims = {"iss": "someone-else", "sub": "acme", "exp": int(NOW.timestamp()) + 60, "role": "editor", "mode": "tenant",
              "features": ["notes"], "org": {"name": "x"}}
    key = ECKey.import_key(pair[0])
    wrong_issuer = jwt.encode({"alg": "ES256"}, claims, key)
    with pytest.raises(OrgKeyError, match="wrong issuer"):
        orgkeys.verify(wrong_issuer, pair[1], now=NOW)
    hmac_token = jwt.encode({"alg": "HS256"}, {**claims, "iss": "insight-vault"}, OctKey.import_key("secret" * 6))
    with pytest.raises(OrgKeyError, match="invalid key"):  # only ES256 is accepted: no algorithm confusion
        orgkeys.verify(hmac_token, pair[1], now=NOW)


def test_a_key_without_an_expiry_is_rejected(pair):
    from joserfc import jwt
    from joserfc.jwk import ECKey

    claims = {"iss": "insight-vault", "sub": "acme", "role": "editor", "mode": "tenant", "features": ["notes"]}
    with pytest.raises(OrgKeyError, match="invalid key"):
        orgkeys.verify(jwt.encode({"alg": "ES256"}, claims, ECKey.import_key(pair[0])), pair[1], now=NOW)


# ---------- helpers ----------
def test_inspect_reads_without_verifying(pair):
    assert orgkeys.inspect(mint(pair))["sub"] == "acme"
    for bad in ("nope", "a.!!!.c"):
        with pytest.raises(OrgKeyError, match="unreadable"):
            orgkeys.inspect(bad)


def test_looks_like_key_tells_org_keys_from_plain_api_keys(pair):
    assert orgkeys.looks_like_key(mint(pair)) and not orgkeys.looks_like_key("plain-api-key-123") and not orgkeys.looks_like_key("")
    assert not orgkeys.looks_like_key(None)


def test_public_key_loads_from_text_or_file(pair, tmp_path):
    assert orgkeys.load_public_key(Settings(org_key_public_key="PEM-TEXT")) == "PEM-TEXT"
    path = tmp_path / "pub.pem"
    path.write_text(pair[1], encoding="utf-8")
    assert orgkeys.load_public_key(Settings(org_key_public_key_file=str(path))) == pair[1]
    assert orgkeys.load_public_key(Settings()) == ""


def test_key_verifier_caches_revocations_and_can_refresh(pair):
    now = {"t": NOW}
    revoked = set()
    calls = []

    def provider():
        calls.append(1)
        return set(revoked)

    verifier = KeyVerifier(pair[1], provider, clock=lambda: now["t"])
    token = mint(pair)
    jti = verifier.verify(token)["jti"]
    revoked.add(jti)
    assert verifier.verify(token)["jti"] == jti and len(calls) == 1  # served from the short cache
    verifier.refresh()
    with pytest.raises(OrgKeyError, match="revoked"):
        verifier.verify(token)
    now["t"] = NOW + timedelta(seconds=20)  # cache expiry also picks it up
    revoked.clear()
    assert verifier.verify(token)["jti"] == jti and len(calls) == 3
    assert verifier.verify(mint(pair, days=1)) and KeyVerifier(pair[1]).revoked() == set()


# ---------- encrypted secrets ----------
def test_secrets_round_trip_and_empty_values():
    box = SecretBox(secrets_box.generate_key())
    assert box.available and box.encrypt({}) == "" and box.decrypt("") == {}
    token = box.encrypt({"llm_api_key": "sk-123"})
    assert "sk-123" not in token and box.decrypt(token) == {"llm_api_key": "sk-123"}


def test_key_rotation_new_key_encrypts_old_key_still_decrypts():
    old, new = secrets_box.generate_key(), secrets_box.generate_key()
    token = SecretBox(old).encrypt({"a": 1})
    rotating = SecretBox(f"{new}, {old}")
    assert rotating.decrypt(token) == {"a": 1}
    assert SecretBox(new).decrypt(rotating.encrypt({"b": 2})) == {"b": 2}  # new data needs only the new key
    with pytest.raises(SecretsUnavailable, match="cannot be decrypted"):
        SecretBox(new).decrypt(token)


def test_secrets_unavailable_without_a_key():
    box = SecretBox("")
    assert not box.available and box.decrypt("") == {} and box.encrypt({}) == ""
    with pytest.raises(SecretsUnavailable, match="cannot be stored"):
        box.encrypt({"a": 1})
    with pytest.raises(SecretsUnavailable, match="cannot be read"):
        box.decrypt("something")


def test_secrets_key_validation():
    good = secrets_box.generate_key()
    assert secrets_box.valid_keys(good) and secrets_box.valid_keys(f"{good},{good}")
    assert not secrets_box.valid_keys("short") and not secrets_box.valid_keys(f"{good},nope")

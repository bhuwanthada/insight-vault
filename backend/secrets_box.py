"""Encryption for secrets stored in the database (per-organization LLM keys, connector credentials).

SECRETS_KEY may hold several comma-separated keys: the first encrypts, all of them decrypt. To rotate,
put the new key first, keep the old one until everything has been re-saved, then drop it.
"""
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretsUnavailable(RuntimeError):
    """Encryption is not configured, or the stored data cannot be decrypted with the configured keys."""


def generate_key():
    return Fernet.generate_key().decode()


def valid_keys(text):
    """True if every comma-separated entry is a well-formed key."""
    try:
        for part in text.split(","):
            Fernet(part.strip().encode())
    except ValueError:
        return False
    return True


class SecretBox:
    def __init__(self, keys):
        parts = [k.strip() for k in (keys or "").split(",") if k.strip()]
        self._fernet = MultiFernet([Fernet(k.encode()) for k in parts]) if parts else None

    @property
    def available(self):
        return self._fernet is not None

    def encrypt(self, data):
        """dict -> opaque text. An empty dict is stored as empty text."""
        if not data:
            return ""
        if self._fernet is None:
            raise SecretsUnavailable("SECRETS_KEY is not set, so secrets cannot be stored")
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, token):
        if not token:
            return {}
        if self._fernet is None:
            raise SecretsUnavailable("SECRETS_KEY is not set, so stored secrets cannot be read")
        try:
            return json.loads(self._fernet.decrypt(token.encode()))
        except InvalidToken as exc:
            raise SecretsUnavailable("stored secrets cannot be decrypted with the configured SECRETS_KEY") from exc

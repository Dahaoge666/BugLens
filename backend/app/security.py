"""Shared input redaction and encrypted SDK checkpoint helpers."""

import base64
import hashlib
import re

from cryptography.fernet import Fernet, InvalidToken
from pydantic import JsonValue

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+"),
    re.compile(r"(?i)((?:password|token|secret)\s*[:=]\s*)[^\s,;]+"),
)
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "secret_key",
    "token",
}


def sanitize_text(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(r"\1[REDACTED]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL REDACTED]", value)
    return re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[PHONE REDACTED]", value)


def sanitize_data(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if str(key).lower() in _SECRET_KEYS
            else sanitize_data(item)
            for key, item in value.items()
        }
    return value


class RunStateCipherError(ValueError):
    """Raised when an encrypted SDK approval checkpoint cannot be used."""

    code = "sdk_run_state_unavailable"


class SDKRunStateCipher:
    """Encrypt Agents SDK ``RunState`` snapshots before they enter SQLite.

    Operators may provide a normal Fernet key or any non-empty secret.  The
    latter is deterministically hashed into a Fernet key, which keeps the
    environment variable ergonomic while still requiring the same secret in a
    second process to resume an approval.  The key itself is never serialized.
    """

    _PREFIX = "fernet-v1:"

    def __init__(self, key: str | bytes) -> None:
        material = key.encode("utf-8") if isinstance(key, str) else bytes(key)
        if not material:
            raise RunStateCipherError("SDK approval state key is empty")
        try:
            encoded_key = material
            Fernet(encoded_key)
        except (ValueError, TypeError):
            encoded_key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
        self._fernet = Fernet(encoded_key)

    def encrypt(self, state: str) -> str:
        if not isinstance(state, str) or not state:
            raise RunStateCipherError("SDK approval state is empty")
        return self._PREFIX + self._fernet.encrypt(state.encode("utf-8")).decode(
            "ascii"
        )

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(self._PREFIX):
            raise RunStateCipherError("SDK approval state format is invalid")
        try:
            plaintext = self._fernet.decrypt(
                value.removeprefix(self._PREFIX).encode("ascii")
            )
        except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
            raise RunStateCipherError("SDK approval state cannot be decrypted") from exc
        return plaintext.decode("utf-8")

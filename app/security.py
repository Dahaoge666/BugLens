"""Shared input redaction at the domain boundary."""

import re

from pydantic import JsonValue

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+"),
    re.compile(r"(?i)((?:password|token|secret)\s*[:=]\s*)[^\s,;]+"),
)


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
        return {key: sanitize_data(item) for key, item in value.items()}
    return value

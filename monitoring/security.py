"""Redact before logs, ledger writes, and artifact writes."""
import re

SENSITIVE = {"authorization", "api_key", "apikey", "x-api-key", "openai_api_key",
             "anthropic_api_key", "password", "token", "access_token"}
PATTERNS = [
    (re.compile(r"(?im)(authorization[\"']?\s*[:=]\s*)[^\r\n]*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)bearer\s+[^\s,;\"'}]+"), "Bearer [REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]+"), "[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|authorization|password)[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+"),
     r"\1[REDACTED]"),
]


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if str(k).lower() in SENSITIVE else redact(v)
                for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
    return value

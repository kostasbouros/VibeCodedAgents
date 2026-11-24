"""Remove personal data from text before it is sent to Claude.

If redaction fails for any reason, RedactionError is raised and the caller must stop.
"""

import re


class RedactionError(Exception):
    """Raised when text cannot be safely redacted. Never send the text to Claude after this."""


# Order matters: longer, more specific patterns run first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("IBAN", re.compile(r"\bGR\d{2}(?:\s?\d{4}){5}\s?\d{3}\b", re.IGNORECASE)),
    ("IMEI", re.compile(r"\b\d{15}\b")),
    # Greek phones: optional +30 / 0030, then 10 digits starting with 2 (landline) or 69 (mobile).
    ("PHONE", re.compile(r"(?:\+30|0030)?[\s-]?\b(?:2\d|69)\d(?:[\s-]?\d){7}\b")),
    ("AFM", re.compile(r"\b\d{9}\b")),  # Greek tax number
]


def redact(text: str) -> str:
    """Return text with personal data replaced by [EMAIL], [PHONE], etc."""
    if not isinstance(text, str):
        raise RedactionError(f"expected text, got {type(text).__name__}")
    try:
        for label, pattern in _PATTERNS:
            text = pattern.sub(f"[{label}]", text)
    except Exception as exc:  # any failure means we must not continue
        raise RedactionError("redaction failed") from exc
    return text

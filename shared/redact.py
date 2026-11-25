"""Remove personal data from text before it reaches Claude.

Use redact_or_raise() in agent code: it raises RedactionError if redaction could not be
confirmed, and the caller must then stop. redact() returns a RedactionResult; when ok is
False its redacted_text is empty so it can never be sent by mistake.

Steps: clean invisible characters -> strip quoted replies -> tokenise names after greetings
-> strip signature (keep the sign-off, tokenise the name) -> replace those names everywhere
-> set dates aside -> tokenise patterns -> verify nothing still matches -> put dates back.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from shared.limits import limits


class RedactionError(Exception):
    """Redaction could not be confirmed. Never send the text to Claude after this."""


@dataclass(frozen=True)
class RedactionResult:
    redacted_text: str
    counts: dict[str, int]  # occurrences replaced, by type, e.g. {"PHONE": 2}
    ok: bool
    problems: tuple[str, ...] = field(default=())  # types still found by the check


# Greek plates use only letters that look the same in Greek and Latin.
_PLATE_GR = "ΑΒΕΖΗΙΚΜΝΟΡΤΥΧ"
_PLATE_LATIN = "ABEZHIKMNOPTYX"
_TO_LATIN = str.maketrans(_PLATE_GR, _PLATE_LATIN)

# Order matters: longer and more specific patterns first. Quantifiers are bounded so long
# inputs (logs, pasted attachments) stay fast.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")),
    # Disguised emails: maria[at]gmail.com, maria (at) gmail [dot] com, maria at gmail dot com
    (
        "EMAIL",
        re.compile(
            r"[\w.+-]{1,64}\s?(?:\[(?:at|@)\]|\((?:at|@)\)|\{at\})\s?[\w-]{1,63}"
            r"(?:\s?(?:\.|\[dot\]|\(dot\))\s?[\w-]{1,63}){1,4}"
            r"|[\w.+-]{1,64}\s+at\s+[\w-]{1,63}(?:\s+dot\s+[\w-]{1,63}){1,4}",
            re.IGNORECASE,
        ),
    ),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
    ("ICCID", re.compile(r"(?<!\d)89\d{17,18}(?!\d)")),
    (
        "VIN",
        re.compile(
            r"\b(?=[A-HJ-NPR-Z0-9]{0,16}\d)(?=[A-HJ-NPR-Z0-9]{0,16}[A-HJ-NPR-Z])"
            r"[A-HJ-NPR-Z0-9]{17}\b",
            re.IGNORECASE,
        ),
    ),
    # 15 digits, optionally grouped 2-6-6-1 as printed on device labels.
    ("IMEI", re.compile(r"(?<!\d)\d{2}[ -]?\d{6}[ -]?\d{6}[ -]?\d(?!\d)")),
    # International: + or 00, country code, then 7-15 more digits with light separators.
    ("PHONE", re.compile(r"(?<![\w+])(?:\+|00)[1-9](?:[ .\-()]{0,2}\d){6,14}(?!\d)")),
    # Greek local: 10 digits starting 69 or 2x, any single separators (dates are protected).
    ("PHONE", re.compile(r"(?<!\d)(?:69|2\d)(?:[ .-]?\d){8}(?!\d)")),
    ("AFM", re.compile(r"(?<!\d)\d{3}[ .]?\d{3}[ .]?\d{3}(?!\d)")),
    ("PLATE", re.compile(rf"(?<!\w)[{_PLATE_GR}{_PLATE_LATIN}]{{3}}[ -]?\d{{4}}(?!\d)")),
    # Lowercase plates only without a space, so "the 2024" is not a plate.
    (
        "PLATE",
        re.compile(rf"(?<!\w)[{_PLATE_GR}{_PLATE_LATIN}]{{3}}-?\d{{4}}(?!\d)", re.IGNORECASE),
    ),
]

# Dates and times are set aside before the patterns run, so they are never mistaken for
# phone numbers, then put back. Two-digit years only with "/" (phones rarely use "/").
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_MONTH = r"(?:0?[1-9]|1[0-2])"
_DATE_OR_TIME = re.compile(
    rf"(?<![\d.\-/])(?:\d{{4}}-{_MONTH}-{_DAY}|{_DAY}[./-]{_MONTH}[./-]\d{{4}}"
    rf"|{_DAY}/{_MONTH}/\d{{2}}|(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?)(?![.\-/:]?\d)"
)
# System IDs (GUIDs) are set aside too: their digit groups can look like card numbers.
_GUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Invisible or look-alike characters that could hide a number from the patterns.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\x00"))
_SPACES_AND_DASHES = str.maketrans(
    "\u00a0\u2007\u202f\u2009\u2002\u2003\u2010\u2011\u2012\u2013\u2014\u2015\u2212",
    " " * 6 + "-" * 7,
)

# Safety net for the final check: unexplained long numbers may be personal (cards, IDs).
_LEFTOVER_NUMBERS = re.compile(r"(?<!\d)\d{10,}(?!\d)|(?<!\d)\d{4}(?:[ -]\d{4}){2,}(?!\d)")

# A name word: Capitalised or ALL CAPS, Latin or Greek.
_NAME_WORD = (
    r"(?:[A-Z][a-zà-ÿ]+(?:[-'][A-Z][a-zà-ÿ]+)?|[A-Z]{2,}(?:[-'][A-Z]{2,})?"
    r"|[Α-ΩΆΈΉΊΌΎΏ][α-ωάέήίόύώϊϋΐΰς]+|[Α-ΩΆΈΉΊΌΎΏΪΫ]{2,})"
)
# Words that follow greetings but are not names ("Hi all,", "Dear Support Team,").
_NOT_NAMES = {
    "all", "team", "everyone", "support", "sir", "madam", "colleagues", "there", "friends",
    "όλους", "όλοι", "ομάδα", "συνάδελφοι", "φίλοι",
}  # fmt: skip
_NAME = rf"{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,3}}"
_TITLE = r"(?:(?i:mr|mrs|ms|miss|dr)\.?|κ\.|κα\.?|κε\.?|κύριε|κυρία)"

_GREETING = re.compile(
    r"^(?P<lead>[ \t]*(?i:dear|hi|hello|hey|good morning|good afternoon|good evening"
    r"|αγαπητέ|αγαπητή|αγαπητοί|γεια σας|γεια σου|γειά σας|καλημέρα|καλησπέρα|χαίρετε)"
    rf"[ \t]+(?:{_TITLE}[ \t]*)?)(?P<name>{_NAME})(?=[ \t]*[,!:])",
    re.MULTILINE,
)
_SIGNOFF = re.compile(
    r"^[ \t]*(?P<phrase>(?i:best regards|kind regards|warm regards|with regards|regards"
    r"|best wishes|best|many thanks|thanks in advance|thanks|thank you|cheers"
    r"|yours sincerely|yours faithfully|sincerely"
    r"|με εκτίμηση|με τιμή|με φιλικούς χαιρετισμούς|φιλικούς χαιρετισμούς|χαιρετισμούς"
    r"|φιλικά|καλή συνέχεια|ευχαριστώ εκ των προτέρων|ευχαριστώ πολύ|ευχαριστώ"
    r"|ευχαριστούμε))"
    rf"(?:[ \t]*[,.!]?[ \t]*|[ \t]*,[ \t]*(?:{_TITLE}[ \t]*)?(?P<name>{_NAME})[ \t]*)$"
)
_NAME_LINE = re.compile(rf"^[ \t]*(?:{_TITLE}[ \t]*)?(?P<name>{_NAME})[ \t]*$")
_SIGNOFF_SEARCH_LINES = limits().redact.signoff_search_lines  # sign-off only near the end

_QUOTE_HEADER = re.compile(
    r"^[ \t]*(?:On\s[^\n]*(?:\n[^\n]*)?\swrote:|Στις\s[^\n]*(?:\n[^\n]*)?\sέγραψε:"
    r"|-{2,}\s*(?:Original Message|Αρχικό μήνυμα)\s*-{2,})[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# Outlook-style reply header: a "From:" line followed within 4 lines by "Sent:" or "Date:".
_OUTLOOK_HEADER = re.compile(
    r"^[ \t]*(?:From|Από)[ \t]*:[^\n]*\n(?:[^\n]*\n){0,3}?"
    r"[ \t]*(?:Sent|Date|Στάλθηκε|Ημερομηνία|Αποστολή)[ \t]*:",
    re.MULTILINE | re.IGNORECASE,
)
_QUOTED_LINE = re.compile(r"^[ \t]*>.*(?:\n|$)", re.MULTILINE)
_DEVICE_FOOTER = re.compile(
    r"^[ \t]*(?:Sent from my .+|Στάλθηκε από .+|Εστάλη από .+)[ \t]*(?:\n|$)",
    re.MULTILINE | re.IGNORECASE,
)


class _Tokens:
    """Hands out stable tokens: the same value always gets the same [TYPE_n]."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, str]] = {}
        self.counts: Counter[str] = Counter()
        self.names: list[str] = []  # every name form seen, for replacing elsewhere

    def token(self, kind: str, value: str) -> str:
        table = self._tables.setdefault(kind, {})
        key = _normalise(kind, value)
        if key not in table:
            table[key] = f"[{kind}_{len(table) + 1}]"
        self.counts[kind] += 1
        return table[key]

    def name(self, value: str) -> str:
        value = value.strip()
        self.names.append(value)
        self.names.extend(
            w for w in value.split() if len(w) >= 3 and w.casefold() not in _NOT_NAMES
        )
        return self.token("NAME", value)


def _normalise(kind: str, value: str) -> str:
    if kind == "PHONE":
        digits = re.sub(r"\D", "", value)
        digits = digits.removeprefix("00")
        return digits[2:] if digits.startswith("30") and len(digits) == 12 else digits
    if kind == "PLATE":
        return re.sub(r"[ -]", "", value.upper()).translate(_TO_LATIN)
    if kind in ("VIN", "IBAN"):
        return value.replace(" ", "").upper()
    return value.casefold()


def _clean_characters(text: str) -> str:
    """Remove invisible characters; turn special spaces and dashes into plain ones."""
    return text.translate(_INVISIBLE).translate(_SPACES_AND_DASHES)


def _strip_quotes(text: str) -> str:
    starts = [m.start() for m in (_QUOTE_HEADER.search(text), _OUTLOOK_HEADER.search(text)) if m]
    if starts:
        text = text[: min(starts)]
    return _QUOTED_LINE.sub("", text)


def _protect_dates(text: str) -> tuple[str, list[str]]:
    """Swap GUIDs, dates and times for placeholders so no pattern can touch them."""
    saved: list[str] = []

    def keep(match: re.Match[str]) -> str:
        saved.append(match.group(0))
        return f"\x00{len(saved) - 1}\x00"

    return _DATE_OR_TIME.sub(keep, _GUID.sub(keep, text)), saved


def _restore_dates(text: str, saved: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], text)


def _strip_signature(text: str, tokens: _Tokens) -> str:
    """Cut at a '-- ' line, or at a sign-off near the end (keeping it, tokenising the name)."""
    lines = _DEVICE_FOOTER.sub("", text).split("\n")
    for i, line in enumerate(lines):
        if line.rstrip() == "--":
            lines = lines[:i]
            break
    filled = [i for i, line in enumerate(lines) if line.strip()]
    for i in filled[-_SIGNOFF_SEARCH_LINES:]:
        match = _SIGNOFF.match(lines[i])
        if not match:
            continue
        if match.group("name"):
            return "\n".join(
                lines[:i] + [f"{match.group('phrase')}, {tokens.name(match.group('name'))}"]
            )
        later = [j for j in filled if j > i]
        name_line = _NAME_LINE.match(lines[later[0]]) if later else None
        kept = lines[: i + 1] + ([tokens.name(name_line.group("name"))] if name_line else [])
        return "\n".join(kept)
    return "\n".join(lines)


def _redact_greetings(text: str, tokens: _Tokens) -> str:
    def replace(m: re.Match[str]) -> str:
        if all(word.casefold() in _NOT_NAMES for word in m.group("name").split()):
            return m.group(0)
        return m.group("lead") + tokens.name(m.group("name"))

    return _GREETING.sub(replace, text)


def _name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)


def _replace_known_names(text: str, tokens: _Tokens) -> str:
    for name in sorted(set(tokens.names), key=len, reverse=True):  # full names first
        text = _name_pattern(name).sub(lambda m: tokens.name(m.group(0)), text)
    return text


def _redact_patterns(text: str, tokens: _Tokens) -> str:
    for kind, pattern in _PATTERNS:
        text = pattern.sub(lambda m, k=kind: tokens.token(k, m.group(0)), text)
    return text


def _verify(text: str, tokens: _Tokens) -> list[str]:
    """Second pass: list every type that still matches. Empty list means clean."""
    problems = [kind for kind, pattern in _PATTERNS if pattern.search(text)]
    if _LEFTOVER_NUMBERS.search(text):
        problems.append("LONG_NUMBER")
    if any(_name_pattern(n).search(text) for n in set(tokens.names)):
        problems.append("NAME")
    return list(dict.fromkeys(problems))


def redact(text: str) -> RedactionResult:
    """Redact personal data. If ok is False, redacted_text is empty and must not be used."""
    if not isinstance(text, str):
        return RedactionResult("", {}, ok=False, problems=("NOT_TEXT",))
    try:
        tokens = _Tokens()
        body = _strip_quotes(_clean_characters(text))
        body = _redact_greetings(body, tokens)
        body = _replace_known_names(_strip_signature(body, tokens), tokens)
        body, dates = _protect_dates(body)
        body = _redact_patterns(body, tokens).strip()
        problems = _verify(body, tokens)
        body = _restore_dates(body, dates)
    except Exception:  # noqa: BLE001 - any failure means redaction is not confirmed
        return RedactionResult("", {}, ok=False, problems=("INTERNAL_ERROR",))
    counts = dict(tokens.counts)
    if problems:
        return RedactionResult("", counts, ok=False, problems=tuple(problems))
    return RedactionResult(body, counts, ok=True)


def find_personal_data(text: str) -> list[str]:
    """Types of personal data still detectable in text (empty list = looks clean).

    A last check before text is sent anywhere. It does not change the text, and it cannot
    see names (those are only found next to greetings and sign-offs by redact()).
    """
    if not isinstance(text, str):
        return ["NOT_TEXT"]
    body, _ = _protect_dates(_clean_characters(text))
    return _verify(body, _Tokens())


def redact_or_raise(text: str) -> str:
    """Return redacted text, or raise RedactionError if redaction could not be confirmed."""
    result = redact(text)
    if not result.ok:
        raise RedactionError(f"redaction failed: {', '.join(result.problems)}")
    return result.redacted_text

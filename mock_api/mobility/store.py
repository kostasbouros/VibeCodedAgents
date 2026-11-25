"""Loads the mock Mobility data and shifts its times so the reference moment becomes now.

That keeps the planted cases true whenever the mock runs ("offline for 2-3 days" stays
2-3 days). Set MOCK_FREEZE_TIME=1 to serve the stored times unchanged (useful in tests).

Times are written like the real API: "2026-11-25T12:00:00" or "...12:00:00.087", no zone
suffix. Fields ending in Utc/UTC are UTC; fields ending in Local are Athens time. Both move by
the same amount, and each keeps its own text format.
"""

import json
import os
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
API_DATE_FORMAT = "%d %b %Y %H:%M:%S"  # "21 May 2013 18:14:56", used by some query parameters
# e.g. GPSTimeUtc, LastReportedTimeUTC, ReadingTimeLocal, CreatedDateTimeUtc, CreatedDate
_TIME_KEY = re.compile(r"(?:Time|Date)\w*(?:Utc|UTC|Local)$|^CreatedDate$")


def parse_time(value: str) -> datetime:
    """ISO 8601 or "21 May 2013 18:14:56". A time without a zone is taken as UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.strptime(value.strip(), API_DATE_FORMAT)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_api_date(value: datetime) -> str:
    return value.astimezone(UTC).strftime(API_DATE_FORMAT)


def _shift_text(value: str, offset: timedelta) -> str:
    """Move a stored time by offset (whole minutes), keeping its fraction and suffix."""
    moved = parse_time(value) + offset
    text = moved.strftime("%Y-%m-%dT%H:%M:%S")
    suffix = "Z" if value.endswith("Z") else ""
    if "." in value:
        text += "." + value.split(".", 1)[1].removesuffix("Z")
    return text + suffix


@lru_cache
def _raw(name: str) -> Any:
    return json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def reference_time() -> datetime:
    return parse_time(_raw("meta")["ReferenceTimeUtc"])


def time_offset() -> timedelta:
    """How far to move stored times forward (0 when MOCK_FREEZE_TIME is set)."""
    if os.environ.get("MOCK_FREEZE_TIME"):
        return timedelta(0)
    # Whole minutes only, so times stored in the same minute stay in the same minute.
    return datetime.now(UTC).replace(second=0, microsecond=0) - reference_time()


def _shift(value: Any, offset: timedelta) -> Any:
    if isinstance(value, list):
        return [_shift(v, offset) for v in value]
    if isinstance(value, dict):
        return {
            k: _shift_text(v, offset)
            if isinstance(v, str) and v and _TIME_KEY.search(k)
            else _shift(v, offset)
            for k, v in value.items()
        }
    return value


def load(name: str) -> Any:
    """A data file with its times moved to the present (a fresh copy on every call)."""
    return _shift(_raw(name), time_offset())

"""Internal notifications for G4S staff (never customers).

For now a notification is a log line plus one JSON line in logs/notifications.jsonl. A real
channel (e-mail to the support team, Teams...) can be added here later without changing agents.
Pass only counts, IDs and file paths: never customer text or secrets.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

from shared.state import PROJECT_ROOT

log = structlog.get_logger(__name__)

NOTIFICATIONS_FILE = PROJECT_ROOT / "logs" / "notifications.jsonl"


def notify(
    agent: str,
    message: str,
    *,
    counts: dict[str, int] | None = None,
    path: str | None = None,
    file: Path = NOTIFICATIONS_FILE,
) -> None:
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent": agent,
        "message": message,
        "counts": counts or {},
        "path": path,
    }
    log.info("notification", agent=agent, message=message, counts=entry["counts"], path=path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

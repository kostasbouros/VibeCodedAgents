"""Flight recorder for one agent run.

    with Trace("desk_triage", agent_version="0.1.0", prompt_version="v1") as trace:
        trace.add_input("ticket", ticket_id)
        with trace.tool_call("zoho_desk.get_ticket"):
            ...
        trace.record_claude(usage)
        trace.record_validation("triage_schema", passed=True)
        trace.set_outcome("ticket triaged", items=1)

On exit it writes logs/traces/YYYY-MM-DD/<run_id>.json and closes the run in the runs table.
A trace holds IDs, names, numbers and statuses only: never prompt, reply or customer text.
"""

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from shared.claude_client import Usage
from shared.limits import limits
from shared.redact import redact
from shared.state import PROJECT_ROOT, StateStore, default_store

TRACES_DIR = PROJECT_ROOT / "logs" / "traces"
_ID = re.compile(r"^[\w.:#/@-]{1,128}$")  # an identifier, not a sentence
_NAME = re.compile(r"^[\w.:/-]{1,100}$")
_MAX_NOTE_CHARS = limits().trace.max_note_chars


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_note(text: str | None) -> str | None:
    """Short, scrubbed note. If scrubbing can't be confirmed the note is dropped.

    Scrub the whole text first, then shorten: cutting first could leave half an email or
    phone number that no longer matches any pattern.
    """
    if not text:
        return None
    result = redact(str(text))
    if not result.ok:
        return "[note removed: could not redact]"
    return result.redacted_text[:_MAX_NOTE_CHARS]


def _check(pattern: re.Pattern[str], value: object, what: str) -> str:
    text = str(value)
    if not pattern.match(text):
        raise ValueError(f"{what} must be a short identifier, got {text[:40]!r}")
    return text


class Trace:
    def __init__(
        self,
        agent: str,
        *,
        agent_version: str,
        model: str | None = None,
        prompt_version: str | None = None,
        dry_run: bool = False,
        mock: bool = True,
        store: StateStore | None = None,
        traces_dir: Path | None = None,
    ) -> None:
        self.agent = _check(_NAME, agent, "agent")
        self.agent_version = agent_version
        self.model = model
        self.prompt_version = prompt_version
        self.dry_run = dry_run
        self.mock = mock
        self._store = store
        self._traces_dir = traces_dir or TRACES_DIR
        self.run_id: str | None = None
        self.inputs: list[dict[str, str]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.claude_calls: list[dict[str, Any]] = []
        self.validations: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.errors: list[dict[str, str | None]] = []
        self.outcome: str | None = None
        self.items = 0
        self.status: str | None = None
        self.path: Path | None = None
        self._started: datetime | None = None

    # --- lifecycle ----------------------------------------------------------------------------

    def __enter__(self) -> Self:
        self._store = self._store or default_store()
        self.run_id = self._store.start_run(self.agent)
        self._started = _now()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            self.record_error(exc)
        finished = _now()
        try:
            self._set_status(exc)
            self.path = self._write(finished)
        except Exception as write_exc:  # noqa: BLE001 - the run must still be closed below
            self.record_error(write_exc)
            self._set_status(exc)
        try:
            self._finish_run()
        except Exception:
            if exc is None:
                raise  # nothing else went wrong: report this failure
            # otherwise keep the agent's own exception; the run row may stay 'running'
        return False  # never swallow the agent's exception

    def _set_status(self, exc: BaseException | None) -> None:
        if exc is not None:
            self.status = "failed"
        elif self.errors:
            self.status = "partial"
        else:
            self.status = "ok"

    def _finish_run(self) -> None:
        totals = self.totals()
        self._store.finish_run(  # type: ignore[union-attr]
            self.run_id,  # type: ignore[arg-type]
            status=self.status,
            items=self.items,
            tokens_in=totals["input_tokens"],
            tokens_out=totals["output_tokens"],
            cost_usd=totals["estimated_cost_usd"],
            error=self.errors[-1]["type"] if self.errors else None,
        )

    # --- recording ----------------------------------------------------------------------------

    def add_input(self, kind: str, item_id: str | int) -> None:
        """Reference an input by ID only (ticket, unit, account). Text is refused."""
        self.inputs.append(
            {"kind": _check(_NAME, kind, "input kind"), "id": _check(_ID, item_id, "input id")}
        )

    @contextmanager
    def tool_call(self, name: str) -> Iterator[None]:
        """Time a tool/API call and record ok/error. Exceptions are re-raised."""
        name = _check(_NAME, name, "tool name")
        started = time.perf_counter()
        try:
            yield
        except BaseException as exc:
            self.record_tool_call(name, "error", _ms(started), error_type=type(exc).__name__)
            raise
        self.record_tool_call(name, "ok", _ms(started))

    def record_tool_call(
        self, name: str, status: str, duration_ms: float, *, error_type: str | None = None
    ) -> None:
        entry: dict[str, Any] = {
            "name": _check(_NAME, name, "tool name"),
            "status": status,
            "duration_ms": duration_ms,
        }
        if error_type:
            entry["error_type"] = error_type
        self.tool_calls.append(entry)

    def record_claude(self, usage: Usage) -> None:
        self.model = self.model or usage.model
        self.claude_calls.append(
            {
                "model": usage.model,
                "attempts": usage.attempts,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_write_tokens": usage.cache_write_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
            }
        )

    def record_validation(self, name: str, *, passed: bool, detail: str | None = None) -> None:
        self.validations.append(
            {"name": _check(_NAME, name, "validation name"), "passed": passed,
             "detail": _safe_note(detail)}
        )  # fmt: skip

    def record_approval(self, action: str, *, approved: bool, by: str | None = None) -> None:
        """A human decision on a proposed action (who: a user ID, not a name)."""
        self.approvals.append(
            {"action": _check(_NAME, action, "action"), "approved": approved,
             "by": _check(_ID, by, "approver id") if by else None}
        )  # fmt: skip

    def record_error(self, exc: BaseException | str) -> None:
        """Record an error. For exceptions only the type is kept: their messages can carry
        customer text (e.g. Pydantic input_value) or tokens the scrubber can't recognise.
        A string is the agent's own note and is scrubbed and shortened."""
        if isinstance(exc, BaseException):
            self.errors.append({"type": type(exc).__name__, "message": None})
        else:
            self.errors.append({"type": "Error", "message": _safe_note(exc)})

    def set_outcome(self, outcome: str, *, items: int | None = None) -> None:
        self.outcome = _safe_note(outcome)
        if items is not None:
            self.items = items

    # --- output -------------------------------------------------------------------------------

    def totals(self) -> dict[str, Any]:
        costs = [c["estimated_cost_usd"] for c in self.claude_calls]
        return {
            "input_tokens": sum(c["input_tokens"] for c in self.claude_calls),
            "output_tokens": sum(c["output_tokens"] for c in self.claude_calls),
            "cache_read_tokens": sum(c["cache_read_tokens"] for c in self.claude_calls),
            # an unknown price makes the total unknown, rather than silently too low
            "estimated_cost_usd": 0.0 if not costs else (
                None if None in costs else round(sum(costs), 6)
            ),
        }  # fmt: skip

    def to_dict(self, finished: datetime | None = None) -> dict[str, Any]:
        started = self._started or _now()
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "agent_version": self.agent_version,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "mock": self.mock,
            "dry_run": self.dry_run,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds") if finished else None,
            "duration_ms": round((finished - started).total_seconds() * 1000, 1)
            if finished
            else None,
            "status": self.status,
            "outcome": self.outcome,
            "items": self.items,
            "inputs": self.inputs,
            "tool_calls": self.tool_calls,
            "claude_calls": self.claude_calls,
            "totals": self.totals(),
            "validations": self.validations,
            "approvals": self.approvals,
            "errors": self.errors,
        }

    def _write(self, finished: datetime) -> Path:
        day = (self._started or finished).strftime("%Y-%m-%d")
        folder = self._traces_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.run_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(finished), indent=2), encoding="utf-8")
        os.replace(tmp, path)  # atomic: readers never see a half-written file
        return path


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)

"""Tests for shared/trace.py. Each test uses its own database and traces folder."""

import json
from pathlib import Path

import pytest

from shared.claude_client import Usage
from shared.state import StateStore
from shared.trace import Trace


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def make_trace(store: StateStore, tmp_path: Path, **kw: object) -> Trace:
    return Trace(
        "desk_triage",
        agent_version="0.1.0",
        prompt_version="v1",
        store=store,
        traces_dir=tmp_path / "traces",
        **kw,  # type: ignore[arg-type]
    )


def usage(cost: float | None = 0.01) -> Usage:
    return Usage(
        model="claude-opus-5-5",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        attempts=1,
        estimated_cost_usd=cost,
    )


def test_successful_run_writes_trace_file_and_updates_runs(store: StateStore, tmp_path: Path):
    with make_trace(store, tmp_path) as trace:
        trace.add_input("ticket", "T-1001")
        with trace.tool_call("zoho_desk.get_ticket"):
            pass
        trace.record_claude(usage())
        trace.record_validation("triage_schema", passed=True)
        trace.record_approval("add_tag", approved=True, by="user-17")
        trace.set_outcome("ticket triaged", items=1)

    data = json.loads(trace.path.read_text(encoding="utf-8"))
    assert trace.path.parent.parent == tmp_path / "traces"
    assert trace.path.name == f"{trace.run_id}.json"
    assert len(trace.run_id) == 32  # uuid4 hex
    assert data["status"] == "ok"
    assert data["agent_version"] == "0.1.0" and data["prompt_version"] == "v1"
    assert data["model"] == "claude-opus-5-5"
    assert data["inputs"] == [{"kind": "ticket", "id": "T-1001"}]
    assert data["tool_calls"][0]["name"] == "zoho_desk.get_ticket"
    assert data["tool_calls"][0]["status"] == "ok"
    assert data["totals"] == {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 500,
        "estimated_cost_usd": 0.01,
    }
    assert data["validations"][0]["passed"] is True
    assert data["approvals"][0] == {"action": "add_tag", "approved": True, "by": "user-17"}

    run = store.get_run(trace.run_id)
    assert (run["status"], run["items"], run["tokens_in"], run["tokens_out"]) == (
        "ok",
        1,
        1000,
        200,
    )
    assert run["cost_usd"] == pytest.approx(0.01)


def test_trace_folder_is_named_by_date(store: StateStore, tmp_path: Path) -> None:
    with make_trace(store, tmp_path) as trace:
        pass
    folder = trace.path.parent.name
    assert len(folder) == 10 and folder[4] == "-" and folder[7] == "-"


def test_crash_is_recorded_as_failed_and_not_swallowed(store: StateStore, tmp_path: Path):
    with pytest.raises(KeyError), make_trace(store, tmp_path) as trace:
        raise KeyError("boom")
    data = json.loads(trace.path.read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["errors"][0]["type"] == "KeyError"
    assert store.get_run(trace.run_id)["status"] == "failed"
    assert store.get_run(trace.run_id)["error"] == "KeyError"


def test_failed_tool_call_is_timed_recorded_and_reraised(store: StateStore, tmp_path: Path):
    with make_trace(store, tmp_path) as trace:
        with pytest.raises(TimeoutError), trace.tool_call("mobility.get_units"):
            raise TimeoutError
        trace.record_error("mobility unavailable, skipped")
    data = json.loads(trace.path.read_text(encoding="utf-8"))
    assert data["tool_calls"][0]["status"] == "error"
    assert data["tool_calls"][0]["error_type"] == "TimeoutError"
    assert data["tool_calls"][0]["duration_ms"] >= 0
    assert data["status"] == "partial"  # handled errors, no crash


def test_inputs_must_be_ids_not_text(store: StateStore, tmp_path: Path) -> None:
    with make_trace(store, tmp_path) as trace:
        with pytest.raises(ValueError, match="identifier"):
            trace.add_input("ticket", "My truck stopped, call me on 6912345678")
        trace.add_input("unit", 356938)
    assert trace.inputs == [{"kind": "unit", "id": "356938"}]


def test_crash_messages_are_not_saved_only_their_type(store: StateStore, tmp_path: Path):
    with pytest.raises(RuntimeError), make_trace(store, tmp_path) as trace:
        raise RuntimeError("customer maria@example.gr token=sk-FAKE-123 not found")
    text = trace.path.read_text(encoding="utf-8")
    assert "maria" not in text and "sk-FAKE" not in text
    assert json.loads(text)["errors"] == [{"type": "RuntimeError", "message": None}]


def test_notes_are_scrubbed(store: StateStore, tmp_path: Path) -> None:
    with make_trace(store, tmp_path) as trace:
        trace.record_validation("plate_check", passed=False, detail="plate IKA-1234 unknown")
        trace.record_error("customer maria@example.gr not found, skipped")
    text = trace.path.read_text(encoding="utf-8")
    assert "maria@example.gr" not in text and "IKA-1234" not in text
    assert "[EMAIL_1]" in text and "[PLATE_1]" in text


def test_trace_never_contains_prompt_or_reply_fields(store: StateStore, tmp_path: Path):
    with make_trace(store, tmp_path) as trace:
        trace.record_claude(usage())
    data = json.loads(trace.path.read_text(encoding="utf-8"))
    claude_keys = set(data["claude_calls"][0])
    assert not claude_keys & {"prompt", "system", "user", "reply", "text", "content"}


def test_unknown_price_makes_total_cost_unknown(store: StateStore, tmp_path: Path) -> None:
    with make_trace(store, tmp_path) as trace:
        trace.record_claude(usage(0.01))
        trace.record_claude(usage(None))
    assert trace.totals()["estimated_cost_usd"] is None
    assert store.get_run(trace.run_id)["cost_usd"] is None


def test_dry_run_and_mock_flags_are_recorded(store: StateStore, tmp_path: Path) -> None:
    with make_trace(store, tmp_path, dry_run=True, mock=True) as trace:
        pass
    data = json.loads(trace.path.read_text(encoding="utf-8"))
    assert (data["dry_run"], data["mock"]) == (True, True)


def test_no_temp_file_is_left_behind(store: StateStore, tmp_path: Path) -> None:
    with make_trace(store, tmp_path) as trace:
        pass
    assert list(trace.path.parent.glob("*.tmp")) == []


# --- Found in review ------------------------------------------------------------------------


def test_note_is_scrubbed_before_it_is_shortened(store: StateStore, tmp_path: Path) -> None:
    # The email straddles the 200-character cut; cutting first would leave "maria.pap@gmai".
    note = "x" * 190 + " maria.papadopoulou@gmail.com"
    with make_trace(store, tmp_path) as trace:
        trace.record_error(note)
    text = trace.path.read_text(encoding="utf-8")
    assert "maria" not in text and "@gmai" not in text


def test_validation_error_input_text_never_reaches_the_trace(store: StateStore, tmp_path):
    from pydantic import BaseModel, ValidationError

    class Ticket(BaseModel):
        priority: int

    with pytest.raises(ValidationError), make_trace(store, tmp_path) as trace:
        Ticket.model_validate({"priority": "Nikos Papadopoulos wants a call back"})
    text = trace.path.read_text(encoding="utf-8")
    assert "Nikos" not in text and "Papadopoulos" not in text


def test_run_is_closed_even_if_trace_file_cannot_be_written(
    store: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = make_trace(store, tmp_path)

    def broken_write(finished: object) -> Path:
        raise PermissionError("disk is read-only")

    monkeypatch.setattr(trace, "_write", broken_write)
    with trace:
        trace.set_outcome("done", items=1)
    run = store.get_run(trace.run_id)
    assert run["status"] == "partial"  # the work finished but the trace file was lost
    assert run["error"] == "PermissionError"


def test_agent_exception_is_kept_if_closing_the_run_also_fails(
    store: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = make_trace(store, tmp_path)
    monkeypatch.setattr(trace, "_finish_run", lambda: (_ for _ in ()).throw(OSError("db gone")))
    with pytest.raises(KeyError), trace:
        raise KeyError("the real problem")


def test_closing_failure_is_reported_when_agent_succeeded(
    store: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = make_trace(store, tmp_path)
    monkeypatch.setattr(trace, "_finish_run", lambda: (_ for _ in ()).throw(OSError("db gone")))
    with pytest.raises(OSError, match="db gone"), trace:
        pass

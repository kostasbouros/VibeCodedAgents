"""Tests for shared/state.py. Each test gets its own database in a temporary folder."""

import sqlite3
import threading
from pathlib import Path

import pytest

from shared import state
from shared.state import ALERT_STATES, StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "sub" / "state.db")


def count(store: StateStore, table: str) -> int:
    with sqlite3.connect(store.path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- Setup ----------------------------------------------------------------------------------


def test_tables_and_folder_are_created_automatically(store: StateStore) -> None:
    assert store.path.is_file()
    with sqlite3.connect(store.path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"processed", "cursors", "alerts", "runs"} <= tables


def test_opening_an_existing_database_keeps_its_data(store: StateStore) -> None:
    store.set_cursor("mobility_start_id", 42)
    assert StateStore(store.path).get_cursor("mobility_start_id") == "42"


# --- processed: idempotency -----------------------------------------------------------------


def test_mark_done_twice_does_not_duplicate(store: StateStore) -> None:
    assert store.mark_done("desk_triage", "ticket", "T-1", "run-1") is True
    assert store.mark_done("desk_triage", "ticket", "T-1", "run-2") is False
    assert count(store, "processed") == 1


def test_already_done(store: StateStore) -> None:
    assert store.already_done("desk_triage", "ticket", "T-1") is False
    store.mark_done("desk_triage", "ticket", "T-1", "run-1")
    assert store.already_done("desk_triage", "ticket", "T-1") is True


def test_same_item_id_is_separate_per_agent_and_type(store: StateStore) -> None:
    store.mark_done("desk_triage", "ticket", "1", None)
    assert store.already_done("fleet_report", "ticket", "1") is False
    assert store.already_done("desk_triage", "device", "1") is False


def test_numeric_and_text_ids_are_the_same_item(store: StateStore) -> None:
    store.mark_done("device_watchdog", "unit", 123, None)  # type: ignore[arg-type]
    assert store.already_done("device_watchdog", "unit", "123") is True


def test_parallel_mark_done_records_exactly_once(store: StateStore) -> None:
    results: list[bool] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(store.mark_done("desk_triage", "ticket", "T-9", None))
        except BaseException as exc:  # noqa: BLE001 - a crash in a thread must fail the test
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(results) == 8  # every attempt finished
    assert results.count(True) == 1
    assert count(store, "processed") == 1


# --- cursors --------------------------------------------------------------------------------


def test_cursor_default_set_and_update(store: StateStore) -> None:
    assert store.get_cursor("alerts_start_uid") is None
    assert store.get_cursor("alerts_start_uid", "0") == "0"
    store.set_cursor("alerts_start_uid", "100")
    store.set_cursor("alerts_start_uid", "250")
    assert store.get_cursor("alerts_start_uid") == "250"
    assert count(store, "cursors") == 1


# --- alerts: forward-only state -------------------------------------------------------------


def new_alert(store: StateStore, state_: str = "new", alert_id: str = "A-1") -> str:
    return store.upsert_alert(
        alert_id, agent="device_watchdog", rule_id="silent_device", unit_uid="U-7", state=state_
    )


def test_alert_moves_forward_through_every_state(store: StateStore) -> None:
    assert new_alert(store, "new") == "new"
    for expected in ("open", "notified", "resolved"):
        assert new_alert(store, expected) == expected
        assert store.get_alert("A-1")["state"] == expected


@pytest.mark.parametrize(
    ("current", "requested"),
    [("open", "new"), ("notified", "open"), ("notified", "new"), ("resolved", "notified"),
     ("resolved", "new")],
)  # fmt: skip
def test_alert_state_never_moves_backwards(store: StateStore, current: str, requested: str) -> None:
    new_alert(store, current)
    assert new_alert(store, requested) == current
    assert store.get_alert("A-1")["state"] == current


def test_alert_can_skip_forward(store: StateStore) -> None:
    new_alert(store, "new")
    assert new_alert(store, "notified") == "notified"


def test_redetecting_an_alert_updates_last_seen_not_first_seen(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "_now", lambda: "2026-01-01T00:00:00+00:00")
    new_alert(store)
    monkeypatch.setattr(state, "_now", lambda: "2026-01-02T00:00:00+00:00")
    new_alert(store)
    alert = store.get_alert("A-1")
    assert (alert["first_seen"], alert["last_seen"]) == (
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
    )
    assert count(store, "alerts") == 1


def test_resolved_alert_stays_resolved_even_if_seen_again(store: StateStore) -> None:
    new_alert(store, "open")
    assert store.resolve_alert("A-1") is True
    assert new_alert(store, "new") == "resolved"
    assert store.open_alerts() == []


def test_resolve_alert_is_idempotent(store: StateStore) -> None:
    new_alert(store)
    assert store.resolve_alert("A-1") is True
    assert store.resolve_alert("A-1") is False
    assert store.resolve_alert("does-not-exist") is False
    assert store.get_alert("A-1")["resolved_at"] is not None


def test_invalid_state_is_rejected(store: StateStore) -> None:
    with pytest.raises(ValueError, match="alert state"):
        new_alert(store, "closed")


def test_database_also_rejects_invalid_state(store: StateStore) -> None:
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO alerts VALUES ('X', 'a', 'u', 'r', 'closed', 't', 't', NULL, NULL)")


def test_open_alerts_filters_by_agent_and_parses_details(store: StateStore) -> None:
    store.upsert_alert("A-1", agent="device_watchdog", rule_id="r", details={"hours": 26})
    store.upsert_alert("A-2", agent="install_verifier", rule_id="r")
    store.upsert_alert("A-3", agent="device_watchdog", rule_id="r", state="resolved")
    alerts = store.open_alerts("device_watchdog")
    assert [a["alert_id"] for a in alerts] == ["A-1"]
    assert alerts[0]["details"] == {"hours": 26}
    assert len(store.open_alerts()) == 2


def test_details_are_kept_when_not_given_again(store: StateStore) -> None:
    store.upsert_alert("A-1", agent="a", rule_id="r", details={"hours": 26})
    store.upsert_alert("A-1", agent="a", rule_id="r", state="open")
    assert store.get_alert("A-1")["details"] == {"hours": 26}


def test_alert_states_are_in_forward_order() -> None:
    assert ALERT_STATES == ("new", "open", "notified", "resolved")


# --- runs -----------------------------------------------------------------------------------


def test_run_lifecycle(store: StateStore) -> None:
    run_id = store.start_run("fleet_report")
    assert store.get_run(run_id)["status"] == "running"
    store.finish_run(run_id, status="ok", items=12, tokens_in=5000, tokens_out=800, cost_usd=0.02)
    run = store.get_run(run_id)
    assert (run["status"], run["items"], run["tokens_in"], run["tokens_out"]) == (
        "ok",
        12,
        5000,
        800,
    )
    assert run["cost_usd"] == pytest.approx(0.02)
    assert run["finished_at"] is not None


def test_run_cannot_be_finished_twice(store: StateStore) -> None:
    run_id = store.start_run("fleet_report")
    store.finish_run(run_id, status="failed", error="ConnectError")
    with pytest.raises(ValueError, match="already finished"):
        store.finish_run(run_id, status="ok")
    assert store.get_run(run_id)["status"] == "failed"


def test_run_error_is_truncated_and_status_checked(store: StateStore) -> None:
    run_id = store.start_run("fleet_report")
    with pytest.raises(ValueError, match="status"):
        store.finish_run(run_id, status="done")
    store.finish_run(run_id, status="failed", error="x" * 2000)
    assert len(store.get_run(run_id)["error"]) == 500


# --- Transactions ---------------------------------------------------------------------------


def test_failed_transaction_leaves_nothing_behind(store: StateStore) -> None:
    with pytest.raises(RuntimeError), store._transaction() as db:
        db.execute("INSERT INTO cursors VALUES ('half', '1', 'now')")
        raise RuntimeError("crash halfway")
    assert store.get_cursor("half") is None


# --- Module-level functions use STATE_DB_PATH -----------------------------------------------


def test_module_functions_use_the_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "default_store", lambda: StateStore(tmp_path / "s.db"))
    assert state.mark_done("a", "t", "1", None) is True
    assert state.already_done("a", "t", "1") is True
    state.set_cursor("c", 5)
    assert state.get_cursor("c") == "5"
    assert state.upsert_alert("A", agent="a", rule_id="r") == "new"
    assert len(state.open_alerts()) == 1
    assert state.resolve_alert("A") is True
    run_id = state.start_run("a")
    state.finish_run(run_id, status="ok")
    assert (tmp_path / "s.db").is_file()


# --- Claims (found in review: overlapping runs) ---------------------------------------------


def test_second_run_cannot_claim_an_item_another_run_is_working_on(store: StateStore) -> None:
    assert store.claim("desk_triage", "ticket", "T-1", "run-A") is True
    assert store.claim("desk_triage", "ticket", "T-1", "run-B") is False
    assert store.already_done("desk_triage", "ticket", "T-1") is False  # claimed, not done


def test_same_run_can_reclaim_its_own_item(store: StateStore) -> None:
    store.claim("desk_triage", "ticket", "T-1", "run-A")
    assert store.claim("desk_triage", "ticket", "T-1", "run-A") is True


def test_done_item_cannot_be_claimed(store: StateStore) -> None:
    store.claim("desk_triage", "ticket", "T-1", "run-A")
    assert store.mark_done("desk_triage", "ticket", "T-1", "run-A") is True
    assert store.mark_done("desk_triage", "ticket", "T-1", "run-A") is False
    assert store.claim("desk_triage", "ticket", "T-1", "run-B") is False
    assert store.already_done("desk_triage", "ticket", "T-1") is True
    assert count(store, "processed") == 1


def test_released_claim_can_be_retried(store: StateStore) -> None:
    store.claim("desk_triage", "ticket", "T-1", "run-A")
    assert store.release("desk_triage", "ticket", "T-1", "run-B") is False  # not B's claim
    assert store.release("desk_triage", "ticket", "T-1", "run-A") is True
    assert store.claim("desk_triage", "ticket", "T-1", "run-B") is True


def test_stale_claim_from_a_crashed_run_is_taken_over(
    store: StateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(state, "_now", lambda: "2026-01-01T10:00:00+00:00")
    store.claim("desk_triage", "ticket", "T-1", "run-A")
    monkeypatch.setattr(state, "_now", lambda: "2026-01-01T10:30:00+00:00")
    assert store.claim("desk_triage", "ticket", "T-1", "run-B") is False  # 30 min: still fresh
    monkeypatch.setattr(state, "_now", lambda: "2026-01-01T11:01:00+00:00")
    assert store.claim("desk_triage", "ticket", "T-1", "run-B") is True  # 61 min: stale


def test_parallel_claims_let_exactly_one_run_through(store: StateStore) -> None:
    results: list[bool] = []
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            results.append(store.claim("desk_triage", "ticket", "T-5", f"run-{n}"))
        except BaseException as exc:  # noqa: BLE001 - a crash in a thread must fail the test
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert results.count(True) == 1


def test_reads_work_while_another_connection_holds_the_write_lock(store: StateStore) -> None:
    store.set_cursor("c", 1)
    writer = sqlite3.connect(store.path, isolation_level=None, timeout=0)
    writer.execute("BEGIN IMMEDIATE")  # another agent is in the middle of a write
    try:
        assert store.get_cursor("c") == "1"  # would wait 30 s if reads took the write lock
    finally:
        writer.execute("ROLLBACK")
        writer.close()

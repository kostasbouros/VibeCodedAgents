"""Small SQLite store that remembers work between runs (data/state.db by default).

- processed: which items an agent has handled, so nothing is done twice.
- cursors:   bookmarks such as the Mobility StartId or alerts StartUID.
- alerts:    alert lifecycle; state only moves forward: new -> open -> notified -> resolved.
- runs:      one row per agent run (status, items, tokens, cost).

Every write runs in its own transaction; tables are created on first use.
Use StateStore(path) directly, or the module-level functions, which use STATE_DB_PATH.
"""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from shared.config import get_settings
from shared.limits import limits

PROJECT_ROOT = Path(__file__).resolve().parents[1]

ALERT_STATES = ("new", "open", "notified", "resolved")  # forward order
_RANK = {state: i for i, state in enumerate(ALERT_STATES)}
RUN_STATUSES = ("ok", "partial", "failed")
_MAX_ERROR_CHARS = limits().state.max_error_chars

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed (
    agent TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_id TEXT NOT NULL,
    run_id TEXT,
    processed_at TEXT NOT NULL,  -- time of the last status change
    status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('claimed', 'done')),
    PRIMARY KEY (agent, item_type, item_id)
);
CREATE TABLE IF NOT EXISTS cursors (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    unit_uid TEXT,
    rule_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('new', 'open', 'notified', 'resolved')),
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (agent, state);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'failed')),
    items INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL DEFAULT 0,  -- NULL when the price is unknown
    error TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        self.path = path if path.is_absolute() else PROJECT_ROOT / path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # executescript manages its own transaction, so it runs outside _transaction().
        # WAL lets readers work while one agent writes; it is stored in the file, so it is
        # set once here (setting it on every connection fails under concurrent access).
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """One connection per call. Writes use BEGIN IMMEDIATE, which takes the write lock up
        front so two agents can't interleave a read-then-write (it waits up to 30 s for the
        lock). Reads take no write lock, so they never wait behind a writer.
        Commits on success, rolls back on error."""
        with closing(sqlite3.connect(self.path, timeout=30, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    # --- processed --------------------------------------------------------------------------

    # Safe pattern for an external write (check state first, record it after):
    #     if store.claim(agent, "ticket", tid, run_id):   # no one else is on it
    #         try:
    #             ...write to Zoho...
    #         except Exception:
    #             store.release(agent, "ticket", tid, run_id)  # let the next run retry
    #             raise
    #         store.mark_done(agent, "ticket", tid, run_id)

    def already_done(self, agent: str, item_type: str, item_id: str) -> bool:
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT 1 FROM processed WHERE agent=? AND item_type=? AND item_id=? "
                "AND status='done'",
                (agent, item_type, str(item_id)),
            ).fetchone()
        return row is not None

    def claim(
        self,
        agent: str,
        item_type: str,
        item_id: str,
        run_id: str,
        *,
        stale_after_minutes: float | None = None,
    ) -> bool:
        """Claim an item before acting on it. Returns True if this run may go ahead.

        False if it is already done, or another run claimed it less than
        `stale_after_minutes` ago. A claim older than that (the other run probably crashed)
        is taken over: the external write may then already have happened, so the agent
        must check the remote system before writing again.
        """
        key = (agent, item_type, str(item_id))
        now = _now()
        if stale_after_minutes is None:
            stale_after_minutes = limits().state.claim_stale_after_minutes
        with self._transaction() as db:
            row = db.execute(
                "SELECT status, run_id, processed_at FROM processed "
                "WHERE agent=? AND item_type=? AND item_id=?",
                key,
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO processed VALUES (?, ?, ?, ?, ?, 'claimed')", (*key, run_id, now)
                )
                return True
            if row["status"] == "done":
                return False
            if row["run_id"] == run_id:
                return True
            age = datetime.fromisoformat(now) - datetime.fromisoformat(row["processed_at"])
            if age.total_seconds() < stale_after_minutes * 60:
                return False
            db.execute(
                "UPDATE processed SET run_id=?, processed_at=? "
                "WHERE agent=? AND item_type=? AND item_id=?",
                (run_id, now, *key),
            )
            return True

    def release(self, agent: str, item_type: str, item_id: str, run_id: str) -> bool:
        """Drop this run's claim (e.g. the external write failed) so a later run can retry."""
        with self._transaction() as db:
            cur = db.execute(
                "DELETE FROM processed WHERE agent=? AND item_type=? AND item_id=? "
                "AND status='claimed' AND run_id=?",
                (agent, item_type, str(item_id), run_id),
            )
        return cur.rowcount == 1

    def mark_done(self, agent: str, item_type: str, item_id: str, run_id: str | None) -> bool:
        """Record an item as handled. Returns False if it was already done (no change)."""
        with self._transaction() as db:
            cur = db.execute(
                "INSERT INTO processed VALUES (?, ?, ?, ?, ?, 'done') "
                "ON CONFLICT(agent, item_type, item_id) DO UPDATE SET status='done', "
                "run_id=excluded.run_id, processed_at=excluded.processed_at "
                "WHERE processed.status='claimed'",
                (agent, item_type, str(item_id), run_id, _now()),
            )
        return cur.rowcount == 1

    # --- cursors ----------------------------------------------------------------------------

    def get_cursor(self, name: str, default: str | None = None) -> str | None:
        with self._transaction(write=False) as db:
            row = db.execute("SELECT value FROM cursors WHERE name=?", (name,)).fetchone()
        return row["value"] if row else default

    def set_cursor(self, name: str, value: str | int) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO cursors VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (name, str(value), _now()),
            )

    # --- alerts -----------------------------------------------------------------------------

    def upsert_alert(
        self,
        alert_id: str,
        *,
        agent: str,
        rule_id: str,
        unit_uid: str | None = None,
        state: str = "new",
        details: dict[str, Any] | None = None,
    ) -> str:
        """Create or refresh an alert and return its state afterwards.

        The state only moves forward (new -> open -> notified -> resolved). Asking for an
        earlier state keeps the current one. A resolved alert stays resolved: a problem that
        comes back needs a new alert_id.
        """
        _check_state(state)
        now = _now()
        details_json = json.dumps(details, sort_keys=True) if details is not None else None
        with self._transaction() as db:
            row = db.execute("SELECT state FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (alert_id, agent, unit_uid, rule_id, state, now, now,
                     now if state == "resolved" else None, details_json),
                )  # fmt: skip
                return state
            current = row["state"]
            if current == "resolved":
                return current
            new_state = state if _RANK[state] > _RANK[current] else current
            db.execute(
                "UPDATE alerts SET state=?, last_seen=?, "
                "details_json=COALESCE(?, details_json), "
                "resolved_at=CASE WHEN ?='resolved' THEN ? ELSE resolved_at END "
                "WHERE alert_id=?",
                (new_state, now, details_json, new_state, now, alert_id),
            )
            return new_state

    def resolve_alert(self, alert_id: str) -> bool:
        """Mark an alert resolved. Returns False if it doesn't exist or was already resolved."""
        now = _now()
        with self._transaction() as db:
            cur = db.execute(
                "UPDATE alerts SET state='resolved', resolved_at=?, last_seen=? "
                "WHERE alert_id=? AND state != 'resolved'",
                (now, now, alert_id),
            )
        return cur.rowcount == 1

    def open_alerts(self, agent: str | None = None) -> list[dict[str, Any]]:
        """All alerts not yet resolved (optionally for one agent), oldest first."""
        sql = "SELECT * FROM alerts WHERE state != 'resolved'"
        params: tuple[str, ...] = ()
        if agent is not None:
            sql += " AND agent=?"
            params = (agent,)
        with self._transaction(write=False) as db:
            rows = db.execute(sql + " ORDER BY first_seen, alert_id", params).fetchall()
        return [_alert_dict(r) for r in rows]

    def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        with self._transaction(write=False) as db:
            row = db.execute("SELECT * FROM alerts WHERE alert_id=?", (alert_id,)).fetchone()
        return _alert_dict(row) if row else None

    # --- runs -------------------------------------------------------------------------------

    def start_run(self, agent: str) -> str:
        run_id = uuid.uuid4().hex
        with self._transaction() as db:
            db.execute(
                "INSERT INTO runs (run_id, agent, started_at, status) VALUES (?, ?, ?, 'running')",
                (run_id, agent, _now()),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        items: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float | None = 0.0,
        error: str | None = None,
    ) -> None:
        """Close a run. `error` is cut to 500 characters; never pass secrets or customer text."""
        if status not in RUN_STATUSES:
            raise ValueError(f"status must be one of {RUN_STATUSES}, got {status!r}")
        with self._transaction() as db:
            cur = db.execute(
                "UPDATE runs SET finished_at=?, status=?, items=?, tokens_in=?, tokens_out=?, "
                "cost_usd=?, error=? WHERE run_id=? AND status='running'",
                (_now(), status, items, tokens_in, tokens_out, cost_usd,
                 error[:_MAX_ERROR_CHARS] if error else None, run_id),
            )  # fmt: skip
            if cur.rowcount != 1:
                raise ValueError(f"run {run_id!r} not found or already finished")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._transaction(write=False) as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def _check_state(state: str) -> None:
    if state not in _RANK:
        raise ValueError(f"alert state must be one of {ALERT_STATES}, got {state!r}")


def _alert_dict(row: sqlite3.Row) -> dict[str, Any]:
    alert = dict(row)
    raw = alert.pop("details_json")
    alert["details"] = json.loads(raw) if raw else None
    return alert


# --- Module-level functions using the store at STATE_DB_PATH -------------------------------


@lru_cache
def default_store() -> StateStore:
    return StateStore(get_settings().state_db_path)


def already_done(agent: str, item_type: str, item_id: str) -> bool:
    return default_store().already_done(agent, item_type, item_id)


def mark_done(agent: str, item_type: str, item_id: str, run_id: str | None) -> bool:
    return default_store().mark_done(agent, item_type, item_id, run_id)


def get_cursor(name: str, default: str | None = None) -> str | None:
    return default_store().get_cursor(name, default)


def set_cursor(name: str, value: str | int) -> None:
    default_store().set_cursor(name, value)


def upsert_alert(alert_id: str, **kwargs: Any) -> str:
    return default_store().upsert_alert(alert_id, **kwargs)


def open_alerts(agent: str | None = None) -> list[dict[str, Any]]:
    return default_store().open_alerts(agent)


def resolve_alert(alert_id: str) -> bool:
    return default_store().resolve_alert(alert_id)


def start_run(agent: str) -> str:
    return default_store().start_run(agent)


def finish_run(run_id: str, **kwargs: Any) -> None:
    default_store().finish_run(run_id, **kwargs)


def claim(agent: str, item_type: str, item_id: str, run_id: str, **kwargs: Any) -> bool:
    return default_store().claim(agent, item_type, item_id, run_id, **kwargs)


def release(agent: str, item_type: str, item_id: str, run_id: str) -> bool:
    return default_store().release(agent, item_type, item_id, run_id)

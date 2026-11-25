import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from agents.device_watchdog.snapshot import Reading, UnitSnapshot
from connectors.mobility import Alert, Unit, UnitLatestPosition
from shared.state import StateStore

MOCK_DATA = Path(__file__).resolve().parents[2] / "mock_api" / "mobility" / "data"
NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)  # mock data reference time


def _load(name: str) -> Any:
    return json.loads((MOCK_DATA / f"{name}.json").read_text(encoding="utf-8"))


class FakeMobility:
    """The mock data through the same read-only calls as MobilityClient. Records calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.positions = [UnitLatestPosition.model_validate(p) for p in _load("latest_positions")]

    def list_units(self) -> list[Unit]:
        self.calls.append("list_units")
        return [Unit.model_validate(u) for u in _load("units")]

    def latest_positions(self, since: datetime | None = None) -> list[UnitLatestPosition]:
        self.calls.append("latest_positions")
        return [p for p in self.positions
                if since is None or (p.last_reported_time_utc or since) > since]  # fmt: skip

    def alerts(self) -> tuple[list[Alert], str | None]:
        self.calls.append("alerts")
        return [Alert.model_validate(a) for a in _load("alerts")], None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass


@pytest.fixture
def mobility() -> FakeMobility:
    return FakeMobility()


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def readings(name: str, *values: Any, start: datetime = NOW, step_s: int = 60) -> dict:
    """History for one input: values oldest first, the last one at `start`."""
    n = len(values)
    times = [start.timestamp() - (n - 1 - i) * step_s for i in range(n)]
    return {name: [Reading(time_utc=datetime.fromtimestamp(t, UTC), value=v)
                   for t, v in zip(times, values, strict=True)]}  # fmt: skip


def snap(uid: str = "U1", *, history: dict | None = None, **kwargs: Any) -> UnitSnapshot:
    """A snapshot; `values` defaults to the latest value of each history list."""
    history = history or {}
    values = {k: v[-1].value for k, v in history.items()} | kwargs.pop("values", {})
    base: dict[str, Any] = {"unit_uid": uid, "name": f"VEH_{uid}", "unit_type": "FMB920",
                            "last_report_utc": NOW}  # fmt: skip
    return UnitSnapshot(**(base | kwargs), values=values, history=history)

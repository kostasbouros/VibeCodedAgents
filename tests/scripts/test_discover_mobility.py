"""Tests for scripts/discover_mobility.py (no network: mock data and a fake client)."""

import importlib.util
from pathlib import Path
from typing import Any, Self

import pytest

from connectors.mobility import MobilityError, UnitLatestPosition
from mock_api.mobility import store
from shared.config import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "discover_mobility.py"
_spec = importlib.util.spec_from_file_location("discover_mobility", SCRIPT)
assert _spec and _spec.loader
discover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discover)

# A unit carrying the kinds of personal data the report must never show.
PERSONAL = {
    "Uid": "ZZ9999",
    "Name": "Jane Example private car",
    "PhoneNumber": "+306970000001",
    "LastReportedTimeUTC": "2099-01-01T00:00:00",
    "Position": {
        "Latitude": 37.123456,
        "Longitude": 23.654321,
        "Address": "12 Example Street, Home of Jane",
        "Driver": {"Uid": "D1", "FirstName": "Jane", "LastName": "Example", "Code": "JEX"},
        "PointOfInterest": {"Uid": "P1", "Name": "Home Example"},
        "InputOutputs": [
            {
                "SystemName": "aux1",
                "Description": "Auxiliary 1",
                "UserDescription": "Door open",
                "Active": True,
            }
        ],
    },
    "SensorReadings": [
        {
            "UnitUid": "ZZ9999",
            "Name": "Signal Strength",
            "Value": "4",
            "MeasurementSign": "",
            "SensorType": "GSM Signal Strength",
            "ReadingTimeLocal": "2099-01-01T02:00:00",
        }
    ],
}
FORBIDDEN = ["Jane", "Example Street", "Home Example", "37.123456", "23.654321", "+306970000001"]


def mock_units() -> list[UnitLatestPosition]:
    return [UnitLatestPosition.model_validate(u) for u in store.load("latest_positions")]


def test_picks_the_most_recent_units() -> None:
    units = mock_units()
    picked = discover.pick_units(units, 3)
    newest = sorted(units, key=lambda u: u.last_reported_time_utc, reverse=True)[:3]
    assert [u.uid for u in picked] == [u.uid for u in newest]


def test_summary_counts_units_per_field() -> None:
    sensors, inputs = discover.summarize(mock_units())
    assert len(sensors[("External Battery", "External Battery", "v")].units) == 20
    assert len(sensors[("Signal Strength", "GSM Signal Strength", "")].units) == 19  # deep sleep
    jam = inputs[("gsmjammed", "GSM Jammed", "")]
    assert (len(jam.units), len(jam.active_units)) == (20, 1)


def test_report_never_contains_personal_data() -> None:
    units = [*mock_units(), UnitLatestPosition.model_validate(PERSONAL)]
    sensors, inputs = discover.summarize(units)
    report = discover.render(sensors, inputs, unit_count=len(units), source="test",
                             generated=store.reference_time())  # fmt: skip
    for text in FORBIDDEN:
        assert text not in report, text
    assert not any(f"VEH_{n:03d}" in report for n in range(1, 21))  # no unit names
    assert "| aux1 | Auxiliary 1 | Door open | 1 | 1 |" in report
    assert "| gsmjammed | GSM Jammed | - | 20 | 1 |" in report  # empty cells show "-"


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.result = result

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def latest_positions(self) -> list[UnitLatestPosition]:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_main_prints_and_saves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(discover, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(discover, "get_mobility_client", lambda: FakeClient(mock_units()))
    out = tmp_path / "fields.md"
    assert discover.main(["--limit", "5", "--output", str(out)]) == 0
    saved = out.read_text(encoding="utf-8")
    assert "5 most recently reporting units" in saved
    assert "the MOCK API" in capsys.readouterr().out


def test_main_reports_api_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(discover, "get_settings", lambda: Settings(_env_file=None))
    error = MobilityError("1001", "Wrong user name or password")
    monkeypatch.setattr(discover, "get_mobility_client", lambda: FakeClient(error))
    assert discover.main(["--output", str(tmp_path / "x.md")]) == 1
    assert "Wrong user name or password" in capsys.readouterr().err
    assert not (tmp_path / "x.md").exists()

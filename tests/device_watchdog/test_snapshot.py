"""Tests for agents/device_watchdog/snapshot.py."""

from datetime import timedelta
from pathlib import Path

from agents.device_watchdog.snapshot import (
    build_snapshots,
    carrier_hint,
    load_cache,
    load_field_map,
    merge_snapshots,
    save_cache,
    with_api_alerts,
)
from tests.device_watchdog.conftest import FakeMobility, readings, snap

FORBIDDEN = ("latitude", "longitude", "address", "driver", "phone", "37.8", "23.7", "+30")


def _snapshots(mobility: FakeMobility) -> dict:
    snaps = build_snapshots(mobility.latest_positions(), mobility.list_units(), load_field_map())
    return {s.unit_uid: s for s in snaps}


def test_snapshot_fields_from_mock_data(mobility: FakeMobility) -> None:
    s = _snapshots(mobility)["CF5F16"]  # VEH_001
    assert (s.name, s.unit_type, s.company_uid, s.carrier_hint) == (
        "VEH_001",
        "FMB920",
        "DD60D9",
        "30697",
    )
    assert s.ignition == "off" and s.odometer == 63251.46
    assert s.values["external_voltage_v"] == 13.04 and s.values["gsm_level"] == 4.0
    assert s.values["gsm_jammed_input"] is False
    assert "sleep_mode" not in s.values  # not provided by Mobility


def test_snapshot_has_no_location_or_personal_data(mobility: FakeMobility) -> None:
    for s in _snapshots(mobility).values():
        dumped = s.model_dump_json().lower()
        for word in FORBIDDEN:
            assert word not in dumped, (s.unit_uid, word)


def test_missing_sensor_is_left_out(mobility: FakeMobility) -> None:
    deep_sleep = next(s for s in _snapshots(mobility).values() if s.name == "VEH_017")
    assert "gsm_level" not in deep_sleep.values


def test_carrier_hint() -> None:
    assert carrier_hint("+30 697-049 9914") == "30697"
    assert carrier_hint("") is None
    assert carrier_hint(None) is None
    assert carrier_hint("+3069") is None  # fewer than 5 digits


def test_merge_keeps_silent_units_and_builds_history() -> None:
    old = {"A": snap("A", history=readings("gsm_level", 4, 3)), "B": snap("B")}
    later = old["A"].history["gsm_level"][-1].time_utc + timedelta(minutes=5)
    fresh = [snap("A", history=readings("gsm_level", 1, start=later))]
    merged = merge_snapshots(old, fresh, history_len=2)
    assert set(merged) == {"A", "B"}
    assert [r.value for r in merged["A"].history["gsm_level"]] == [3, 1]


def test_merge_ignores_repeated_readings() -> None:
    old = {"A": snap("A", history=readings("gsm_level", 2))}
    merged = merge_snapshots(old, [old["A"]], history_len=3)
    assert len(merged["A"].history["gsm_level"]) == 1


def test_api_alerts_matched_by_unit_name(mobility: FakeMobility) -> None:
    snaps = with_api_alerts(_snapshots(mobility), mobility.alerts()[0])
    veh9 = next(s for s in snaps.values() if s.name == "VEH_009")
    assert {a.alert_type for a in veh9.api_alerts} == {"Jamming"}


def test_cache_round_trip_without_api_alerts(tmp_path: Path, mobility: FakeMobility) -> None:
    snaps = with_api_alerts(_snapshots(mobility), mobility.alerts()[0])
    save_cache(snaps, tmp_path / "c.json")
    loaded = load_cache(tmp_path / "c.json")
    assert set(loaded) == set(snaps)
    assert all(s.api_alerts == [] for s in loaded.values())
    assert loaded["CF5F16"].values == snaps["CF5F16"].values

"""Tests for agents/device_watchdog/rules.py and config/rules.yaml (one section per rule)."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agents.device_watchdog.rules import (
    INTERVALS_FILE,
    RULES_FILE,
    Rule,
    evaluate,
    evaluate_all,
    history_needed,
    load_rules,
)
from agents.device_watchdog.snapshot import ApiAlert
from tests.device_watchdog.conftest import NOW, readings, snap

ROOT = Path(__file__).resolve().parents[2]
MARK = "# EXAMPLE VALUE - operations to confirm"
THRESHOLD_KEYS = (
    "interval_multiplier", "definite_after_hours", "max_external_voltage_v", "max_level",
    "consecutive_readings", "min_satellites", "min_duration_seconds", "alert_lookback_hours",
    "max_voltage_v", "value", "parked_seconds", "moving_seconds",
)  # fmt: skip


@pytest.fixture(scope="module")
def rules() -> dict[str, Rule]:
    return {r.rule_id: r for r in load_rules()}


# --- The YAML files -------------------------------------------------------------------------


def test_all_example_rules_load(rules: dict[str, Rule]) -> None:
    assert set(rules) == {"OFFLINE_01", "POWER_01", "GSM_01", "GPS_01", "JAM_01", "BATT_01"}
    assert not rules["BATT_01"].enabled
    assert rules["JAM_01"].escalate


@pytest.mark.parametrize("path", [RULES_FILE, INTERVALS_FILE])
def test_every_threshold_is_marked_as_example(path: Path) -> None:
    pattern = re.compile(rf"^\s*-?\s*({'|'.join(THRESHOLD_KEYS)}):")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if pattern.match(ln)]
    assert lines, "no thresholds found"
    assert [ln for ln in lines if MARK not in ln] == []


def test_each_rule_names_an_existing_test_file(rules: dict[str, Rule]) -> None:
    for rule in rules.values():
        assert (ROOT / rule.test_file).is_file(), rule.rule_id


def test_rules_py_has_no_numeric_thresholds() -> None:
    code = (ROOT / "agents" / "device_watchdog" / "rules.py").read_text(encoding="utf-8")
    for number in ("3600", "1.0", " 24", " 60", " 4"):
        assert not re.search(rf"[<>=]=?\s*{re.escape(number.strip())}\b", code), number


def _write_rules(tmp_path: Path, change) -> Path:
    data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    change(data["rules"])
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_unknown_input_is_refused(tmp_path: Path) -> None:
    path = _write_rules(tmp_path, lambda rs: rs[2]["inputs"].append("no_such_field"))
    with pytest.raises(ValueError, match="not in the field map"):
        load_rules(path)


def test_battery_rule_cannot_be_enabled_without_threshold(tmp_path: Path) -> None:
    path = _write_rules(tmp_path, lambda rs: rs[5].update(enabled=True))
    with pytest.raises(ValidationError, match="max_voltage_v"):
        load_rules(path)


def test_misspelled_parameter_is_refused(tmp_path: Path) -> None:
    path = _write_rules(tmp_path, lambda rs: rs[3]["condition"].update(min_satelites=4))
    with pytest.raises(ValidationError):
        load_rules(path)


def test_history_needed_follows_gsm_rule(rules: dict[str, Rule]) -> None:
    assert history_needed(rules.values()) == rules["GSM_01"].condition.consecutive_readings


# --- General behaviour ----------------------------------------------------------------------


def test_disabled_rule_never_fires(rules: dict[str, Rule]) -> None:
    rule = rules["GPS_01"].model_copy(update={"enabled": False})
    assert evaluate(rule, snap(values={"satellites": 0, "ignition": "on"}), NOW) is None


def test_applies_to_filters_unit_types(rules: dict[str, Rule]) -> None:
    rule = rules["GPS_01"].model_copy(update={"applies_to": ["FMC130"]})
    s = snap(values={"satellites": 0, "ignition": "on"})
    assert evaluate(rule, s, NOW) is None
    assert evaluate(rule, s.model_copy(update={"unit_type": "FMC130"}), NOW) is not None


def test_naive_now_is_refused(rules: dict[str, Rule]) -> None:
    with pytest.raises(ValueError, match="time-zone"):
        evaluate(rules["GPS_01"], snap(), NOW.replace(tzinfo=None))


def test_missing_input_never_fires(rules: dict[str, Rule]) -> None:
    for rule in rules.values():
        if rule.rule_id != "OFFLINE_01":  # OFFLINE_01 only needs the report time
            assert evaluate(rule, snap(), NOW) is None, rule.rule_id


# --- OFFLINE_01 -----------------------------------------------------------------------------


def _ago(hours: float, **kw) -> object:
    return snap(last_report_utc=NOW - timedelta(hours=hours), **kw)


def test_offline_not_triggered_within_interval(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["OFFLINE_01"], _ago(2, values={"ignition": "off"}), NOW) is None


def test_offline_possible_after_three_intervals(rules: dict[str, Rule]) -> None:
    hit = evaluate(rules["OFFLINE_01"], _ago(3.5, values={"ignition": "off"}), NOW)
    assert hit is not None and hit.severity == "warning" and hit.evidence["definite"] is False
    assert hit.evidence["interval_source"] == "FMB920"


def test_offline_definite_after_24h(rules: dict[str, Rule]) -> None:
    hit = evaluate(rules["OFFLINE_01"], _ago(25, values={"ignition": "off"}), NOW)
    assert hit is not None and hit.severity == "critical" and hit.evidence["definite"] is True


def test_offline_uses_moving_interval_when_ignition_on(rules: dict[str, Rule]) -> None:
    hit = evaluate(rules["OFFLINE_01"], _ago(0.5, values={"ignition": "On"}), NOW)
    assert hit is not None and hit.evidence["state"] == "moving"


def test_offline_unknown_unit_type_uses_default(rules: dict[str, Rule]) -> None:
    hit = evaluate(rules["OFFLINE_01"], _ago(4, unit_type="XYZ", values={"ignition": "off"}), NOW)
    assert hit is not None and hit.evidence["interval_source"] == "default"


def test_offline_skipped_in_sleep_mode(rules: dict[str, Rule]) -> None:
    sleeping = _ago(30, values={"ignition": "off", "sleep_mode": 1})
    awake = _ago(30, values={"ignition": "off", "sleep_mode": 0})
    assert evaluate(rules["OFFLINE_01"], sleeping, NOW) is None
    assert evaluate(rules["OFFLINE_01"], awake, NOW) is not None


# --- POWER_01 -------------------------------------------------------------------------------


def test_power_loss_with_battery_present(rules: dict[str, Rule]) -> None:
    s = snap(values={"external_voltage_v": 0.2, "internal_battery_pct": 90})
    hit = evaluate(rules["POWER_01"], s, NOW)
    assert hit is not None and hit.escalate and hit.evidence["external_voltage_v"] == 0.2


def test_power_loss_needs_battery_reading(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["POWER_01"], snap(values={"external_voltage_v": 0.0}), NOW) is None


def test_power_normal_voltage(rules: dict[str, Rule]) -> None:
    s = snap(values={"external_voltage_v": 12.6, "battery_voltage_v": 4.1})
    assert evaluate(rules["POWER_01"], s, NOW) is None


# --- GSM_01 ---------------------------------------------------------------------------------


def test_gsm_low_on_three_consecutive_readings(rules: dict[str, Rule]) -> None:
    hit = evaluate(rules["GSM_01"], snap(history=readings("gsm_level", 4, 2, 1, 2)), NOW)
    assert hit is not None and hit.evidence["gsm_levels"] == "2,1,2"


def test_gsm_needs_enough_readings(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["GSM_01"], snap(history=readings("gsm_level", 1, 1)), NOW) is None


def test_gsm_one_good_reading_breaks_the_run(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["GSM_01"], snap(history=readings("gsm_level", 1, 3, 1)), NOW) is None


# --- GPS_01 ---------------------------------------------------------------------------------


def test_gps_low_satellites_with_ignition_on(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["GPS_01"], snap(values={"satellites": 3, "ignition": "on"}), NOW)


def test_gps_ignored_with_ignition_off(rules: dict[str, Rule]) -> None:
    s = snap(values={"satellites": 0, "ignition": "off"})
    assert evaluate(rules["GPS_01"], s, NOW) is None


def test_gps_enough_satellites(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["GPS_01"], snap(values={"satellites": 4, "ignition": "on"}), NOW) is None


# --- JAM_01 ---------------------------------------------------------------------------------


def test_jamming_state_held_over_60s(rules: dict[str, Rule]) -> None:
    # readings at now-130 s (0), now-70 s (2), now-10 s (2): jamming held for 70 s
    s = snap(history=readings("jamming", 0, 2, 2, start=NOW - timedelta(seconds=10), step_s=60))
    hit = evaluate(rules["JAM_01"], s, NOW)
    assert hit is not None and hit.escalate and hit.evidence["jamming_seconds"] == 70


def test_jamming_state_short_is_ignored(rules: dict[str, Rule]) -> None:
    s = snap(history=readings("jamming", 0, 2, start=NOW - timedelta(seconds=30)))
    assert evaluate(rules["JAM_01"], s, NOW) is None


def test_jamming_ended_is_ignored(rules: dict[str, Rule]) -> None:
    s = snap(history=readings("jamming", 2, 2, 0, start=NOW, step_s=120))
    assert evaluate(rules["JAM_01"], s, NOW) is None


def test_jamming_alert_from_api_always_escalates(rules: dict[str, Rule]) -> None:
    s = snap(api_alerts=[ApiAlert(alert_type="Jamming", created_utc=NOW - timedelta(hours=1))])
    hit = evaluate(rules["JAM_01"], s, NOW)
    assert hit is not None and hit.escalate and hit.evidence["api_jamming_alerts"] == 1


def test_old_or_other_api_alerts_are_ignored(rules: dict[str, Rule]) -> None:
    s = snap(api_alerts=[ApiAlert(alert_type="Jamming", created_utc=NOW - timedelta(days=3)),
                         ApiAlert(alert_type="Panic", created_utc=NOW)])  # fmt: skip
    assert evaluate(rules["JAM_01"], s, NOW) is None


# --- BATT_01 --------------------------------------------------------------------------------


def test_battery_rule_disabled_by_default(rules: dict[str, Rule]) -> None:
    assert evaluate(rules["BATT_01"], snap(values={"battery_voltage_v": 0.1}), NOW) is None


def test_battery_rule_fires_once_operations_set_threshold(rules: dict[str, Rule]) -> None:
    rule = rules["BATT_01"]
    rule = rule.model_copy(
        update={
            "enabled": True,
            "condition": rule.condition.model_copy(update={"max_voltage_v": 3.5}),
        }
    )
    assert evaluate(rule, snap(values={"battery_voltage_v": 3.2}), NOW) is not None
    assert evaluate(rule, snap(values={"battery_voltage_v": 3.9}), NOW) is None


# --- All rules together ---------------------------------------------------------------------


def test_evaluate_all_returns_every_hit(rules: dict[str, Rule]) -> None:
    s1 = snap("A", values={"satellites": 1, "ignition": "on"})
    s2 = snap("B", values={"external_voltage_v": 0.0, "internal_battery_pct": 50})
    hits = evaluate_all(rules.values(), [s1, s2], NOW)
    assert sorted((h.unit_uid, h.rule_id) for h in hits) == [("A", "GPS_01"), ("B", "POWER_01")]

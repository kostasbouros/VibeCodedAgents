"""Device health rules: config/rules.yaml loaded into Pydantic models, evaluated as pure code.

    rules = load_rules()
    hit = evaluate(rule, snapshot, now)   # RuleHit or None

No thresholds live here: every number comes from config/rules.yaml or
config/report_intervals.yaml. evaluate() has no side effects and reads no clock or file, so
the same snapshot and `now` always give the same answer.

A rule never fires on missing data: if an input it needs has no reading, it returns None.
"""

import operator
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import structlog
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from agents.device_watchdog.snapshot import FIELD_MAP_FILE, FieldMap, UnitSnapshot, load_field_map
from shared.state import PROJECT_ROOT

log = structlog.get_logger(__name__)

RULES_FILE = PROJECT_ROOT / "config" / "rules.yaml"
INTERVALS_FILE = PROJECT_ROOT / "config" / "report_intervals.yaml"

Severity = Literal["info", "warning", "critical"]
Evidence = dict[str, float | int | str | bool | None]
_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "gt": operator.gt, "ge": operator.ge, "lt": operator.lt,
    "le": operator.le, "eq": operator.eq, "ne": operator.ne,
}  # fmt: skip


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # a typo'd name is an error


# --- Report intervals -----------------------------------------------------------------------


class IntervalPair(_Strict):
    parked_seconds: PositiveFloat
    moving_seconds: PositiveFloat


class ReportIntervals(_Strict):
    default: IntervalPair
    unit_types: dict[str, IntervalPair] = {}

    def for_unit(self, unit_type: str | None) -> tuple[IntervalPair, str]:
        """(intervals, where they came from: the unit type or "default")."""
        if unit_type and unit_type in self.unit_types:
            return self.unit_types[unit_type], unit_type
        return self.default, "default"


# --- Conditions (one model per check) --------------------------------------------------------


class OfflineCondition(_Strict):
    type: Literal["offline"]
    interval_multiplier: PositiveFloat
    definite_after_hours: PositiveFloat
    definite_severity: Severity
    moving_ignition_values: list[str] = Field(min_length=1)
    intervals: ReportIntervals | None = None  # filled from report_intervals.yaml by load_rules


class PowerLossCondition(_Strict):
    type: Literal["power_loss"]
    max_external_voltage_v: float
    battery_present_inputs: list[str] = Field(min_length=1)


class LowGsmCondition(_Strict):
    type: Literal["low_gsm"]
    max_level: float
    consecutive_readings: PositiveInt


class LowGpsCondition(_Strict):
    type: Literal["low_gps"]
    min_satellites: float
    ignition_on_values: list[str] = Field(min_length=1)


class JammingCondition(_Strict):
    type: Literal["jamming"]
    jamming_value: float
    min_duration_seconds: NonNegativeFloat
    alert_types: list[str]
    alert_lookback_hours: PositiveFloat


class LowBatteryCondition(_Strict):
    type: Literal["low_battery"]
    max_voltage_v: float | None  # None until operations set it


Condition = Annotated[
    OfflineCondition | PowerLossCondition | LowGsmCondition | LowGpsCondition
    | JammingCondition | LowBatteryCondition,
    Field(discriminator="type"),
]  # fmt: skip

# Inputs each check reads; a rule must list them in `inputs`.
_NEEDS: dict[str, set[str]] = {
    "offline": {"last_report_time_utc", "ignition"},
    "power_loss": {"external_voltage_v"},
    "low_gsm": {"gsm_level"},
    "low_gps": {"satellites", "ignition"},
    "jamming": {"jamming", "api_alert_type"},
    "low_battery": {"battery_voltage_v"},
}


# --- Rule -----------------------------------------------------------------------------------


class RuleException(_Strict):
    input: str
    op: Literal["gt", "ge", "lt", "le", "eq", "ne"]
    value: float | str | bool
    reason: str


class Rule(_Strict):
    rule_id: str = Field(pattern=r"^[A-Z]+_\d{2}$")
    version: PositiveInt
    enabled: bool
    applies_to: Literal["*"] | list[str]
    inputs: list[str] = Field(min_length=1)
    condition: Condition
    severity: Severity
    escalate: bool = False
    rationale: str = Field(min_length=1)
    exceptions: list[RuleException] = []
    owner: str = Field(min_length=1)
    test_file: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        used = set(_NEEDS[self.condition.type]) | {e.input for e in self.exceptions}
        if isinstance(self.condition, PowerLossCondition):
            used |= set(self.condition.battery_present_inputs)
        if missing := used - set(self.inputs):
            raise ValueError(f"{self.rule_id}: inputs must include {sorted(missing)}")
        cond = self.condition
        if self.enabled and isinstance(cond, LowBatteryCondition) and cond.max_voltage_v is None:
            raise ValueError(f"{self.rule_id}: cannot be enabled before max_voltage_v is set")
        return self


class RuleSet(_Strict):
    rules: list[Rule]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [r.rule_id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule_id values must be unique")
        return self


class RuleHit(_Strict):
    rule_id: str
    rule_version: int
    unit_uid: str
    severity: Severity
    escalate: bool
    summary: str
    evidence: Evidence


# --- Loading --------------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _check_inputs(rules: Iterable[Rule], field_map: FieldMap) -> None:
    for rule in rules:
        if unknown := set(rule.inputs) - set(field_map.fields):
            raise ValueError(f"{rule.rule_id}: inputs not in the field map: {sorted(unknown)}")
        missing = [i for i in rule.inputs if not field_map.fields[i].available]
        if rule.enabled and missing:
            log.warning("rule_inputs_unavailable", rule_id=rule.rule_id, inputs=missing)


def load_rules(
    rules_path: Path = RULES_FILE,
    intervals_path: Path = INTERVALS_FILE,
    field_map_path: Path = FIELD_MAP_FILE,
) -> list[Rule]:
    """All rules (enabled or not), checked. Raises on any problem in the files."""
    rule_set = RuleSet.model_validate(_read_yaml(rules_path))
    intervals = ReportIntervals.model_validate(_read_yaml(intervals_path))
    _check_inputs(rule_set.rules, load_field_map(field_map_path))
    return [
        r.model_copy(update={"condition": r.condition.model_copy(update={"intervals": intervals})})
        if isinstance(r.condition, OfflineCondition)
        else r
        for r in rule_set.rules
    ]


def history_needed(rules: Iterable[Rule]) -> int:
    """How many readings per input the snapshot cache must keep for these rules."""
    counts = [r.condition.consecutive_readings for r in rules
              if isinstance(r.condition, LowGsmCondition)]  # fmt: skip
    return max(counts, default=1)


# --- Evaluation -----------------------------------------------------------------------------


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _in(value: Any, choices: list[str]) -> bool:
    return value is not None and str(value).strip().lower() in {c.lower() for c in choices}


def _excepted(rule: Rule, snap: UnitSnapshot) -> bool:
    for exc in rule.exceptions:
        actual = snap.value(exc.input)
        if actual is None:
            continue
        if isinstance(exc.value, (int, float)) and not isinstance(exc.value, bool):
            actual = _number(actual)
            if actual is None:
                continue
        if _OPS[exc.op](actual, exc.value):
            return True
    return False


def _hit(rule: Rule, snap: UnitSnapshot, summary: str, evidence: Evidence,
         severity: Severity | None = None) -> RuleHit:  # fmt: skip
    return RuleHit(
        rule_id=rule.rule_id, rule_version=rule.version, unit_uid=snap.unit_uid,
        severity=severity or rule.severity, escalate=rule.escalate,
        summary=summary, evidence=evidence,
    )  # fmt: skip


def _offline(rule: Rule, c: OfflineCondition, snap: UnitSnapshot, now: datetime) -> RuleHit | None:
    assert c.intervals is not None, "load_rules() fills in the report intervals"
    pair, source = c.intervals.for_unit(snap.unit_type)
    moving = _in(snap.value("ignition"), c.moving_ignition_values)
    expected = pair.moving_seconds if moving else pair.parked_seconds
    evidence: Evidence = {
        "state": "moving" if moving else "parked",
        "expected_interval_seconds": expected,
        "interval_source": source,
    }
    if snap.last_report_utc is None:
        return _hit(rule, snap, "No report time known.", evidence | {"definite": True},
                    c.definite_severity)  # fmt: skip
    silent = (now - snap.last_report_utc).total_seconds()
    evidence |= {"silent_hours": round(silent / 3600, 2)}
    if silent > c.definite_after_hours * 3600:
        return _hit(rule, snap, f"No report for {silent / 3600:.1f} h: offline.",
                    evidence | {"definite": True}, c.definite_severity)  # fmt: skip
    if silent > c.interval_multiplier * expected:
        return _hit(rule, snap, f"No report for {silent / 3600:.1f} h: possibly offline.",
                    evidence | {"definite": False})  # fmt: skip
    return None


def _power(rule: Rule, c: PowerLossCondition, snap: UnitSnapshot, now: datetime) -> RuleHit | None:
    ext = _number(snap.value("external_voltage_v"))
    battery = [i for i in c.battery_present_inputs if snap.value(i) is not None]
    if ext is None or ext >= c.max_external_voltage_v or not battery:
        return None
    evidence: Evidence = {"external_voltage_v": ext} | {i: snap.value(i) for i in battery}  # type: ignore[misc]
    return _hit(rule, snap, f"External power lost ({ext} V), running on battery.", evidence)


def _gsm(rule: Rule, c: LowGsmCondition, snap: UnitSnapshot, now: datetime) -> RuleHit | None:
    levels = [_number(r.value) for r in snap.history.get("gsm_level", [])][
        -c.consecutive_readings :
    ]
    if len(levels) < c.consecutive_readings or any(v is None or v > c.max_level for v in levels):
        return None
    summary = f"Weak GSM signal on the last {len(levels)} readings."
    return _hit(rule, snap, summary, {"gsm_levels": ",".join(f"{v:g}" for v in levels)})  # type: ignore[union-attr]


def _gps(rule: Rule, c: LowGpsCondition, snap: UnitSnapshot, now: datetime) -> RuleHit | None:
    sats = _number(snap.value("satellites"))
    if (
        sats is None
        or sats >= c.min_satellites
        or not _in(snap.value("ignition"), c.ignition_on_values)
    ):
        return None
    return _hit(rule, snap, f"Only {sats:g} satellites with ignition on.",
                {"satellites": sats, "ignition": str(snap.value("ignition"))})  # fmt: skip


def _jamming_seconds(c: JammingCondition, snap: UnitSnapshot, now: datetime) -> float | None:
    """How long the jamming value has been held, up to now (None if not jamming now)."""
    start = None
    for reading in reversed(snap.history.get("jamming", [])):
        if _number(reading.value) != c.jamming_value or reading.time_utc is None:
            break
        start = reading.time_utc
    return (now - start).total_seconds() if start else None


def _jamming(rule: Rule, c: JammingCondition, snap: UnitSnapshot, now: datetime) -> RuleHit | None:
    held = _jamming_seconds(c, snap, now)
    since = now - timedelta(hours=c.alert_lookback_hours)
    alerts = [a for a in snap.api_alerts if a.alert_type in c.alert_types
              and a.created_utc is not None and since <= a.created_utc <= now]  # fmt: skip
    by_state = held is not None and held > c.min_duration_seconds
    if not by_state and not alerts:
        return None
    reason = "jamming reported by the device" if by_state else "Jamming alert from Mobility"
    return _hit(rule, snap, f"Possible GPS/GSM jamming ({reason}).",
                {"jamming_seconds": held, "api_jamming_alerts": len(alerts)})  # fmt: skip


def _battery(
    rule: Rule, c: LowBatteryCondition, snap: UnitSnapshot, now: datetime
) -> RuleHit | None:
    volts = _number(snap.value("battery_voltage_v"))
    if c.max_voltage_v is None or volts is None or volts >= c.max_voltage_v:
        return None
    return _hit(rule, snap, f"Battery voltage low ({volts} V).", {"battery_voltage_v": volts})


_CHECKS: dict[str, Callable[[Rule, Any, UnitSnapshot, datetime], RuleHit | None]] = {
    "offline": _offline, "power_loss": _power, "low_gsm": _gsm,
    "low_gps": _gps, "jamming": _jamming, "low_battery": _battery,
}  # fmt: skip


def evaluate(rule: Rule, unit_snapshot: UnitSnapshot, now: datetime) -> RuleHit | None:
    """RuleHit if the rule fires for this unit at `now` (time-zone aware), else None."""
    if now.tzinfo is None:
        raise ValueError("now must be time-zone aware (UTC)")
    if not rule.enabled:
        return None
    if rule.applies_to != "*" and unit_snapshot.unit_type not in rule.applies_to:
        return None
    if _excepted(rule, unit_snapshot):
        return None
    return _CHECKS[rule.condition.type](rule, rule.condition, unit_snapshot, now)


def evaluate_all(
    rules: Iterable[Rule], snapshots: Iterable[UnitSnapshot], now: datetime
) -> list[RuleHit]:
    rules = list(rules)
    return [h for s in snapshots for r in rules if (h := evaluate(r, s, now)) is not None]

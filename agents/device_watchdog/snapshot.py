"""One UnitSnapshot per unit: the only device data the watchdog rules ever see.

Built from MobilityClient.latest_positions() (+ list_units() for unit type, company and phone)
using config/mobility_field_map.yaml. Personal or location data is never copied in: no
latitude, longitude, address, driver or phone number. Only the first 5 digits of the phone
number are kept, as carrier_hint, to group units on the same mobile network.

Snapshots are cached between runs (data/device_watchdog/snapshots.json) because
latest_positions(since=...) only returns units that reported since the last run: a silent
unit must still be checked. The cache also keeps the last few readings of each input, for
rules that need several readings in a row (GSM_01).
"""

import json
import os
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from connectors.mobility import Alert, Unit, UnitLatestPosition
from shared.state import PROJECT_ROOT

FIELD_MAP_FILE = PROJECT_ROOT / "config" / "mobility_field_map.yaml"
CACHE_FILE = PROJECT_ROOT / "data" / "device_watchdog" / "snapshots.json"
CARRIER_HINT_DIGITS = 5  # "+30 697..." -> "30697": country code + mobile prefix

Value = float | bool | str


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Field map ------------------------------------------------------------------------------


class FieldSpec(_Strict):
    source: str | None
    kind: Literal["unit", "latest", "position", "sensor", "io", "alert", "none"]
    key: str | None
    unit: str
    available: bool

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.kind == "none") == self.available:
            raise ValueError("kind 'none' means available: false, and only then")
        if self.kind != "none" and not self.key:
            raise ValueError(f"kind {self.kind!r} needs a key")
        return self


class FieldMap(_Strict):
    fields: dict[str, FieldSpec]


def load_field_map(path: Path = FIELD_MAP_FILE) -> FieldMap:
    with path.open(encoding="utf-8") as f:
        return FieldMap.model_validate(yaml.safe_load(f))


# --- Snapshot model -------------------------------------------------------------------------


class Reading(_Strict):
    time_utc: datetime | None
    value: Value


class ApiAlert(_Strict):
    alert_type: str
    created_utc: datetime | None


class UnitSnapshot(_Strict):
    unit_uid: str
    name: str | None = None
    unit_type: str | None = None
    company_uid: str | None = None
    carrier_hint: str | None = None
    last_report_utc: datetime | None = None
    ignition: str | None = None
    odometer: float | None = None
    values: dict[str, Value] = {}  # latest value per field-map name
    history: dict[str, list[Reading]] = {}  # recent readings per name, oldest first
    api_alerts: list[ApiAlert] = []

    def value(self, name: str) -> Value | datetime | None:
        """Latest value of a field-map input, or None when there is no reading."""
        if name == "last_report_time_utc":
            return self.last_report_utc
        if name == "unit_type":
            return self.unit_type
        return self.values.get(name)


# --- Building -------------------------------------------------------------------------------


def carrier_hint(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return digits[:CARRIER_HINT_DIGITS] if len(digits) >= CARRIER_HINT_DIGITS else None


def _as_number(text: str | None) -> float | None:
    try:
        return float(text) if text not in (None, "") else None
    except ValueError:
        return None


def _read(spec: FieldSpec, pos: UnitLatestPosition, unit: Unit) -> Reading | None:
    """One input's value and time from a latest-position row, per its field-map entry."""
    when = pos.last_reported_time_utc
    value: Any = None
    if spec.kind == "unit":
        value = getattr(unit, spec.key)  # type: ignore[arg-type]
    elif spec.kind == "latest":
        value = getattr(pos, spec.key)  # type: ignore[arg-type]
    elif spec.kind == "position" and pos.position is not None:
        value = getattr(pos.position, spec.key)  # type: ignore[arg-type]
    elif spec.kind == "sensor":
        hit = next((s for s in pos.sensor_readings if s.name == spec.key), None)
        if hit is not None:
            value, when = _as_number(hit.value), hit.reading_time_utc or when
    elif spec.kind == "io" and pos.position is not None:
        hit = next((io for io in pos.position.input_outputs if io.system_name == spec.key), None)
        value = hit.active if hit is not None else None
    if value is None or isinstance(value, datetime):  # times are kept as top-level fields
        return None
    return Reading(time_utc=when, value=value)


def build_snapshot(pos: UnitLatestPosition, unit: Unit | None, field_map: FieldMap) -> UnitSnapshot:
    unit = unit or pos  # UnitLatestPosition is also a Unit (fields may be empty)
    readings = {
        name: reading
        for name, spec in field_map.fields.items()
        if spec.kind not in ("none", "alert") and (reading := _read(spec, pos, unit)) is not None
    }
    position = pos.position
    return UnitSnapshot(
        unit_uid=pos.uid or "",
        name=pos.name or unit.name,
        unit_type=unit.unit_type,
        company_uid=unit.company_uid,
        carrier_hint=carrier_hint(unit.phone_number),
        last_report_utc=pos.last_reported_time_utc,
        ignition=position.ignition if position else None,
        odometer=position.odometer if position else None,
        values={name: r.value for name, r in readings.items()},
        history={name: [r] for name, r in readings.items()},
    )


def build_snapshots(
    positions: Iterable[UnitLatestPosition], units: Iterable[Unit], field_map: FieldMap
) -> list[UnitSnapshot]:
    by_uid = {u.uid: u for u in units if u.uid}
    return [build_snapshot(p, by_uid.get(p.uid), field_map) for p in positions if p.uid]


# --- Merging with earlier runs ----------------------------------------------------------------


def _merge_history(old: list[Reading], new: list[Reading], keep: int) -> list[Reading]:
    last = old[-1].time_utc if old else None
    fresh = [r for r in new if last is None or (r.time_utc is not None and r.time_utc > last)]
    return (old + fresh)[-keep:]


def merge_snapshots(
    previous: dict[str, UnitSnapshot], fresh: Sequence[UnitSnapshot], history_len: int
) -> dict[str, UnitSnapshot]:
    """Fresh snapshots replace earlier ones; units that did not report keep their last one.
    Each input keeps its last `history_len` readings (at least 1)."""
    keep = max(1, history_len)
    merged = dict(previous)
    for snap in fresh:
        old = previous.get(snap.unit_uid)
        names = set(snap.history) | (set(old.history) if old else set())
        history = {
            n: _merge_history(old.history.get(n, []) if old else [], snap.history.get(n, []), keep)
            for n in names
        }
        merged[snap.unit_uid] = snap.model_copy(update={"history": history})
    return merged


def with_api_alerts(
    snapshots: dict[str, UnitSnapshot], alerts: Iterable[Alert]
) -> dict[str, UnitSnapshot]:
    """Attach Mobility alerts (matched by unit name, the API's "Vehicle") to each snapshot."""
    by_name: dict[str, list[ApiAlert]] = {}
    for a in alerts:
        if a.vehicle and a.alert_type:
            by_name.setdefault(a.vehicle, []).append(
                ApiAlert(alert_type=a.alert_type, created_utc=a.created_date)
            )
    return {
        uid: s.model_copy(update={"api_alerts": by_name.get(s.name or "", [])})
        for uid, s in snapshots.items()
    }


# --- Cache file -------------------------------------------------------------------------------


def load_cache(path: Path = CACHE_FILE) -> dict[str, UnitSnapshot]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {uid: UnitSnapshot.model_validate(s) for uid, s in raw.items()}


def save_cache(snapshots: dict[str, UnitSnapshot], path: Path = CACHE_FILE) -> None:
    """Atomic write. API alerts are not cached: they are fetched fresh every run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {uid: s.model_dump(mode="json", exclude={"api_alerts"}) for uid, s in snapshots.items()}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)

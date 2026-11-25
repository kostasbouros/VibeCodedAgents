"""Discover which sensor names and inputs/outputs your Mobility account reports (read-only).

    python scripts/discover_mobility.py            # uses USE_MOCKS from your settings
    python scripts/discover_mobility.py --limit 5

Reads the latest data of at most N units (the ones that reported most recently) and lists:
- every distinct sensor reading (Name / SensorType / MeasurementSign), with one example value,
  the newest reading time seen and how many units report it;
- every distinct input/output (SystemName / Description / UserDescription), with how many
  units report it and how many have it active right now.

Prints a table and saves it to reports/discovery/mobility_fields.md (not saved to git).
It never prints coordinates, addresses, driver names, phone numbers or unit names.
Operations then use the result to fill in config/mobility_field_map.yaml.
"""

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from connectors.mobility import MobilityError, UnitLatestPosition, get_mobility_client
from shared.config import get_settings

DEFAULT_LIMIT = 10
OUTPUT = Path("reports/discovery/mobility_fields.md")


@dataclass
class SensorSummary:
    units: set[str] = field(default_factory=set)
    example: str = ""
    newest: datetime | None = None


@dataclass
class InputSummary:
    units: set[str] = field(default_factory=set)
    active_units: set[str] = field(default_factory=set)


def pick_units(latest: list[UnitLatestPosition], limit: int) -> list[UnitLatestPosition]:
    """The `limit` units that reported most recently."""
    oldest = datetime.min.replace(tzinfo=UTC)
    ordered = sorted(latest, key=lambda u: u.last_reported_time_utc or oldest, reverse=True)
    return ordered[:limit]


def summarize(
    units: Iterable[UnitLatestPosition],
) -> tuple[dict[tuple[str, str, str], SensorSummary], dict[tuple[str, str, str], InputSummary]]:
    """Group sensors and inputs/outputs by their names. Uses unit Uids only for counting."""
    sensors: dict[tuple[str, str, str], SensorSummary] = {}
    inputs: dict[tuple[str, str, str], InputSummary] = {}
    for unit in units:
        uid = unit.uid or ""
        for r in unit.sensor_readings:
            key = (r.name or "", r.sensor_type or "", r.measurement_sign or "")
            s = sensors.setdefault(key, SensorSummary())
            s.units.add(uid)
            s.example = s.example or (r.value or "")
            if r.reading_time_local and (s.newest is None or r.reading_time_local > s.newest):
                s.newest = r.reading_time_local
        for io in unit.position.input_outputs if unit.position else []:
            key = (io.system_name or "", io.description or "", io.user_description or "")
            i = inputs.setdefault(key, InputSummary())
            i.units.add(uid)
            if io.active:
                i.active_units.add(uid)
    return sensors, inputs


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip() or "-"


def render(
    sensors: dict[tuple[str, str, str], SensorSummary],
    inputs: dict[tuple[str, str, str], InputSummary],
    *,
    unit_count: int,
    source: str,
    generated: datetime,
) -> str:
    lines = [
        "# Mobility fields on this account",
        "",
        (
            f"Generated {generated:%Y-%m-%d %H:%M} UTC from {source}, "
            f"looking at the {unit_count} most recently reporting units."
        ),
        "No coordinates, addresses, driver names, phone numbers or unit names are included.",
        "",
        f"## Sensor readings ({len(sensors)})",
        "",
        "| Name | SensorType | MeasurementSign | Example value | Newest reading (local) | Units |",
        "|---|---|---|---|---|---|",
    ]
    for (name, stype, sign), s in sorted(
        sensors.items(), key=lambda kv: (-len(kv[1].units), kv[0])
    ):
        newest = f"{s.newest:%Y-%m-%d %H:%M}" if s.newest else ""
        lines.append(
            f"| {_cell(name)} | {_cell(stype)} | {_cell(sign)} | {_cell(s.example)} "
            f"| {_cell(newest)} | {len(s.units)} |"
        )
    lines += [
        "",
        f"## Inputs/outputs ({len(inputs)})",
        "",
        "| SystemName | Description | UserDescription | Units | Active now |",
        "|---|---|---|---|---|",
    ]
    for (system, desc, user_desc), i in sorted(inputs.items(), key=lambda kv: kv[0]):
        lines.append(
            f"| {_cell(system)} | {_cell(desc)} | {_cell(user_desc)} "
            f"| {len(i.units)} | {len(i.active_units)} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="units to look at")
    parser.add_argument("--output", type=Path, default=OUTPUT, help="where to save the table")
    args = parser.parse_args(argv)
    # Greek labels must not crash printing on a Windows console; the saved file stays exact.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    source = "the MOCK API" if get_settings().use_mocks else "the LIVE Mobility API"
    print(f"Reading from {source} (read-only).")
    try:
        with get_mobility_client() as client:
            latest = client.latest_positions()
    except MobilityError as exc:
        print(f"The Mobility API refused the request: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:  # missing settings for the live API
        print(str(exc), file=sys.stderr)
        return 1

    units = pick_units(latest, args.limit)
    sensors, inputs = summarize(units)
    report = render(sensors, inputs, unit_count=len(units), source=source,
                    generated=datetime.now(UTC))  # fmt: skip
    print(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Saved to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

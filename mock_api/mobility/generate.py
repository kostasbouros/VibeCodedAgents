"""Generate the mock 3dtracking (Mobility) data in mock_api/mobility/data/.

    python -m mock_api.mobility.generate

Deterministic (fixed seed): running it again produces identical files. All times are written
relative to REFERENCE_TIME; the mock server shifts them so REFERENCE_TIME becomes "now".
Everything here is made up: no real vehicles, customers, people or phone numbers.

Field names follow the published OpenAPI spec (apiv2.3dtracking.net/openapi/v1.json); value
formats follow a real latestpositionslist response: 6-character unit Uids, times without a
time zone suffix (UTC fields plus Athens "Local" copies), "kph", "on"/"off", sensor values as
text, Driver/PointOfInterest objects with empty strings when unknown.

The "Id" on positions and sensor readings is ours: it is the StartId cursor and the server
removes it before answering. Alerts use AlertQueueId as their StartUID cursor (our guess).

Planted cases (see test_mobility_mock.py):
  VEH_003, VEH_008, VEH_015  offline, last report 2-3 days old
  VEH_005, VEH_012           low internal battery (%)
  VEH_009                    GSM jammed (input "gsmjammed" active, 0 satellites)
  VEH_011                    power cut: input "externalpowerfailure" active, External Battery 0
  VEH_017                    deep sleep: old report (30 h), no Signal Strength reading
  VEH_018..VEH_020           offline in the same minute, same phone prefix (carrier outage)
"""

import json
import math
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
REFERENCE_TIME = datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)
# Europe/Athens in winter (EET). The mock data only covers a few days of late November.
LOCAL_OFFSET = timedelta(hours=2)
NAMESPACE = uuid.UUID("6f1c2d3e-4b5a-4c6d-8e7f-001122334455")  # stable ids per name
SEED = 42

OFFLINE = {"VEH_003": 50, "VEH_008": 60, "VEH_015": 70}  # hours since last report
LOW_BATTERY = {"VEH_005": 8, "VEH_012": 12}  # internal battery, %
JAMMING = ("VEH_009",)
POWER_CUT = "VEH_011"
DEEP_SLEEP = "VEH_017"
CARRIER_OUTAGE = ("VEH_018", "VEH_019", "VEH_020")
OUTAGE_PHONE_PREFIX = "+3069900"
MOVING_UNITS = ("VEH_001", "VEH_002", "VEH_004", "VEH_006", "VEH_007", "VEH_010")

AREAS = [
    ("Marousi, Athens", 38.0561, 23.8086),
    ("Piraeus", 37.9420, 23.6465),
    ("Glyfada, Athens", 37.8650, 23.7530),
    ("Peristeri, Athens", 38.0153, 23.6916),
    ("Kalamaria, Thessaloniki", 40.5826, 22.9505),
    ("Sindos, Thessaloniki", 40.6700, 22.8050),
]
ALERT_TYPES = ["Power Cut", "Jamming", "Tamper", "Geofence Entry", "Panic"]
NO_DRIVER = {"Uid": "", "FirstName": "", "LastName": "", "Code": ""}
NO_POI = {"Uid": "", "Name": "unspecified"}


def fmt(t: datetime, ms: int | None = None) -> str:
    """A UTC time the way the real API writes it: no zone suffix, optional milliseconds."""
    text = t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return text if ms is None else f"{text}.{ms:03d}"


def fmt_local(t: datetime, ms: int | None = None) -> str:
    return fmt(t + LOCAL_OFFSET, ms)


def guid(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


def short_uid(name: str) -> str:
    """6 hex characters, like the real unit Uids."""
    return uuid.uuid5(NAMESPACE, name).hex[:6].upper()


def imei(rng: random.Random) -> str:
    body = "35" + "".join(str(rng.randint(0, 9)) for _ in range(12))  # 14 digits
    total = 0
    for i, ch in enumerate(reversed(body)):  # Luhn check digit
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return body + str((10 - total % 10) % 10)


def phone(rng: random.Random, name: str) -> str:
    if name in CARRIER_OUTAGE:
        return OUTAGE_PHONE_PREFIX + f"{rng.randint(0, 9999):04d}"
    return "+30697" + f"{rng.randint(0, 9999999):07d}"


def make_units(rng: random.Random) -> list[dict]:
    units = []
    for n in range(1, 21):
        name = f"VEH_{n:03d}"
        company = "CUST_042" if n <= 12 else "CUST_077"
        units.append(
            {
                "Uid": short_uid(name),
                "Name": name,
                "Imei": imei(rng),
                "Status": "Active",
                "GroupName": "Athens Fleet" if n % 3 else "Thessaloniki Fleet",
                "CompanyName": company,
                "CompanyUid": short_uid(company),
                "PhoneNumber": phone(rng, name),
                "UnitType": "FMB920" if n % 2 else "FMC130",
                "CreatedDateTimeUtc": fmt(REFERENCE_TIME - timedelta(days=200 + 7 * n)),
            }
        )
    assert len({u["Uid"] for u in units}) == len(units), "unit Uids must be unique"
    return units


def io(system: str, description: str, active: bool = False) -> dict:
    return {"SystemName": system, "Description": description, "UserDescription": "",
            "Active": active}  # fmt: skip


def inputs_outputs(name: str) -> list[dict]:
    return [
        io("panic", "Panic Button Activated"),
        io("externalpowerfailure", "External Power Failure", name == POWER_CUT),
        io("gsmjammed", "GSM Jammed", name in JAMMING),
    ]


def engine_status(ignition: bool, speed: float) -> str:
    if speed > 0:
        return "in transit" if ignition else "in transit (ign off)"
    return "idling" if ignition else "stopped"


def position(
    rng: random.Random, area: tuple, lat: float, lon: float, t: datetime, *,
    speed: float = 0.0, heading: float = 0.0, ignition: bool = False,
    odometer: float = 0.0, engine_hours: float = 0.0, ios: list[dict] | None = None,
) -> dict:  # fmt: skip
    server = t + timedelta(seconds=rng.randint(1, 4))
    return {
        "Latitude": round(lat, 6),
        "Longitude": round(lon, 6),
        "Address": f"{area[0]}, GR [{round(lat, 4)} / {round(lon, 4)}]",
        "Speed": float(round(speed)),
        "SpeedMeasure": "kph",
        "Heading": float(round(heading % 360)),
        "Ignition": "on" if ignition else "off",
        "Odometer": round(odometer, 2),
        "EngineTime": float(round(engine_hours * 3600)),  # seconds, as in the real data
        "EngineStatus": engine_status(ignition, speed),
        "ServerTimeUTC": fmt(server, rng.randint(0, 999)),
        "GPSTimeLocal": fmt_local(t),
        "GPSTimeUtc": fmt(t),
        "Driver": dict(NO_DRIVER),
        "PointOfInterest": dict(NO_POI),
        "InputOutputs": ios or [],
    }


def unit_ref(unit: dict) -> dict:
    return {"Uid": unit["Uid"], "Name": unit["Name"], "Imei": unit["Imei"]}


def make_trips(rng: random.Random, units: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """2 days of trips for MOVING_UNITS, a point every 30 s while moving."""
    points: list[tuple[datetime, dict]] = []
    last: dict[str, dict] = {}
    for idx, name in enumerate(MOVING_UNITS):
        ref = unit_ref(next(u for u in units if u["Name"] == name))
        area = AREAS[idx % len(AREAS)]
        lat, lon = area[1], area[2]
        odometer = rng.uniform(40_000, 180_000)
        hours = odometer / 45
        for start_hours in (46, 38, 22, 14):  # two trips per day before REFERENCE_TIME
            t = REFERENCE_TIME - timedelta(hours=start_hours, minutes=rng.randint(0, 50))
            heading, speed = rng.uniform(0, 360), 0.0
            for _ in range(rng.randint(50, 100)):  # 25-50 minutes of driving
                speed = min(110.0, max(15.0, speed + rng.uniform(-12, 15)))
                heading += rng.uniform(-25, 25)
                km = speed / 3600 * 30
                lat += km * math.cos(math.radians(heading)) / 111
                lon += km * math.sin(math.radians(heading)) / (111 * math.cos(math.radians(lat)))
                odometer += km
                hours += 30 / 3600
                p = position(rng, area, lat, lon, t, speed=speed, heading=heading,
                             ignition=True, odometer=odometer, engine_hours=hours)  # fmt: skip
                points.append((t, {"Unit": ref, **p}))
                t += timedelta(seconds=30)
            stop = position(rng, area, lat, lon, t, heading=heading, odometer=odometer,
                            engine_hours=hours)  # fmt: skip
            points.append((t, {"Unit": ref, **stop}))
            last[name] = {"area": area, "lat": lat, "lon": lon, "odometer": odometer,
                          "engine_hours": hours}  # fmt: skip
    points.sort(key=lambda p: (p[0], p[1]["Unit"]["Uid"]))
    return [{"Id": i, **p} for i, (_, p) in enumerate(points, start=1)], last


def reading(uid: str, name: str, value: str, sign: str, sensor_type: str, t: datetime) -> dict:
    """As in the real latestpositionslist answer, which carries only ReadingTimeLocal."""
    return {"UnitUid": uid, "Name": name, "Value": value, "MeasurementSign": sign,
            "ReadingTimeLocal": fmt_local(t), "SensorType": sensor_type}  # fmt: skip


def sensor_readings(rng: random.Random, unit: dict, t: datetime) -> list[dict]:
    name, uid = unit["Name"], unit["Uid"]
    truck = unit["UnitType"] == "FMC130"
    external = rng.uniform(25.5, 27.8) if truck else rng.uniform(12.4, 14.1)
    internal = rng.randint(90, 100)
    gsm, sats = rng.randint(3, 5), rng.randint(8, 16)
    if name in LOW_BATTERY:
        internal = LOW_BATTERY[name]
    if name in JAMMING:
        sats = 0
    if name == POWER_CUT:
        external = 0.0
    if name in CARRIER_OUTAGE:
        gsm = 1
    readings = [
        reading(uid, "External Battery", f"{external:.2f}", "v", "External Battery", t),
        reading(uid, "Internal Battery", str(internal), "%", "Battery", t),
        reading(uid, "Signal Strength", str(gsm), "", "GSM Signal Strength", t),
        reading(uid, "Satellite Count", str(sats), "", "Satellite Count", t),
    ]
    if name == DEEP_SLEEP:
        readings = [r for r in readings if r["Name"] != "Signal Strength"]
    return readings


def last_report_time(rng: random.Random, name: str) -> datetime:
    if name in OFFLINE:
        return REFERENCE_TIME - timedelta(hours=OFFLINE[name], minutes=rng.randint(0, 59))
    if name == DEEP_SLEEP:
        return REFERENCE_TIME - timedelta(hours=30, minutes=12)
    if name in CARRIER_OUTAGE:  # same minute, different seconds
        return (
            REFERENCE_TIME
            - timedelta(hours=5, minutes=12)
            + timedelta(seconds=5 + 15 * CARRIER_OUTAGE.index(name))
        )
    return REFERENCE_TIME - timedelta(minutes=rng.randint(1, 9), seconds=rng.randint(0, 59))


def make_latest(rng: random.Random, units: list[dict], last_trip: dict) -> list[dict]:
    """One record per unit, oldest report first (the real list is ordered that way)."""
    latest = []
    for i, unit in enumerate(units):
        name = unit["Name"]
        t = last_report_time(rng, name)
        ms = rng.randint(0, 999)
        if name in last_trip:  # parked where its last trip ended
            lt = last_trip[name]
            area, lat, lon = lt["area"], lt["lat"], lt["lon"]
            odo, hours = lt["odometer"], lt["engine_hours"]
        else:
            area = AREAS[i % len(AREAS)]
            lat = area[1] + rng.uniform(-0.02, 0.02)
            lon = area[2] + rng.uniform(-0.02, 0.02)
            odo = rng.uniform(20_000, 250_000)
            hours = odo / 45
        pos = position(rng, area, lat, lon, t, heading=rng.uniform(0, 360), odometer=odo,
                       engine_hours=hours, ios=inputs_outputs(name))  # fmt: skip
        latest.append(
            {
                "Uid": unit["Uid"],
                "Name": name,
                "Imei": unit["Imei"],
                "LastReportedTimeLocal": fmt_local(t, ms),
                "LastReportedTimeUTC": fmt(t, ms),
                "Position": pos,
                "SensorReadings": sensor_readings(rng, unit, t),
                "Assets": [],
            }
        )
    latest.sort(key=lambda lp: (lp["LastReportedTimeUTC"], lp["Uid"]))
    return latest


def make_sensor_list(latest: list[dict]) -> list[dict]:
    """sensorreadingslist rows: the latest readings with all three times (spec fields)."""
    rows = []
    for lp in latest:
        utc = lp["Position"]["GPSTimeUtc"]
        server = lp["LastReportedTimeUTC"]
        for r in lp["SensorReadings"]:
            rows.append({**r, "ReadingTimeUtc": utc, "ServerTimeUtc": server})
    rows.sort(key=lambda r: (r["ServerTimeUtc"], r["UnitUid"], r["Name"]))
    return [{"Id": i, **r} for i, r in enumerate(rows, start=1)]


def make_alerts(latest: list[dict]) -> tuple[list[dict], list[dict]]:
    types = [{"UID": guid(f"alert-type-{name}"), "Name": name} for name in ALERT_TYPES]
    type_uid = {t["Name"]: t["UID"] for t in types}
    by_name = {lp["Name"]: lp for lp in latest}
    planned = [
        ("VEH_002", "Geofence Entry", 40),
        ("VEH_009", "Jamming", 20),
        ("VEH_014", "Tamper", 15),
        ("VEH_006", "Geofence Entry", 13),
        ("VEH_011", "Power Cut", 6),
        ("VEH_007", "Panic", 3),
        ("VEH_009", "Jamming", 1),
        ("VEH_011", "Power Cut", 0.5),
    ]
    planned.sort(key=lambda p: -p[2])  # oldest first
    alerts = []
    for queue_id, (unit_name, type_name, hours_ago) in enumerate(planned, start=50_001):
        address = by_name[unit_name]["Position"]["Address"]
        alerts.append(
            {
                "AlertUID": guid(f"alert-{unit_name}-{type_name}-{hours_ago}"),
                "AlertName": f"{type_name} alert",
                "AlertType": type_name,
                "AlertTypeUID": type_uid[type_name],
                "Vehicle": unit_name,
                "CreatedDate": fmt(REFERENCE_TIME - timedelta(hours=hours_ago)),
                "AlertMessage": f"{type_name}: {unit_name} near {address}",
                "AlertQueueId": queue_id,
            }
        )
    return alerts, types


def build() -> dict[str, object]:
    rng = random.Random(SEED)
    units = make_units(rng)
    positions, last_trip = make_trips(rng, units)
    latest = make_latest(rng, units, last_trip)
    alerts, alert_types = make_alerts(latest)
    return {
        "meta.json": {
            "ReferenceTimeUtc": REFERENCE_TIME.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seed": SEED,
        },
        "units.json": units,
        "latest_positions.json": latest,
        "positions.json": positions,
        "sensor_readings.json": make_sensor_list(latest),
        "alerts.json": alerts,
        "alert_types.json": alert_types,
    }


def render(content: object) -> str:
    return json.dumps(content, indent=1, ensure_ascii=False) + "\n"


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for filename, content in build().items():
        (DATA_DIR / filename).write_text(render(content), encoding="utf-8")
        size = len(content) if isinstance(content, list) else 1
        print(f"wrote {filename} ({size} records)")


if __name__ == "__main__":
    main()

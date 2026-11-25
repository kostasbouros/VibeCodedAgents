"""Tests for the mock 3dtracking (Mobility) API."""

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mock_api.mobility import generate, routes, store
from mock_api.mobility.routes import ERROR_CODES, MOCK_USER_ID
from mock_api.server import app

API = "/api/v1.0"
client = TestClient(app)

# Field names from the published OpenAPI spec (apiv2.3dtracking.net/openapi/v1.json).
UNIT_FIELDS = {"Uid", "Name", "Imei", "Status", "GroupName", "CompanyName", "CompanyUid",
               "PhoneNumber", "UnitType", "CreatedDateTimeUtc"}  # fmt: skip
LATEST_FIELDS = UNIT_FIELDS | {"LastReportedTimeLocal", "LastReportedTimeUTC", "Position",
                               "SensorReadings", "Assets"}  # fmt: skip
POSITION_FIELDS = {"Unit", "Latitude", "Longitude", "Address", "Speed", "SpeedMeasure", "Heading",
                   "Ignition", "Odometer", "EngineTime", "EngineStatus", "ServerTimeUTC",
                   "GPSTimeLocal", "GPSTimeUtc", "Driver", "PointOfInterest",
                   "InputOutputs"}  # fmt: skip
READING_FIELDS = {"UnitUid", "Name", "Value", "MeasurementSign", "ReadingTimeLocal",
                  "ReadingTimeUtc", "ServerTimeUtc", "SensorType"}  # fmt: skip
ALERT_FIELDS = {"AlertUID", "AlertName", "AlertType", "AlertTypeUID", "Vehicle", "CreatedDate",
                "AlertMessage", "AlertQueueId"}  # fmt: skip
LOGIN = f"{API}/authentication/userauthenticate?username=mock&password=mock"


@pytest.fixture
def auth() -> dict[str, str]:
    body = client.post(LOGIN).json()
    assert body["Status"]["Result"] == "ok"
    return body["Result"]


def get(path: str, auth: dict[str, str], **params: Any) -> Any:
    body = client.get(f"{API}{path}", params={**auth, **params}).json()
    assert body["Status"]["Result"] == "ok", body["Status"]
    return body["Result"]


def latest_by_name(auth: dict[str, str]) -> dict[str, dict]:
    return {lp["Name"]: lp for lp in get("/units/latestpositionslist", auth)}


def readings(lp: dict) -> dict[str, float]:
    return {r["Name"]: float(r["Value"]) for r in lp["SensorReadings"]}


def active_inputs(lp: dict) -> set[str]:
    return {io["SystemName"] for io in lp["Position"]["InputOutputs"] if io["Active"]}


def age(lp: dict) -> timedelta:
    return datetime.now(UTC) - store.parse_time(lp["LastReportedTimeUTC"])


# --- Authentication -------------------------------------------------------------------------


def test_login_returns_the_real_fields() -> None:
    body = client.post(LOGIN).json()
    assert body["Status"] == {"Result": "ok", "ErrorCode": "0", "Message": "ok"}
    assert set(body["Result"]) == {"UserIdGuid", "SessionId"}  # as in the real response
    assert body["Result"]["UserIdGuid"] == MOCK_USER_ID
    assert len(body["Result"]["SessionId"]) == 36


def test_wrong_password_fails() -> None:
    body = client.post(f"{API}/authentication/userauthenticate?username=mock&password=x").json()
    assert body["Status"] == {
        "Result": "Failed",
        "ErrorCode": ERROR_CODES["InvalidCredentials"],
        "Message": "Wrong user name or password",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/authentication/errorcodes",
        "/units/unit/list",
        "/units/latestpositionslist",
        "/data/positionslist",
        "/data/sensorreadingslist",
        "/alerts/alerts/alerttypelist",
        "/alerts/alerts/list",
        "/units/ABC123/positionattime",
    ],
)
@pytest.mark.parametrize("which", ["missing", "wrong_session", "wrong_user"])
def test_wrong_or_missing_session_is_rejected(path: str, which: str, auth: dict[str, str]) -> None:
    params = {
        "missing": {},
        "wrong_session": {**auth, "SessionId": "not-a-real-session"},
        "wrong_user": {**auth, "UserIdGuid": "00000000-0000-0000-0000-000000000000"},
    }[which]
    body = client.get(f"{API}{path}", params=params).json()
    assert body["Status"]["Result"] == "Failed"
    assert body["Status"]["ErrorCode"] == ERROR_CODES["InvalidSession"]
    assert body["Result"] is None


def test_expired_sessions_are_rejected(auth: dict[str, str]) -> None:
    routes.expire_all_sessions()
    body = client.get(f"{API}/units/unit/list", params=auth).json()
    assert body["Status"]["ErrorCode"] == ERROR_CODES["InvalidSession"]


def test_error_code_list(auth: dict[str, str]) -> None:
    rows = get("/authentication/errorcodes", auth)
    assert {r["Code"] for r in rows} == set(ERROR_CODES)
    assert all(set(r) == {"Number", "Code", "Description"} for r in rows)


# --- Units ----------------------------------------------------------------------------------


def test_units_have_the_documented_fields(auth: dict[str, str]) -> None:
    units = get("/units/unit/list", auth)
    assert [u["Name"] for u in units] == [f"VEH_{n:03d}" for n in range(1, 21)]
    for u in units:
        assert set(u) == UNIT_FIELDS
        assert len(u["Imei"]) == 15 and u["Imei"].isdigit()
        assert u["CompanyName"] in {"CUST_042", "CUST_077"}
        assert u["UnitType"] in {"FMB920", "FMC130"}
        assert len(u["Uid"]) == 6  # like the real Uids


def test_latest_position_shape_matches_the_real_response(auth: dict[str, str]) -> None:
    rows = get("/units/latestpositionslist", auth)
    lp = rows[0]
    # The real answer sends a subset of the spec's fields, in this order.
    assert list(lp) == ["Uid", "Name", "Imei", "LastReportedTimeLocal", "LastReportedTimeUTC",
                        "Position", "SensorReadings", "Assets"]  # fmt: skip
    assert set(lp) <= LATEST_FIELDS
    pos = lp["Position"]
    assert set(pos) == POSITION_FIELDS - {"Unit"}
    assert pos["SpeedMeasure"] == "kph" and pos["Ignition"] in {"on", "off"}
    assert pos["Driver"] == {"Uid": "", "FirstName": "", "LastName": "", "Code": ""}
    assert pos["PointOfInterest"] == {"Uid": "", "Name": "unspecified"}
    assert all(set(io) == {"SystemName", "Description", "UserDescription", "Active"}
               for io in pos["InputOutputs"])  # fmt: skip
    for r in lp["SensorReadings"]:
        assert set(r) <= READING_FIELDS and isinstance(r["Value"], str)
    times = [r["LastReportedTimeUTC"] for r in rows]
    assert times == sorted(times)  # oldest report first, as in the real list


def test_times_have_no_zone_suffix_and_local_is_athens(auth: dict[str, str]) -> None:
    lp = get("/units/latestpositionslist", auth)[-1]
    assert not lp["LastReportedTimeUTC"].endswith("Z")
    assert "." in lp["LastReportedTimeUTC"]  # milliseconds, like the real data
    utc = store.parse_time(lp["LastReportedTimeUTC"])
    local = store.parse_time(lp["LastReportedTimeLocal"])
    assert local - utc == generate.LOCAL_OFFSET


def test_latest_positions_since_and_one_unit(auth: dict[str, str]) -> None:
    every = get("/units/latestpositionslist", auth)
    since = store.parse_time(every[-5]["LastReportedTimeUTC"]) - timedelta(seconds=1)
    newer = get("/units/latestpositionslist", auth,
                LastDateReceivedUtc=store.format_api_date(since))  # fmt: skip
    assert [r["Uid"] for r in newer] == [r["Uid"] for r in every[-5:]]
    one = get("/units/latestpositionslist", auth, UnitUid=every[0]["Uid"])
    assert [r["Uid"] for r in one] == [every[0]["Uid"]]


# --- Planted cases (times are shifted, so ages are relative to now) -------------------------


def test_offline_units_last_reported_2_to_3_days_ago(auth: dict[str, str]) -> None:
    latest = latest_by_name(auth)
    for name in ("VEH_003", "VEH_008", "VEH_015"):
        assert timedelta(days=2) <= age(latest[name]) <= timedelta(days=3, hours=1), name


def test_healthy_units_reported_in_the_last_15_minutes(auth: dict[str, str]) -> None:
    latest = latest_by_name(auth)
    for name in ("VEH_001", "VEH_013", "VEH_016"):
        assert age(latest[name]) < timedelta(minutes=15), name


def test_low_battery_units(auth: dict[str, str]) -> None:
    latest = latest_by_name(auth)
    for name in ("VEH_005", "VEH_012"):
        assert readings(latest[name])["Internal Battery"] < 20, name
    assert readings(latest["VEH_001"])["Internal Battery"] >= 90


def test_jamming_unit(auth: dict[str, str]) -> None:
    latest = latest_by_name(auth)
    assert "gsmjammed" in active_inputs(latest["VEH_009"])
    assert readings(latest["VEH_009"])["Satellite Count"] == 0
    assert not any("gsmjammed" in active_inputs(lp) for n, lp in latest.items() if n != "VEH_009")


def test_power_cut_unit_has_battery_but_no_external_power(auth: dict[str, str]) -> None:
    lp = latest_by_name(auth)["VEH_011"]
    assert "externalpowerfailure" in active_inputs(lp)
    assert readings(lp)["External Battery"] == 0
    assert readings(lp)["Internal Battery"] >= 90


def test_deep_sleep_unit(auth: dict[str, str]) -> None:
    lp = latest_by_name(auth)["VEH_017"]
    assert "Signal Strength" not in readings(lp)
    assert age(lp) > timedelta(hours=24)


def test_carrier_outage_units_went_quiet_in_the_same_minute(auth: dict[str, str]) -> None:
    latest = latest_by_name(auth)
    units = {u["Name"]: u for u in get("/units/unit/list", auth)}
    outage = ("VEH_018", "VEH_019", "VEH_020")
    minutes = {latest[n]["LastReportedTimeUTC"][:16] for n in outage}
    assert len(minutes) == 1
    prefixes = {units[n]["PhoneNumber"][:8] for n in outage}
    assert prefixes == {generate.OUTAGE_PHONE_PREFIX}
    others = [u for n, u in units.items() if n not in outage]
    assert not any(u["PhoneNumber"].startswith(generate.OUTAGE_PHONE_PREFIX) for u in others)
    assert all(age(latest[n]) > timedelta(hours=4) for n in outage)


# --- Positions ------------------------------------------------------------------------------


def test_positions_are_trips_for_six_units_every_30_seconds(auth: dict[str, str]) -> None:
    result = get("/data/positionslist", auth, StartId="0")
    assert set(result) == {"Position", "StartId", "IsCurrent"}
    rows = result["Position"]
    assert result["IsCurrent"] is True
    assert all(set(r) == POSITION_FIELDS for r in rows)
    assert len({r["Unit"]["Uid"] for r in rows}) == 6
    first = rows[0]["Unit"]["Uid"]
    one_unit = [r for r in rows if r["Unit"]["Uid"] == first and r["Ignition"] == "on"]
    times = [store.parse_time(r["GPSTimeUtc"]) for r in one_unit]
    assert 30.0 in {(b - a).total_seconds() for a, b in itertools.pairwise(times)}
    assert timedelta(hours=24) < times[-1] - times[0] < timedelta(days=2)


def test_positions_start_id_pages_and_is_current(
    auth: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(routes, "POSITIONS_PAGE", 1000)
    first = get("/data/positionslist", auth, StartId="0")
    assert len(first["Position"]) == 1000 and first["IsCurrent"] is False
    second = get("/data/positionslist", auth, StartId=str(first["StartId"]))
    assert second["IsCurrent"] is True
    assert len(first["Position"]) + len(second["Position"]) == 1818
    last = get("/data/positionslist", auth, StartId=str(second["StartId"]))
    assert last == {"Position": [], "StartId": second["StartId"], "IsCurrent": True}


def test_positions_blank_start_means_last_24_hours(auth: dict[str, str]) -> None:
    all_rows = get("/data/positionslist", auth, StartId="0")["Position"]
    recent = get("/data/positionslist", auth)["Position"]
    assert 0 < len(recent) < len(all_rows)
    cutoff = datetime.now(UTC) - timedelta(hours=24, minutes=1)
    assert all(store.parse_time(r["GPSTimeUtc"]) >= cutoff for r in recent)


def test_positions_start_hour_and_unit_filter(auth: dict[str, str]) -> None:
    rows = get("/data/positionslist", auth, StartId="0")["Position"]
    moving = sorted({r["Unit"]["Uid"] for r in rows})
    two = get("/data/positionslist", auth, StartId="0", Uid=f"{moving[0]},{moving[1]}")
    assert {r["Unit"]["Uid"] for r in two["Position"]} == {moving[0], moving[1]}
    mine = [r for r in rows if r["Unit"]["Uid"] == moving[0]]
    start = store.parse_time(mine[len(mine) // 2]["GPSTimeUtc"])
    later = get("/data/positionslist", auth, Uid=moving[0], StartHourUtc=start.isoformat())
    assert later["Position"]
    assert all(store.parse_time(r["GPSTimeUtc"]) >= start for r in later["Position"])


def test_positions_bad_start_id(auth: dict[str, str]) -> None:
    body = client.get(f"{API}/data/positionslist", params={**auth, "StartId": "abc"}).json()
    assert body["Status"]["ErrorCode"] == ERROR_CODES["InvalidParameter"]


def test_position_at_time(auth: dict[str, str]) -> None:
    target = get("/data/positionslist", auth, StartId="0")["Position"][500]
    when = store.format_api_date(store.parse_time(target["GPSTimeUtc"]))
    found = get(f"/units/{target['Unit']['Uid']}/positionattime", auth,
                PointInTimeDateTimeUTC=when)  # fmt: skip
    assert set(found) == UNIT_FIELDS | {"Position", "SensorReadings", "Assets"}
    assert found["Position"]["GPSTimeUtc"] == target["GPSTimeUtc"]
    assert found["Position"]["Latitude"] == target["Latitude"]


def test_position_at_time_errors(auth: dict[str, str]) -> None:
    uid = get("/units/unit/list", auth)[0]["Uid"]
    long_ago = client.get(
        f"{API}/units/{uid}/positionattime",
        params={**auth, "PointInTimeDateTimeUTC": "01 Jan 2000 00:00:00"},
    ).json()
    assert long_ago["Status"]["ErrorCode"] == ERROR_CODES["NoData"]
    unknown = client.get(
        f"{API}/units/nope/positionattime",
        params={**auth, "PointInTimeDateTimeUTC": "01 Jan 2026 00:00:00"},
    ).json()
    assert unknown["Status"]["ErrorCode"] == ERROR_CODES["UnitNotFound"]


# --- Sensor readings ------------------------------------------------------------------------


def test_sensor_readings_list(auth: dict[str, str]) -> None:
    result = get("/data/sensorreadingslist", auth, StartId="0")
    assert set(result) == {"SensorReadings", "StartId"}
    rows = result["SensorReadings"]
    assert len({r["UnitUid"] for r in rows}) == 20
    assert all(set(r) == READING_FIELDS for r in rows)
    after = get("/data/sensorreadingslist", auth, StartId=str(result["StartId"]))
    assert after["SensorReadings"] == []
    uid = get("/units/unit/list", auth)[10]["Uid"]  # VEH_011
    mine = get("/data/sensorreadingslist", auth, Uid=uid, StartId="0")["SensorReadings"]
    assert {r["Name"]: r["Value"] for r in mine}["External Battery"] == "0.00"


# --- Alerts ---------------------------------------------------------------------------------


def test_alert_types(auth: dict[str, str]) -> None:
    types = get("/alerts/alerts/alerttypelist", auth)
    assert [t["Name"] for t in types] == generate.ALERT_TYPES
    assert all(set(t) == {"UID", "Name"} for t in types)


def test_alerts_list_honours_start_uid(auth: dict[str, str]) -> None:
    result = get("/alerts/alerts/list", auth, StartUID="0")
    assert set(result) == {"AlertList", "StartUID"}
    alerts = result["AlertList"]
    assert all(set(a) == ALERT_FIELDS for a in alerts)
    assert [a["CreatedDate"] for a in alerts] == sorted(a["CreatedDate"] for a in alerts)
    assert result["StartUID"] == str(alerts[-1]["AlertQueueId"])
    after_third = get("/alerts/alerts/list", auth, StartUID=str(alerts[2]["AlertQueueId"]))
    assert after_third["AlertList"] == alerts[3:]
    empty = get("/alerts/alerts/list", auth, StartUID=result["StartUID"])
    assert empty == {"AlertList": [], "StartUID": result["StartUID"]}
    bad = client.get(f"{API}/alerts/alerts/list", params={**auth, "StartUID": "nope"}).json()
    assert bad["Status"]["ErrorCode"] == ERROR_CODES["InvalidStartUID"]


def test_alerts_blank_start_and_type_filter(auth: dict[str, str]) -> None:
    every = get("/alerts/alerts/list", auth, StartUID="0")["AlertList"]
    recent = get("/alerts/alerts/list", auth)["AlertList"]
    assert 0 < len(recent) < len(every)  # blank = last 24 hours only
    types = get("/alerts/alerts/alerttypelist", auth)
    jamming = next(t["UID"] for t in types if t["Name"] == "Jamming")
    only = get("/alerts/alerts/list", auth, StartUID="0", AlertTypeUID=jamming)["AlertList"]
    assert only and all(a["AlertType"] == "Jamming" for a in only)


def test_planted_alerts_match_the_planted_units(auth: dict[str, str]) -> None:
    alerts = get("/alerts/alerts/list", auth, StartUID="0")["AlertList"]
    pairs = {(a["Vehicle"], a["AlertType"]) for a in alerts}
    assert {("VEH_011", "Power Cut"), ("VEH_009", "Jamming")} <= pairs


# --- Data files -----------------------------------------------------------------------------


def test_generator_reproduces_the_committed_files_exactly() -> None:
    for filename, content in generate.build().items():
        on_disk = (store.DATA_DIR / filename).read_text(encoding="utf-8")
        assert on_disk == generate.render(content), filename


def test_frozen_time_serves_stored_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_FREEZE_TIME", "1")
    assert store.load("meta")["ReferenceTimeUtc"] == "2026-11-25T12:00:00Z"
    assert store.time_offset() == timedelta(0)
    raw = store._raw("latest_positions")[0]["LastReportedTimeUTC"]
    assert store.load("latest_positions")[0]["LastReportedTimeUTC"] == raw


def test_shift_keeps_each_time_format() -> None:
    offset = timedelta(days=1, minutes=3)
    assert store._shift_text("2026-11-25T12:00:00.087", offset) == "2026-11-26T12:03:00.087"
    assert store._shift_text("2026-11-25T12:00:00", offset) == "2026-11-26T12:03:00"
    assert store._shift_text("2026-11-25T12:00:00Z", offset) == "2026-11-26T12:03:00Z"


def test_api_date_format() -> None:
    parsed = store.parse_time("21 May 2013 18:14:56")
    assert parsed == datetime(2013, 5, 21, 18, 14, 56, tzinfo=UTC)
    assert store.format_api_date(parsed) == "21 May 2013 18:14:56"


def test_mock_data_contains_no_real_looking_personal_data() -> None:
    from shared.redact import redact

    for name in ("units", "latest_positions", "alerts", "sensor_readings"):
        text = (store.DATA_DIR / f"{name}.json").read_text(encoding="utf-8")
        result = redact(text)
        assert result.ok, (name, result.problems)
        # Only the (fake) phone numbers and IMEIs are personal-looking; ids, dates,
        # coordinates and voltages must be left alone.
        assert set(result.counts) <= {"PHONE", "IMEI"}, (name, result.counts)


def test_every_session_is_new_but_the_user_id_is_stable() -> None:
    first, second = client.post(LOGIN).json()["Result"], client.post(LOGIN).json()["Result"]
    assert first["SessionId"] != second["SessionId"]
    assert first["UserIdGuid"] == second["UserIdGuid"]


def test_error_codes_are_numeric_strings() -> None:
    assert all(code.isdigit() and code != "0" for code in ERROR_CODES.values())

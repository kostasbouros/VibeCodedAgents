"""Mock of the 3dtracking Client WebApi v1.0 (Mobility). Same paths, query parameters and
{Status, Result} envelope as the published OpenAPI spec (apiv2.3dtracking.net/openapi/v1.json).

Log in with username "mock" / password "mock" (query parameters) to get UserIdGuid and
SessionId, then pass both as query parameters on every other call.

CONFIRMED (real responses): success is HTTP 200 with
Status {"Result": "ok", "ErrorCode": "0", "Message": "ok"}; login returns
{"UserIdGuid", "SessionId"}; latestpositionslist field names and value formats.
FROM THE SPEC: parameter names, result shapes, cursors (StartId / StartUID), IsCurrent,
page sizes (positions 5000, or 2000 with input/outputs; sensor readings 2500),
"0 = 7 days ago, blank = 24 hours ago", dates as "21 May 2013 18:14:56".
OUR GUESSES (check against the real API):
- Failures: HTTP 200, Status.Result "Failed", a numeric-string ErrorCode (numbers are ours).
- alerts/list StartUID is the AlertQueueId of the last alert received.
- CreatedDate on alerts is UTC.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter

from mock_api.mobility import store

router = APIRouter(prefix="/api/v1.0")

MOCK_USERNAME = "mock"
MOCK_PASSWORD = "mock"
MOCK_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "mock-api/mobility/user/mock"))
POSITIONS_PAGE = 5000
POSITIONS_PAGE_WITH_IO = 2000
SENSOR_READINGS_PAGE = 2500
_sessions: set[str] = set()

# Our guesses until we see a real failure response. Served by /authentication/errorcodes.
ERROR_CODES = {
    "InvalidCredentials": "1001",
    "InvalidSession": "1002",
    "UnitNotFound": "1003",
    "MissingParameter": "1004",
    "InvalidParameter": "1005",
    "NoData": "1006",
    "InvalidStartUID": "1007",
}


def ok(result: Any) -> dict[str, Any]:
    return {"Status": {"Result": "ok", "ErrorCode": "0", "Message": "ok"}, "Result": result}


def failed(error: str, message: str) -> dict[str, Any]:
    """`error` is a key of ERROR_CODES; the response carries its numeric code."""
    status = {"Result": "Failed", "ErrorCode": ERROR_CODES[error], "Message": message}
    return {"Status": status, "Result": None}


def expire_all_sessions() -> None:
    """For tests: behave as if every session reached its 24-hour limit."""
    _sessions.clear()


def _session_error(user_id: str | None, session_id: str | None) -> dict[str, Any] | None:
    if user_id != MOCK_USER_ID or not session_id or session_id not in _sessions:
        return failed("InvalidSession", "UserIdGuid or SessionId is missing, unknown or expired")
    return None


def _now() -> datetime:
    """The mock's present moment (the reference time when MOCK_FREEZE_TIME is set)."""
    return store.reference_time() + store.time_offset()


def _unit_filter(uids: str | None) -> set[str] | None:
    """None means all units ("" or "*")."""
    if not uids or uids.strip() == "*":
        return None
    return {u.strip() for u in uids.split(",") if u.strip()}


def _cursor_rows(
    rows: list[dict], start: str | None, time_key: str
) -> tuple[list[dict], int | None] | None:
    """Rows after a numeric cursor: blank = last 24 h, 0 = last 7 days. None if not a number."""
    if start is None or start == "":
        cutoff = _now() - timedelta(hours=24)
        return [r for r in rows if store.parse_time(r[time_key]) >= cutoff], None
    if not start.isdigit():
        return None
    if int(start) == 0:
        cutoff = _now() - timedelta(days=7)
        return [r for r in rows if store.parse_time(r[time_key]) >= cutoff], 0
    return [r for r in rows if r["Id"] > int(start)], int(start)


def _page(rows: list[dict], size: int, given: int | None, all_rows: list[dict]) -> tuple:
    """One page, the next cursor and whether nothing is left after it."""
    page = rows[:size]
    if page:
        next_id = page[-1]["Id"]
    else:
        next_id = given if given else max((r["Id"] for r in all_rows), default=0)
    return [{k: v for k, v in r.items() if k != "Id"} for r in page], next_id, len(rows) <= size


def _login(username: str | None, password: str | None) -> dict[str, Any]:
    if username != MOCK_USERNAME or password != MOCK_PASSWORD:
        return failed("InvalidCredentials", "Wrong user name or password")
    session_id = str(uuid.uuid4())
    _sessions.add(session_id)
    return ok({"UserIdGuid": MOCK_USER_ID, "SessionId": session_id})


# --- Authentication -------------------------------------------------------------------------


@router.post("/authentication/userauthenticate")
def authenticate_post(username: str | None = None, password: str | None = None) -> dict:
    return _login(username, password)


@router.get("/authentication/userauthenticate")
def authenticate_get(username: str | None = None, password: str | None = None) -> dict:
    return _login(username, password)


@router.get("/authentication/errorcodes")
def error_codes(UserIdGuid: str | None = None, SessionId: str | None = None) -> dict:
    rows = [
        {"Number": int(n), "Code": code, "Description": code} for code, n in ERROR_CODES.items()
    ]
    return _session_error(UserIdGuid, SessionId) or ok(rows)


# --- Units ----------------------------------------------------------------------------------


@router.get("/units/unit/list")
def unit_list(UserIdGuid: str | None = None, SessionId: str | None = None) -> dict:
    return _session_error(UserIdGuid, SessionId) or ok(store.load("units"))


@router.get("/units/latestpositionslist")
def latest_positions(
    UserIdGuid: str | None = None,
    SessionId: str | None = None,
    LastDateReceivedUtc: str = "",
    UnitUid: str = "",
) -> dict:
    if error := _session_error(UserIdGuid, SessionId):
        return error
    rows = store.load("latest_positions")
    if UnitUid:
        rows = [r for r in rows if r["Uid"] == UnitUid]
    if LastDateReceivedUtc:
        try:
            since = store.parse_time(LastDateReceivedUtc)
        except ValueError:
            return failed("InvalidParameter", "LastDateReceivedUtc: use dd MMM yyyy HH:mm:ss")
        rows = [r for r in rows if store.parse_time(r["LastReportedTimeUTC"]) > since]
    return ok(rows)


@router.get("/units/{Uid}/positionattime")
def position_at_time(
    Uid: str,
    UserIdGuid: str | None = None,
    SessionId: str | None = None,
    PointInTimeDateTimeUTC: str = "",
) -> dict:
    if error := _session_error(UserIdGuid, SessionId):
        return error
    unit = next((u for u in store.load("units") if u["Uid"] == Uid), None)
    if unit is None:
        return failed("UnitNotFound", f"No unit with Uid {Uid}")
    if not PointInTimeDateTimeUTC:
        return failed("MissingParameter", "PointInTimeDateTimeUTC is required")
    try:
        at = store.parse_time(PointInTimeDateTimeUTC)
    except ValueError:
        return failed("InvalidParameter", "PointInTimeDateTimeUTC: use dd MMM yyyy HH:mm:ss")
    candidates = [p for p in store.load("positions") if p["Unit"]["Uid"] == Uid]
    candidates += [lp["Position"] for lp in store.load("latest_positions") if lp["Uid"] == Uid]
    before = [p for p in candidates if store.parse_time(p["GPSTimeUtc"]) <= at]
    if not before:
        return failed("NoData", "No position at or before that time")
    best = max(before, key=lambda p: store.parse_time(p["GPSTimeUtc"]))
    best = {k: v for k, v in best.items() if k not in ("Id", "Unit")}
    return ok({**unit, "Position": best, "SensorReadings": [], "Assets": []})


# --- Data -----------------------------------------------------------------------------------


@router.get("/data/positionslist")
def positions_list(
    UserIdGuid: str | None = None,
    SessionId: str | None = None,
    Uid: str = "*",
    StartId: str | None = None,
    IncludeInputOutputs: str = "false",
    StartHourUtc: str | None = None,
) -> dict:
    if error := _session_error(UserIdGuid, SessionId):
        return error
    all_rows = store.load("positions")
    wanted = _unit_filter(Uid)
    rows = [p for p in all_rows if wanted is None or p["Unit"]["Uid"] in wanted]
    if not StartId and StartHourUtc:  # only considered when StartId is not given
        try:
            start = store.parse_time(StartHourUtc)
        except ValueError:
            return failed("InvalidParameter", "StartHourUtc is not a valid date and time")
        rows, given = [p for p in rows if store.parse_time(p["GPSTimeUtc"]) >= start], None
    else:
        picked = _cursor_rows(rows, StartId, "GPSTimeUtc")
        if picked is None:
            return failed("InvalidParameter", "StartId must be a whole number")
        rows, given = picked
    include_io = IncludeInputOutputs.strip().lower() == "true"
    size = POSITIONS_PAGE_WITH_IO if include_io else POSITIONS_PAGE
    page, next_id, is_current = _page(rows, size, given, all_rows)
    if not include_io:
        page = [{**p, "InputOutputs": []} for p in page]
    return ok({"Position": page, "StartId": next_id, "IsCurrent": is_current})


@router.get("/data/sensorreadingslist")
def sensor_readings_list(
    UserIdGuid: str | None = None,
    SessionId: str | None = None,
    Uid: str = "*",
    StartId: str | None = None,
) -> dict:
    if error := _session_error(UserIdGuid, SessionId):
        return error
    all_rows = store.load("sensor_readings")
    wanted = _unit_filter(Uid)
    rows = [r for r in all_rows if wanted is None or r["UnitUid"] in wanted]
    picked = _cursor_rows(rows, StartId, "ServerTimeUtc")
    if picked is None:
        return failed("InvalidParameter", "StartId must be a whole number")
    page, next_id, _ = _page(picked[0], SENSOR_READINGS_PAGE, picked[1], all_rows)
    return ok({"SensorReadings": page, "StartId": next_id})


# --- Alerts ---------------------------------------------------------------------------------


@router.get("/alerts/alerts/alerttypelist")
def alert_type_list(UserIdGuid: str | None = None, SessionId: str | None = None) -> dict:
    return _session_error(UserIdGuid, SessionId) or ok(store.load("alert_types"))


@router.get("/alerts/alerts/list")
def alert_list(
    UserIdGuid: str | None = None,
    SessionId: str | None = None,
    AlertTypeUID: str = "",
    StartUID: str = "",
) -> dict:
    if error := _session_error(UserIdGuid, SessionId):
        return error
    all_alerts = store.load("alerts")
    alerts = [a for a in all_alerts if not AlertTypeUID or a["AlertTypeUID"] == AlertTypeUID]
    if not StartUID or StartUID == "0":
        cutoff = _now() - (timedelta(days=7) if StartUID == "0" else timedelta(hours=24))
        alerts = [a for a in alerts if store.parse_time(a["CreatedDate"]) >= cutoff]
    elif StartUID.isdigit():
        alerts = [a for a in alerts if a["AlertQueueId"] > int(StartUID)]
    else:
        return failed("InvalidStartUID", f"StartUID {StartUID} is not valid")
    if alerts:
        next_uid = str(alerts[-1]["AlertQueueId"])
    else:
        next_uid = StartUID or str(max((a["AlertQueueId"] for a in all_alerts), default=0))
    return ok({"AlertList": alerts, "StartUID": next_uid})

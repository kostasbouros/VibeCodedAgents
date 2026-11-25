"""Read-only client for the G4S Mobility platform (3dtracking Client WebApi v1.0).

Source of truth: the published OpenAPI spec (https://apiv2.3dtracking.net/openapi/v1.json)
plus real responses (login and latestpositionslist) seen on 25 Nov 2026.

- Authentication is a session: POST /authentication/userauthenticate?username=..&password=..
  returns UserIdGuid and SessionId, and every other call sends both as query parameters.
  They are kept in memory only and never logged (shared/http.py logs paths without queries).
- Sessions expire after 24 hours. We renew after 23 hours, and once more if a call says the
  session is invalid.
- Every answer is {"Status": {"Result", "ErrorCode", "Message"}, "Result": ...}. A Status.Result
  other than ok/success raises MobilityError.
- READ-ONLY on purpose: the API also has write endpoints (drivers, POIs, journals, fuel logs...).
  This client implements authentication and GET calls only. Do not add other POST methods.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Self
from urllib.parse import quote

import httpx
import structlog
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, SecretStr

from shared.config import get_settings
from shared.http import make_client, request_with_retry

log = structlog.get_logger(__name__)

API = "/api/v1.0"
AUTH_PATH = f"{API}/authentication/userauthenticate"
SESSION_MAX_AGE = timedelta(hours=23)  # the API's limit is 24 hours
MOCK_BASE_URL = "http://localhost:8099"
MOCK_USERNAME = MOCK_PASSWORD = "mock"
_OK_RESULTS = {"ok", "success"}  # real login says "ok"; the build guide says "Success"
# The real invalid-session error code is not confirmed yet (see /authentication/errorcodes),
# so a failed call counts as a session problem when its message mentions the session.
_SESSION_WORDS = ("session", "expired")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MobilityError(Exception):
    """The Mobility API refused or failed a call. Never contains credentials or session ids."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(f"Mobility API error {error_code}: {message}")
        self.error_code = error_code
        self.message = message


# --- Value helpers --------------------------------------------------------------------------


def _blank_as_none(value: Any) -> Any:
    return None if value == "" else value


def _as_utc(value: datetime | None) -> datetime | None:
    """The API writes UTC times without a zone suffix; mark them as UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _none_as_empty(value: Any) -> Any:
    return [] if value is None else value


UtcTime = Annotated[datetime | None, BeforeValidator(_blank_as_none), AfterValidator(_as_utc)]
# Platform-local wall-clock time (Athens), kept without a time zone.
LocalTime = Annotated[datetime | None, BeforeValidator(_blank_as_none)]


def format_api_date(when: datetime) -> str:
    """Format as "21 May 2013 18:14:56" (UTC), as LastDateReceivedUtc and similar expect.

    Month names are fixed English abbreviations, whatever the computer's language.
    """
    if when.tzinfo is None:
        raise ValueError("pass a time-zone aware datetime (UTC)")
    t = when.astimezone(UTC)
    return f"{t.day:02d} {_MONTHS[t.month - 1]} {t.year} {t:%H:%M:%S}"


def _iso_utc(when: datetime) -> str:
    if when.tzinfo is None:
        raise ValueError("pass a time-zone aware datetime (UTC)")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


# --- Models (exact API field names as aliases) ----------------------------------------------


class _ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore", frozen=True)


class UnitRef(_ApiModel):
    uid: str | None = Field(None, alias="Uid")
    name: str | None = Field(None, alias="Name")
    imei: str | None = Field(None, alias="Imei")


class Driver(_ApiModel):
    uid: str | None = Field(None, alias="Uid")
    first_name: str | None = Field(None, alias="FirstName")
    last_name: str | None = Field(None, alias="LastName")
    code: str | None = Field(None, alias="Code")


class PointOfInterest(_ApiModel):
    uid: str | None = Field(None, alias="Uid")
    name: str | None = Field(None, alias="Name")


class InputOutput(_ApiModel):
    system_name: str | None = Field(None, alias="SystemName")
    description: str | None = Field(None, alias="Description")
    user_description: str | None = Field(None, alias="UserDescription")
    active: bool = Field(False, alias="Active")


class Position(_ApiModel):
    unit: UnitRef | None = Field(None, alias="Unit")
    latitude: float = Field(alias="Latitude")
    longitude: float = Field(alias="Longitude")
    address: str | None = Field(None, alias="Address")
    speed: float = Field(0.0, alias="Speed")
    speed_measure: str | None = Field(None, alias="SpeedMeasure")
    heading: float = Field(0.0, alias="Heading")
    ignition: str | None = Field(None, alias="Ignition")
    odometer: float = Field(0.0, alias="Odometer")
    engine_time: float = Field(0.0, alias="EngineTime")
    engine_status: str | None = Field(None, alias="EngineStatus")
    server_time_utc: UtcTime = Field(None, alias="ServerTimeUTC")
    gps_time_local: LocalTime = Field(None, alias="GPSTimeLocal")
    gps_time_utc: UtcTime = Field(None, alias="GPSTimeUtc")
    driver: Driver | None = Field(None, alias="Driver")
    point_of_interest: PointOfInterest | None = Field(None, alias="PointOfInterest")
    input_outputs: Annotated[list[InputOutput], BeforeValidator(_none_as_empty)] = Field(
        default_factory=list, alias="InputOutputs"
    )


class SensorReading(_ApiModel):
    unit_uid: str | None = Field(None, alias="UnitUid")
    name: str | None = Field(None, alias="Name")
    value: str | None = Field(None, alias="Value")  # always text, e.g. "12.46"
    measurement_sign: str | None = Field(None, alias="MeasurementSign")
    sensor_type: str | None = Field(None, alias="SensorType")
    reading_time_local: LocalTime = Field(None, alias="ReadingTimeLocal")
    reading_time_utc: UtcTime = Field(None, alias="ReadingTimeUtc")
    server_time_utc: UtcTime = Field(None, alias="ServerTimeUtc")


class AssetItem(_ApiModel):
    asset_name: str | None = Field(None, alias="AssetName")
    asset_type: str | None = Field(None, alias="AssetType")


class Unit(_ApiModel):
    uid: str | None = Field(None, alias="Uid")
    name: str | None = Field(None, alias="Name")
    imei: str | None = Field(None, alias="Imei")
    status: str | None = Field(None, alias="Status")
    group_name: str | None = Field(None, alias="GroupName")
    company_name: str | None = Field(None, alias="CompanyName")
    company_uid: str | None = Field(None, alias="CompanyUid")
    phone_number: str | None = Field(None, alias="PhoneNumber")
    unit_type: str | None = Field(None, alias="UnitType")
    created_date_time_utc: UtcTime = Field(None, alias="CreatedDateTimeUtc")


class UnitAtPosition(Unit):
    position: Position | None = Field(None, alias="Position")
    sensor_readings: Annotated[list[SensorReading], BeforeValidator(_none_as_empty)] = Field(
        default_factory=list, alias="SensorReadings"
    )
    assets: Annotated[list[AssetItem], BeforeValidator(_none_as_empty)] = Field(
        default_factory=list, alias="Assets"
    )


class UnitLatestPosition(UnitAtPosition):
    last_reported_time_local: LocalTime = Field(None, alias="LastReportedTimeLocal")
    last_reported_time_utc: UtcTime = Field(None, alias="LastReportedTimeUTC")


class AlertType(_ApiModel):
    uid: str | None = Field(None, alias="UID")
    name: str | None = Field(None, alias="Name")


class Alert(_ApiModel):
    alert_uid: str | None = Field(None, alias="AlertUID")
    alert_name: str | None = Field(None, alias="AlertName")
    alert_type: str | None = Field(None, alias="AlertType")
    alert_type_uid: str | None = Field(None, alias="AlertTypeUID")
    vehicle: str | None = Field(None, alias="Vehicle")
    created_date: UtcTime = Field(None, alias="CreatedDate")  # assumed UTC: not confirmed
    alert_message: str | None = Field(None, alias="AlertMessage")
    alert_queue_id: int | None = Field(None, alias="AlertQueueId")


class _SessionToken(_ApiModel):
    user_id_guid: str = Field(alias="UserIdGuid", min_length=1)
    session_id: str = Field(alias="SessionId", min_length=1)


# --- Client ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Session:
    user_id_guid: str
    session_id: str
    created_at: datetime

    def __repr__(self) -> str:  # never show the ids
        return f"_Session(created_at={self.created_at.isoformat()})"


def _is_ok(body: dict[str, Any]) -> bool:
    status = body.get("Status") or {}
    return str(status.get("Result", "")).strip().lower() in _OK_RESULTS


def _is_session_error(body: dict[str, Any]) -> bool:
    message = str((body.get("Status") or {}).get("Message", "")).lower()
    return any(word in message for word in _SESSION_WORDS)


def _failure(body: dict[str, Any]) -> MobilityError:
    status = body.get("Status") or {}
    return MobilityError(str(status.get("ErrorCode", "")), str(status.get("Message", "")))


class MobilityClient:
    """Read-only access to units, positions, sensor readings and alerts."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str | SecretStr,
        *,
        timeout: float = 20,
        clock: Any = None,
    ) -> None:
        self._http = make_client(base_url, timeout=timeout)
        self._username = username
        self._password = password if isinstance(password, SecretStr) else SecretStr(password)
        self._session: _Session | None = None
        self._now = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return f"MobilityClient(base_url={str(self._http.base_url)!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- transport -------------------------------------------------------------------------

    def _send(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """One HTTP call (with shared retries). HTTP and network failures become MobilityError."""
        try:
            response = request_with_retry(self._http, method, path, params=params)
        except httpx.HTTPStatusError as exc:  # message is already free of the query string
            raise MobilityError(f"HTTP_{exc.response.status_code}", str(exc)) from None
        except httpx.TransportError as exc:  # the request repr would hold the URL: drop it
            raise MobilityError(type(exc).__name__, f"{method} {path} failed") from None
        try:
            body = response.json()
        except ValueError:
            raise MobilityError("INVALID_RESPONSE", f"{method} {path}: not JSON") from None
        if not isinstance(body, dict) or "Status" not in body:
            raise MobilityError("INVALID_RESPONSE", f"{method} {path}: no Status")
        return body

    def authenticate(self) -> None:
        """Log in and keep UserIdGuid and SessionId in memory (never logged)."""
        params = {"username": self._username, "password": self._password.get_secret_value()}
        body = self._send("POST", AUTH_PATH, params)
        if not _is_ok(body):
            self._session = None
            raise _failure(body)
        token = _SessionToken.model_validate(body.get("Result") or {})
        self._session = _Session(token.user_id_guid, token.session_id, self._now())
        log.info("mobility_authenticated")

    def _session_too_old(self) -> bool:
        return self._session is None or self._now() - self._session.created_at >= SESSION_MAX_AGE

    def _get_once(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._session is not None
        query = {k: v for k, v in params.items() if v is not None}
        query["UserIdGuid"] = self._session.user_id_guid
        query["SessionId"] = self._session.session_id
        return self._send("GET", path, query)

    def _get(self, path: str, **params: Any) -> Any:
        """GET with the session added. Renews the session at most once per call."""
        if self._session_too_old():
            self.authenticate()
        body = self._get_once(path, params)
        if not _is_ok(body) and _is_session_error(body):
            log.info("mobility_session_renewed", path=path)
            self.authenticate()
            body = self._get_once(path, params)
        if not _is_ok(body):
            raise _failure(body)
        return body.get("Result")

    # -- endpoints (all GET) -----------------------------------------------------------------

    def list_units(self) -> list[Unit]:
        return [Unit.model_validate(u) for u in self._get(f"{API}/units/unit/list") or []]

    def latest_positions(
        self, since: datetime | None = None, unit_uid: str | None = None
    ) -> list[UnitLatestPosition]:
        """Latest position per unit; `since` keeps only units that reported after it."""
        rows = self._get(
            f"{API}/units/latestpositionslist",
            LastDateReceivedUtc=format_api_date(since) if since else None,
            UnitUid=unit_uid,
        )
        return [UnitLatestPosition.model_validate(r) for r in rows or []]

    def positions(
        self,
        unit_uids: Sequence[str] | None = None,
        start_id: int | None = None,
        start_hour_utc: datetime | None = None,
        include_io: bool = False,
    ) -> tuple[list[Position], int | None, bool]:
        """(positions, next_start_id, is_current). No start_id = last 24 h; 0 = last 7 days.

        If is_current is False there is more to read: call again with next_start_id.
        """
        result = self._get(
            f"{API}/data/positionslist",
            Uid=",".join(unit_uids) if unit_uids else "*",
            StartId=start_id,
            StartHourUtc=_iso_utc(start_hour_utc) if start_hour_utc else None,
            IncludeInputOutputs="true" if include_io else "false",
        )
        result = result or {}
        rows = [Position.model_validate(p) for p in result.get("Position") or []]
        return rows, result.get("StartId"), bool(result.get("IsCurrent", True))

    def sensor_readings(
        self, unit_uids: Sequence[str] | None = None, start_id: int | None = None
    ) -> tuple[list[SensorReading], int | None]:
        """(readings, next_start_id). No start_id = last 24 h; 0 = last 7 days."""
        result = self._get(
            f"{API}/data/sensorreadingslist",
            Uid=",".join(unit_uids) if unit_uids else "*",
            StartId=start_id,
        )
        result = result or {}
        rows = [SensorReading.model_validate(r) for r in result.get("SensorReadings") or []]
        return rows, result.get("StartId")

    def alert_types(self) -> list[AlertType]:
        rows = self._get(f"{API}/alerts/alerts/alerttypelist")
        return [AlertType.model_validate(t) for t in rows or []]

    def alerts(
        self, alert_type_uid: str | None = None, start_uid: str | None = None
    ) -> tuple[list[Alert], str | None]:
        """(alerts, next_start_uid). No start_uid = last 24 h; "0" = last 7 days."""
        result = self._get(
            f"{API}/alerts/alerts/list", AlertTypeUID=alert_type_uid, StartUID=start_uid
        )
        result = result or {}
        rows = [Alert.model_validate(a) for a in result.get("AlertList") or []]
        return rows, result.get("StartUID")

    def position_at_time(self, uid: str, when: datetime) -> UnitAtPosition:
        result = self._get(
            f"{API}/units/{quote(uid, safe='')}/positionattime",
            PointInTimeDateTimeUTC=format_api_date(when),
        )
        return UnitAtPosition.model_validate(result or {})


def get_mobility_client() -> MobilityClient:
    """The local mock (http://localhost:8099, mock/mock) unless USE_MOCKS=false."""
    settings = get_settings()
    if settings.use_mocks:
        return MobilityClient(MOCK_BASE_URL, MOCK_USERNAME, MOCK_PASSWORD)
    missing = [
        name
        for name, value in (
            ("MOBILITY_BASE_URL", settings.mobility_base_url),
            ("MOBILITY_USERNAME", settings.mobility_username),
            ("MOBILITY_PASSWORD", settings.mobility_password),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing settings for the real Mobility API: {', '.join(missing)}")
    assert settings.mobility_password is not None
    return MobilityClient(
        settings.mobility_base_url, settings.mobility_username, settings.mobility_password
    )

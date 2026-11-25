"""Tests for connectors/mobility.py: against the running mock server, and with respx fakes."""

import inspect
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
import respx
import uvicorn
from structlog.testing import capture_logs

from connectors import mobility
from connectors.mobility import (
    Alert,
    MobilityClient,
    MobilityError,
    Position,
    UnitLatestPosition,
    format_api_date,
)
from mock_api.mobility import routes
from mock_api.server import app
from shared import http
from shared.config import Settings

# --- Mock server fixture --------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    """Run the mock API in a background thread for the tests in this module."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(server_url: str) -> Iterator[MobilityClient]:
    with MobilityClient(server_url, "mock", "mock") as c:
        yield c


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Retries in shared/http.py happen instantly; the waits are recorded."""
    waits: list[float] = []
    monkeypatch.setattr(http, "_sleep", waits.append)
    return waits


# --- Against the mock server ----------------------------------------------------------------


def test_list_units(client: MobilityClient) -> None:
    units = client.list_units()
    assert [u.name for u in units] == [f"VEH_{n:03d}" for n in range(1, 21)]
    assert units[0].created_date_time_utc is not None
    assert units[0].created_date_time_utc.tzinfo is UTC


def test_latest_positions_are_typed(client: MobilityClient) -> None:
    latest = {lp.name: lp for lp in client.latest_positions()}
    assert len(latest) == 20 and all(isinstance(lp, UnitLatestPosition) for lp in latest.values())
    power_cut = latest["VEH_011"]
    assert power_cut.position is not None
    active = {io.system_name for io in power_cut.position.input_outputs if io.active}
    assert active == {"externalpowerfailure"}
    readings = {r.name: r.value for r in power_cut.sensor_readings}
    assert readings["External Battery"] == "0.00"
    assert power_cut.last_reported_time_utc is not None
    assert power_cut.last_reported_time_utc.tzinfo is UTC
    assert power_cut.last_reported_time_local is not None  # Athens wall-clock, no zone
    assert power_cut.position.speed_measure == "kph"


def test_latest_positions_since_and_one_unit(client: MobilityClient) -> None:
    every = client.latest_positions()
    cutoff = every[-3].last_reported_time_utc
    assert cutoff is not None
    newer = client.latest_positions(since=cutoff - timedelta(seconds=1))
    assert [lp.uid for lp in newer] == [lp.uid for lp in every[-3:]]
    assert [lp.uid for lp in client.latest_positions(unit_uid=every[0].uid)] == [every[0].uid]


def test_positions_cursor(client: MobilityClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "POSITIONS_PAGE", 1000)  # the server runs in this process
    first, next_id, current = client.positions(start_id=0)
    assert len(first) == 1000 and current is False and isinstance(first[0], Position)
    assert first[0].unit is not None and first[0].unit.uid
    second, next_id2, current2 = client.positions(start_id=next_id)
    assert current2 is True and len(first) + len(second) == 1818
    assert client.positions(start_id=next_id2) == ([], next_id2, True)


def test_positions_for_some_units_from_an_hour(client: MobilityClient) -> None:
    rows, _, _ = client.positions(start_id=0)
    uid = rows[0].unit.uid if rows[0].unit else None
    assert uid
    mine = [p for p in rows if p.unit and p.unit.uid == uid]
    start = mine[len(mine) // 2].gps_time_utc
    assert start is not None
    later, _, _ = client.positions([uid], start_hour_utc=start, include_io=True)
    assert later and all(p.gps_time_utc and p.gps_time_utc >= start for p in later)


def test_sensor_readings_cursor(client: MobilityClient) -> None:
    rows, next_id = client.sensor_readings(start_id=0)
    assert len({r.unit_uid for r in rows}) == 20
    assert all(r.reading_time_utc and r.reading_time_utc.tzinfo is UTC for r in rows)
    assert client.sensor_readings(start_id=next_id) == ([], next_id)


def test_alert_types_and_alerts_cursor(client: MobilityClient) -> None:
    types = {t.name: t.uid for t in client.alert_types()}
    assert set(types) == {"Power Cut", "Jamming", "Tamper", "Geofence Entry", "Panic"}
    alerts, next_uid = client.alerts(start_uid="0")
    assert all(isinstance(a, Alert) for a in alerts) and next_uid
    assert client.alerts(start_uid=next_uid) == ([], next_uid)
    jamming, _ = client.alerts(alert_type_uid=types["Jamming"], start_uid="0")
    assert jamming and {a.vehicle for a in jamming} == {"VEH_009"}


def test_position_at_time(client: MobilityClient) -> None:
    rows, _, _ = client.positions(start_id=0)
    target = rows[300]
    assert target.unit and target.unit.uid and target.gps_time_utc
    found = client.position_at_time(target.unit.uid, target.gps_time_utc)
    assert found.position is not None
    assert found.position.gps_time_utc == target.gps_time_utc
    assert found.name == target.unit.name


def test_unknown_unit_raises_mobility_error(client: MobilityClient) -> None:
    with pytest.raises(MobilityError) as err:
        client.position_at_time("NOPE00", datetime.now(UTC))
    assert err.value.error_code == routes.ERROR_CODES["UnitNotFound"]


def test_server_forgetting_the_session_triggers_one_login(client: MobilityClient) -> None:
    client.list_units()
    routes.expire_all_sessions()
    assert len(client.list_units()) == 20  # logged in again by itself


def test_session_older_than_23_hours_is_renewed_before_the_call(server_url: str) -> None:
    now = [datetime(2026, 11, 25, 12, 0, tzinfo=UTC)]
    with MobilityClient(server_url, "mock", "mock", clock=lambda: now[0]) as c:
        c.list_units()
        first = c._session
        now[0] += timedelta(hours=22, minutes=59)
        c.list_units()
        assert c._session is first  # still young enough
        now[0] += timedelta(minutes=1)
        c.list_units()
        assert c._session is not first


def test_wrong_password(server_url: str) -> None:
    with MobilityClient(server_url, "mock", "wrong") as c, pytest.raises(MobilityError) as err:
        c.list_units()
    assert err.value.error_code == routes.ERROR_CODES["InvalidCredentials"]


def test_secrets_never_reach_logs_or_repr(server_url: str) -> None:
    with (
        capture_logs() as logs,
        MobilityClient(server_url, "mock", "s3cret-pass") as c,
        pytest.raises(MobilityError),
    ):
        c.list_units()
    text = repr(logs) + repr(c)
    assert "s3cret-pass" not in text and "password" not in text.lower()


# --- respx fakes for error cases ------------------------------------------------------------

BASE = "https://mobility.test"
AUTH_OK = {"Status": {"Result": "ok", "ErrorCode": "0", "Message": "ok"},
           "Result": {"UserIdGuid": "user-guid-1", "SessionId": "session-guid-1"}}  # fmt: skip
UNITS_OK = {"Status": {"Result": "ok", "ErrorCode": "0", "Message": "ok"},
            "Result": [{"Uid": "ABC123", "Name": "VEH_001"}]}  # fmt: skip


def failed(code: str, message: str) -> dict:
    return {"Status": {"Result": "Failed", "ErrorCode": code, "Message": message}, "Result": None}


@pytest.fixture
def fake() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.post("/api/v1.0/authentication/userauthenticate").respond(json=AUTH_OK)
        yield router


def test_login_sends_post_with_query_parameters(fake: respx.MockRouter) -> None:
    units = fake.get("/api/v1.0/units/unit/list").respond(json=UNITS_OK)
    MobilityClient(BASE, "user", "pass").list_units()
    login = fake.calls[0].request
    assert login.method == "POST"
    assert dict(login.url.params) == {"username": "user", "password": "pass"}
    sent = dict(units.calls[0].request.url.params)
    assert sent == {"UserIdGuid": "user-guid-1", "SessionId": "session-guid-1"}


def test_expired_session_logs_in_again_and_retries_once(fake: respx.MockRouter) -> None:
    units = fake.get("/api/v1.0/units/unit/list")
    units.side_effect = [
        httpx.Response(200, json=failed("12", "Session has expired")),
        httpx.Response(200, json=UNITS_OK),
    ]
    assert [u.uid for u in MobilityClient(BASE, "u", "p").list_units()] == ["ABC123"]
    logins = [c for c in fake.calls if c.request.method == "POST"]
    assert len(logins) == 2 and units.call_count == 2


def test_session_error_twice_gives_up(fake: respx.MockRouter) -> None:
    fake.get("/api/v1.0/units/unit/list").respond(json=failed("12", "Invalid session"))
    with pytest.raises(MobilityError) as err:
        MobilityClient(BASE, "u", "p").list_units()
    assert err.value.error_code == "12"
    assert len([c for c in fake.calls if c.request.method == "POST"]) == 2  # no endless loop


def test_failed_status_raises_without_logging_in_again(fake: respx.MockRouter) -> None:
    fake.get("/api/v1.0/units/unit/list").respond(json=failed("7", "Unit not found"))
    with pytest.raises(MobilityError) as err:
        MobilityClient(BASE, "u", "p").list_units()
    assert (err.value.error_code, err.value.message) == ("7", "Unit not found")
    assert len([c for c in fake.calls if c.request.method == "POST"]) == 1


def test_429_is_retried_then_succeeds(fake: respx.MockRouter, no_wait: list[float]) -> None:
    units = fake.get("/api/v1.0/units/unit/list")
    units.side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json=UNITS_OK),
    ]
    assert len(MobilityClient(BASE, "u", "p").list_units()) == 1
    assert no_wait == [2.0]


def test_429_that_never_ends_raises(fake: respx.MockRouter, no_wait: list[float]) -> None:
    fake.get("/api/v1.0/units/unit/list").respond(429)
    with pytest.raises(MobilityError) as err:
        MobilityClient(BASE, "u", "p").list_units()
    assert err.value.error_code == "HTTP_429"
    assert len(no_wait) == http.MAX_ATTEMPTS - 1


def test_timeout_is_retried_then_raises(fake: respx.MockRouter, no_wait: list[float]) -> None:
    units = fake.get("/api/v1.0/units/unit/list").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(MobilityError) as err:
        MobilityClient(BASE, "u", "p").list_units()
    assert err.value.error_code == "ReadTimeout"
    assert units.call_count == http.MAX_ATTEMPTS
    assert "session-guid-1" not in str(err.value) and "SessionId" not in str(err.value)


def test_errors_never_contain_the_password(fake: respx.MockRouter) -> None:
    fake.post("/api/v1.0/authentication/userauthenticate").respond(500)
    with pytest.raises(MobilityError) as err, capture_logs() as logs:
        MobilityClient(BASE, "u", "pa55word").list_units()
    assert "pa55word" not in str(err.value) and "pa55word" not in repr(logs)


def test_api_date_format_is_english_and_utc() -> None:
    athens_summer = timezone(timedelta(hours=3))  # EEST, so the result must move 3 hours
    when = datetime(2013, 5, 21, 21, 14, 56, tzinfo=athens_summer)
    assert format_api_date(when) == "21 May 2013 18:14:56"
    with pytest.raises(ValueError):
        format_api_date(datetime(2013, 5, 21, 18, 14, 56))  # no time zone


# --- Read-only guarantee and factory --------------------------------------------------------


def test_only_authentication_uses_post() -> None:
    source = inspect.getsource(mobility)
    assert source.count('"POST"') == 1  # inside authenticate()
    public = {n for n, _ in inspect.getmembers(MobilityClient, inspect.isfunction)
              if not n.startswith("_")}  # fmt: skip
    assert public == {"authenticate", "close", "list_units", "latest_positions", "positions",
                      "sensor_readings", "alert_types", "alerts", "position_at_time"}  # fmt: skip


def test_factory_uses_the_mock_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mobility, "get_settings", lambda: Settings(_env_file=None))
    client = mobility.get_mobility_client()
    assert str(client._http.base_url).rstrip("/") == "http://localhost:8099"


def test_factory_live_needs_all_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, use_mocks=False, mobility_base_url="https://x.test")
    monkeypatch.setattr(mobility, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="MOBILITY_USERNAME, MOBILITY_PASSWORD"):
        mobility.get_mobility_client()

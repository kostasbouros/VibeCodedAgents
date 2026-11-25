"""Tests for shared/http.py. Waits are recorded instead of slept, so tests run instantly."""

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from shared import http

BASE = "https://api.example.test"


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(http, "_sleep", recorded.append)
    return recorded


@pytest.fixture
def client() -> httpx.Client:
    with http.make_client(BASE) as c:
        yield c


def test_make_client_sets_base_url_and_timeout() -> None:
    with http.make_client(BASE, timeout=5) as c:
        assert str(c.base_url) == BASE
        assert c.timeout.read == 5


@respx.mock(base_url=BASE)
def test_429_then_200_is_retried(respx_mock, client, waits) -> None:
    route = respx_mock.get("/tickets").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    response = http.request_with_retry(client, "GET", "/tickets")
    assert response.json() == {"ok": True}
    assert route.call_count == 2
    assert len(waits) == 1 and 1.0 <= waits[0] <= 1.5  # 1s backoff + up to 0.5s jitter


@respx.mock(base_url=BASE)
def test_timeout_then_200_is_retried(respx_mock, client, waits) -> None:
    route = respx_mock.get("/devices").mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)]
    )
    assert http.request_with_retry(client, "GET", "/devices").status_code == 200
    assert route.call_count == 2
    assert len(waits) == 1


@respx.mock(base_url=BASE)
def test_400_is_not_retried(respx_mock, client, waits) -> None:
    route = respx_mock.post("/tickets").mock(return_value=httpx.Response(400))
    with pytest.raises(httpx.HTTPStatusError) as err:
        http.request_with_retry(client, "POST", "/tickets", json={"a": 1})
    assert err.value.response.status_code == 400
    assert route.call_count == 1
    assert waits == []


@respx.mock(base_url=BASE)
def test_retry_after_header_is_honoured(respx_mock, client, waits) -> None:
    respx_mock.get("/x").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    http.request_with_retry(client, "GET", "/x")
    assert waits == [7.0]


@respx.mock(base_url=BASE)
def test_gives_up_after_four_attempts(respx_mock, client, waits) -> None:
    route = respx_mock.get("/x").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError) as err:
        http.request_with_retry(client, "GET", "/x")
    assert err.value.response.status_code == 503
    assert route.call_count == 4
    assert [int(w) for w in waits] == [1, 2, 4]  # backoff without the jitter


@respx.mock(base_url=BASE)
def test_logs_never_contain_query_string_or_headers(respx_mock, client, waits) -> None:
    respx_mock.get("/accounts").mock(return_value=httpx.Response(200))
    with capture_logs() as logs:
        http.request_with_retry(
            client,
            "GET",
            "/accounts?access_token=FAKE-TOKEN",
            params={"api_key": "FAKE-KEY"},
            headers={"Authorization": "Bearer FAKE-BEARER"},
        )
    assert logs == [
        {
            "event": "http_request",
            "log_level": "info",
            "method": "GET",
            "path": "/accounts",
            "status": 200,
            "duration_ms": logs[0]["duration_ms"],
        }
    ]
    assert "FAKE" not in str(logs)


# --- Found in review -----------------------------------------------------------------------


@respx.mock(base_url=BASE)
def test_error_message_never_contains_query_string(respx_mock, client, waits) -> None:
    respx_mock.get("/x").mock(return_value=httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError) as err:
        http.request_with_retry(client, "GET", "/x?access_token=FAKE-TOKEN")
    assert str(err.value) == "HTTP 401 for GET /x"


@respx.mock(base_url=BASE)
def test_final_5xx_error_message_never_contains_query_string(respx_mock, client, waits) -> None:
    respx_mock.get("/x").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError) as err:
        http.request_with_retry(client, "GET", "/x", params={"api_key": "FAKE-KEY"})
    assert "FAKE" not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__


@respx.mock(base_url=BASE)
def test_post_is_not_retried_after_read_timeout(respx_mock, client, waits) -> None:
    # The server may already have stored the write; sending it again could duplicate it.
    route = respx_mock.post("/comments").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        http.request_with_retry(client, "POST", "/comments", json={"text": "hi"})
    assert route.call_count == 1


@respx.mock(base_url=BASE)
def test_post_is_not_retried_after_500(respx_mock, client, waits) -> None:
    route = respx_mock.post("/comments").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        http.request_with_retry(client, "POST", "/comments")
    assert route.call_count == 1


@respx.mock(base_url=BASE)
def test_post_is_retried_when_it_never_connected(respx_mock, client, waits) -> None:
    route = respx_mock.post("/comments").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(201)]
    )
    assert http.request_with_retry(client, "POST", "/comments").status_code == 201
    assert route.call_count == 2


@respx.mock(base_url=BASE)
def test_post_is_retried_on_429(respx_mock, client, waits) -> None:
    route = respx_mock.post("/comments").mock(
        side_effect=[httpx.Response(429), httpx.Response(201)]
    )
    assert http.request_with_retry(client, "POST", "/comments").status_code == 201
    assert route.call_count == 2


@respx.mock(base_url=BASE)
def test_very_long_retry_after_gives_up_instead_of_sleeping(respx_mock, client, waits) -> None:
    route = respx_mock.get("/x").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "86400"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        http.request_with_retry(client, "GET", "/x")
    assert route.call_count == 1
    assert waits == []  # never slept for a day


@respx.mock(base_url=BASE)
def test_retry_after_as_http_date(respx_mock, client, waits) -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    respx_mock.get("/x").mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": when}), httpx.Response(200)]
    )
    http.request_with_retry(client, "GET", "/x")
    assert len(waits) == 1 and 25 <= waits[0] <= 31


@respx.mock(base_url=BASE)
def test_patch_is_not_retried_after_read_timeout(respx_mock, client, waits) -> None:
    route = respx_mock.patch("/tickets/1").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        http.request_with_retry(client, "PATCH", "/tickets/1", json={"status": "x"})
    assert route.call_count == 1


@respx.mock(base_url=BASE)
def test_post_is_retried_after_pool_timeout(respx_mock, client, waits) -> None:
    route = respx_mock.post("/comments").mock(
        side_effect=[httpx.PoolTimeout("no free connection"), httpx.Response(201)]
    )
    assert http.request_with_retry(client, "POST", "/comments").status_code == 201
    assert route.call_count == 2

"""Shared HTTP client with safe retries and credential-free logging."""

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
import structlog
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from shared.limits import limits

log = structlog.get_logger(__name__)

MAX_ATTEMPTS = limits().http.max_attempts
BASE_WAIT_SECONDS = limits().http.base_wait_seconds  # waits double: 1s, 2s, 4s...
MAX_JITTER_SECONDS = limits().http.max_jitter_seconds
# If a server's Retry-After asks for longer than this, give up instead of sleeping.
MAX_RETRY_AFTER_SECONDS = limits().http.max_retry_after_seconds

_sleep = time.sleep  # replaced in tests so they run instantly


class _RetryableResponse(Exception):
    """Internal: a 429 or 5xx response that is worth trying again."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


def make_client(base_url: str, timeout: float = 20) -> httpx.Client:
    """Create an HTTP client for one external system."""
    return httpx.Client(base_url=base_url, timeout=timeout)


def _safe_path(path: str) -> str:
    """Path without query string or host, safe to log."""
    return urlsplit(path).path or "/"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read Retry-After as seconds (a number or an HTTP date). None if absent or invalid."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


# Methods that are safe to send twice. Others (POST, PATCH) are retried only when the
# request certainly never reached the server, or on 429 (the server refused to process it).
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _retry_check(method: str) -> Callable[[BaseException], bool]:
    """Build the 'should we retry?' rule for this method."""
    idempotent = method in _IDEMPOTENT_METHODS

    def is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, _RetryableResponse):
            return idempotent or exc.response.status_code == 429
        if isinstance(exc, httpx.TransportError):
            return idempotent or isinstance(exc, _NOT_SENT_ERRORS)
        return False

    return is_retryable


def _status_error(method: str, path: str, response: httpx.Response) -> httpx.HTTPStatusError:
    """Like response.raise_for_status(), but the message never contains the query string."""
    message = f"HTTP {response.status_code} for {method} {_safe_path(path)}"
    return httpx.HTTPStatusError(message, request=response.request, response=response)


def _retry_after_too_long(state: RetryCallState) -> bool:
    """Stop retrying when the server asks us to wait longer than MAX_RETRY_AFTER_SECONDS."""
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, _RetryableResponse):
        retry_after = _retry_after_seconds(exc.response)
        return retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS
    return False


def _wait(state: RetryCallState) -> float:
    """Retry-After if the server sent one, otherwise exponential backoff plus jitter."""
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, _RetryableResponse):
        retry_after = _retry_after_seconds(exc.response)
        if retry_after is not None:
            return retry_after
    backoff = BASE_WAIT_SECONDS * 2 ** (state.attempt_number - 1)
    return backoff + random.uniform(0, MAX_JITTER_SECONDS)


def request_with_retry(
    client: httpx.Client, method: str, path: str, **kwargs: object
) -> httpx.Response:
    """Send a request, retrying connection errors, timeouts, 429 and 5xx.

    GET/HEAD/OPTIONS/PUT/DELETE retry on all of those. POST/PATCH retry only on 429 and on
    errors where the request never left (connect errors/timeouts), so a write is never
    sent twice. Returns the response on success. Raises httpx.HTTPStatusError (message
    without the query string) for other 4xx at once, or for the last 429/5xx. Raises
    httpx.TransportError if the last attempt could not connect or timed out.
    """
    method = method.upper()
    retrying = Retrying(
        stop=stop_after_attempt(MAX_ATTEMPTS) | _retry_after_too_long,
        wait=_wait,
        retry=retry_if_exception(_retry_check(method)),
        sleep=lambda seconds: _sleep(seconds),
        reraise=True,
    )
    try:
        return retrying(_send_once, client, method, path, kwargs)
    except _RetryableResponse as exc:
        raise _status_error(method, path, exc.response) from None


def _send_once(
    client: httpx.Client, method: str, path: str, kwargs: dict[str, object]
) -> httpx.Response:
    """One attempt. Logs only method, path (no query), status and duration."""
    started = time.perf_counter()
    try:
        response = client.request(method, path, **kwargs)  # type: ignore[arg-type]
    except httpx.TransportError as exc:
        _log(method, path, None, started, error=type(exc).__name__)
        raise
    _log(method, path, response.status_code, started)
    if response.status_code == 429 or response.status_code >= 500:
        raise _RetryableResponse(response)
    if response.is_error:
        raise _status_error(method, path, response)
    return response


def _log(method: str, path: str, status: int | None, started: float, **extra: str) -> None:
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    level = log.info if status is not None and status < 400 else log.warning
    level(
        "http_request",
        method=method,
        path=_safe_path(path),
        status=status,
        duration_ms=duration_ms,
        **extra,
    )

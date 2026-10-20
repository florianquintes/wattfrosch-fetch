"""Base HTTP layer for all clients (retry, backoff, rate limiting, caching).

Shared transport behavior on top of a sync ``httpx.Client``:

- retry with exponential backoff + jitter (tenacity) on transport errors,
  5xx and 429; ``Retry-After`` on 429 is honored before falling back to
  the backoff schedule;
- per-source rate limiting with config values (see
  :data:`DEFAULT_RATE_LIMITS`), never hardcoded in the limiter itself;
- strict JSON parsing and a validation hook: deviations from the expected
  format are reported as :class:`FetchValidationError`, never silently
  coerced (fetch is strict; contracts is lenient);
- response caching is intentionally not implemented in v1 (Milestone 0
  decision: default OFF, hishel backend later).

All failures surface through the typed exception hierarchy so the CLI can
map them to exit codes:

- 0 success; 1 config/usage error (:class:`FetchConfigError`);
- 2 HTTP/transport error after retries (:class:`FetchHTTPError`);
- 3 response validation error (:class:`FetchValidationError`).
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from wattfrosch_fetch.contracts import MeasurementRecord


class FetchError(Exception):
    """Base class for all wattfrosch-fetch errors."""


class FetchConfigError(FetchError):
    """Configuration/usage error -> CLI exit code 1."""


class FetchHTTPError(FetchError):
    """HTTP/transport error after retries exhausted -> CLI exit code 2."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FetchValidationError(FetchError):
    """Response deviates from the expected API format -> CLI exit code 3."""


class FetchSettings(BaseSettings):
    """Runtime settings, overridable via ``WATTFROSCH_FETCH_*`` env vars."""

    model_config = SettingsConfigDict(env_prefix="WATTFROSCH_FETCH_")

    http_timeout_seconds: float = 30.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 1.0
    cache_enabled: bool = False
    cache_ttl_seconds: float = 300.0
    archive_lag_days: int = 7
    forecast_horizon_days: int = 16


@dataclass(frozen=True)
class RateLimit:
    """Max requests allowed per sliding time window for one source."""

    requests: int
    window_seconds: float


DEFAULT_RATE_LIMITS: Final[dict[str, RateLimit]] = {
    "open-meteo": RateLimit(requests=60, window_seconds=60.0),
    "energy-charts": RateLimit(requests=2, window_seconds=60.0),
    "smard": RateLimit(requests=30, window_seconds=60.0),
}

_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset(
    {429, *range(500, 600)},
)


class _RetryableHTTPError(FetchHTTPError):
    """Internal marker so tenacity retries only the retryable failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header in seconds; unparseable -> None."""
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, _RetryableHTTPError | httpx.TransportError)


def _make_wait(settings: FetchSettings) -> Callable[[RetryCallState], float]:
    """Exponential backoff with jitter; 429 ``Retry-After`` takes priority."""

    def wait(retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, _RetryableHTTPError) and exc.retry_after_seconds is not None:
            return exc.retry_after_seconds
        backoff = settings.http_backoff_base_seconds * (
            2 ** (retry_state.attempt_number - 1)
        )
        return random.uniform(0.0, backoff)

    return wait


def _default_rate_limit(source: str) -> RateLimit:
    try:
        return DEFAULT_RATE_LIMITS[source]
    except KeyError:
        raise FetchConfigError(
            f"no rate limit configured for source {source!r}",
        ) from None


class _SlidingWindowRateLimiter:
    """Sliding-window limiter; clock/sleep injectable for tests."""

    def __init__(
        self,
        limit: RateLimit,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._limit = limit
        self._clock = clock
        self._sleep = sleep
        self._timestamps: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            window_start = now - self._limit.window_seconds
            while self._timestamps and self._timestamps[0] <= window_start:
                self._timestamps.popleft()
            if len(self._timestamps) < self._limit.requests:
                self._timestamps.append(now)
                return
            wait = max(self._timestamps[0] + self._limit.window_seconds - now, 0.0)
            self._sleep(wait)


class BaseClient:
    """Shared HTTP behavior; one instance per source.

    Clients subclass this and call :meth:`get_json` for every endpoint.
    """

    def __init__(
        self,
        *,
        base_url: str,
        source: str,
        settings: FetchSettings | None = None,
        rate_limit: RateLimit | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings is None:
            settings = FetchSettings()
        if settings.cache_enabled:
            raise FetchConfigError(
                "response caching is not implemented in v1; "
                "keep cache_enabled=false (hishel backend planned)",
            )
        self._settings = settings
        self._client = httpx.Client(
            base_url=base_url,
            timeout=settings.http_timeout_seconds,
        )
        limit = rate_limit if rate_limit is not None else _default_rate_limit(source)
        self._rate_limiter = _SlidingWindowRateLimiter(
            limit,
            clock=clock,
            sleep=sleep,
        )
        self._retrying = Retrying(
            retry=retry_if_exception(_is_retryable),
            wait=_make_wait(settings),
            stop=stop_after_attempt(settings.http_max_retries + 1),
            reraise=True,
            sleep=sleep,
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def fetch_series(
        self,
        metric: str,
        region: str,
        start: datetime,
        end: datetime,
    ) -> Iterable[MeasurementRecord]:
        """Uniform fetch signature; implemented by each source client.

        Yields ``MeasurementRecord``s for the half-open window
        ``[start, end)``; missing values are missing rows.
        """
        raise NotImplementedError

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        validate: Callable[[Any], None] | None = None,
        not_found_ok: bool = False,
    ) -> Any:
        """GET ``url`` and return the parsed JSON body.

        Retries retryable failures (transport errors, 5xx, 429 honoring
        ``Retry-After``), raises :class:`FetchHTTPError` after exhaustion or
        immediately on other 4xx. With ``not_found_ok=True`` a 404 response
        returns ``None`` instead (sources reporting "no data for that
        zone/range" as empty result, not error). Non-JSON responses and
        deviations reported by ``validate`` raise
        :class:`FetchValidationError`.
        """
        self._rate_limiter.acquire()
        try:
            response = self._retrying(self._send, url, params=params)
        except httpx.TransportError as exc:
            raise FetchHTTPError(
                f"{url}: transport error after retries: {exc}"
            ) from exc
        except FetchHTTPError as exc:
            if not_found_ok and exc.status_code == 404:
                return None
            raise
        try:
            data = response.json()
        except ValueError as exc:
            raise FetchValidationError(f"{url}: response is not valid JSON") from exc
        if validate is not None:
            validate(data)
        return data

    def _send(self, url: str, params: dict[str, Any] | None) -> httpx.Response:
        response = self._client.get(url, params=params)
        if response.status_code in _RETRYABLE_STATUS_CODES:
            retry_after = None
            if response.status_code == 429:
                retry_after = _parse_retry_after(
                    response.headers.get("Retry-After"),
                )
            raise _RetryableHTTPError(
                f"{url}: HTTP {response.status_code} after retries",
                status_code=response.status_code,
                retry_after_seconds=retry_after,
            )
        if response.is_error:
            raise FetchHTTPError(
                f"{url}: HTTP {response.status_code}",
                status_code=response.status_code,
            )
        return response


__all__ = [
    "DEFAULT_RATE_LIMITS",
    "BaseClient",
    "FetchConfigError",
    "FetchError",
    "FetchHTTPError",
    "FetchSettings",
    "FetchValidationError",
    "RateLimit",
]

"""Tests for the base HTTP layer (respx only — no real API calls)."""

from datetime import UTC, datetime

import httpx
import pytest
import respx

from wattfrosch_fetch.http import (
    BaseClient,
    FetchConfigError,
    FetchHTTPError,
    FetchSettings,
    FetchValidationError,
    RateLimit,
)

BASE_URL = "https://api.example.test"


class FakeClock:
    """Injectable clock/sleep: sleeping advances the fake clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(**overrides: object) -> BaseClient:
    kwargs = {
        "base_url": BASE_URL,
        "source": "open-meteo",
        "settings": FetchSettings(http_backoff_base_seconds=0.0),
    }
    kwargs.update(overrides)
    return BaseClient(**kwargs)  # type: ignore[arg-type]


@respx.mock
def test_get_json_returns_parsed_data() -> None:
    route = respx.get(f"{BASE_URL}/data").respond(json={"foo": 1})
    client = _client()

    assert client.get_json("/data") == {"foo": 1}
    assert route.call_count == 1


@respx.mock
def test_retries_5xx_then_succeeds() -> None:
    route = respx.get(f"{BASE_URL}/data").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})],
    )
    client = _client()

    assert client.get_json("/data") == {"ok": True}
    assert route.call_count == 2


@respx.mock
def test_raises_http_error_after_retries_exhausted() -> None:
    settings = FetchSettings(http_backoff_base_seconds=0.0, http_max_retries=2)
    route = respx.get(f"{BASE_URL}/data").mock(
        side_effect=[httpx.Response(500)] * 3,
    )
    client = _client(settings=settings)

    with pytest.raises(FetchHTTPError):
        client.get_json("/data")
    assert route.call_count == 3


@respx.mock
def test_429_honors_retry_after() -> None:
    clock = FakeClock()
    route = respx.get(f"{BASE_URL}/data").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"ok": True}),
        ],
    )
    client = _client(clock=clock, sleep=clock.sleep)

    assert client.get_json("/data") == {"ok": True}
    assert clock.sleeps == [7.0]
    assert route.call_count == 2


@respx.mock
def test_404_not_retried() -> None:
    route = respx.get(f"{BASE_URL}/data").respond(404)
    client = _client()

    with pytest.raises(FetchHTTPError):
        client.get_json("/data")
    assert route.call_count == 1


@respx.mock
def test_invalid_json_raises_validation_error() -> None:
    respx.get(f"{BASE_URL}/data").respond(
        200,
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    client = _client()

    with pytest.raises(FetchValidationError):
        client.get_json("/data")


@respx.mock
def test_validate_hook_raises_validation_error() -> None:
    respx.get(f"{BASE_URL}/data").respond(json={"unexpected": True})
    client = _client()

    def validate(data: object) -> None:
        raise FetchValidationError("API deviation: unexpected key")

    with pytest.raises(FetchValidationError):
        client.get_json("/data", validate=validate)


@respx.mock
def test_rate_limiter_delays_requests_beyond_limit() -> None:
    clock = FakeClock()
    respx.get(f"{BASE_URL}/data").respond(json={})
    client = _client(
        rate_limit=RateLimit(requests=2, window_seconds=60.0),
        clock=clock,
        sleep=clock.sleep,
    )

    client.get_json("/data")
    client.get_json("/data")
    client.get_json("/data")

    assert clock.sleeps == [60.0]


@respx.mock
def test_cache_disabled_hits_api_twice() -> None:
    route = respx.get(f"{BASE_URL}/data").respond(json={})
    client = _client()

    client.get_json("/data")
    client.get_json("/data")

    assert route.call_count == 2


def test_cache_enabled_raises_config_error() -> None:
    with pytest.raises(FetchConfigError):
        _client(settings=FetchSettings(cache_enabled=True))


def test_unknown_source_raises_config_error() -> None:
    with pytest.raises(FetchConfigError):
        BaseClient(base_url=BASE_URL, source="unknown-source")


@respx.mock
def test_429_without_retry_after_falls_back_to_backoff() -> None:
    clock = FakeClock()
    route = respx.get(f"{BASE_URL}/data").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ],
    )
    client = _client(clock=clock, sleep=clock.sleep)

    assert client.get_json("/data") == {"ok": True}
    assert clock.sleeps == [0.0]
    assert route.call_count == 2


@respx.mock
def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    clock = FakeClock()
    route = respx.get(f"{BASE_URL}/data").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "soon"}),
            httpx.Response(200, json={"ok": True}),
        ],
    )
    client = _client(clock=clock, sleep=clock.sleep)

    assert client.get_json("/data") == {"ok": True}
    assert clock.sleeps == [0.0]
    assert route.call_count == 2


@respx.mock
def test_close_closes_underlying_http_client() -> None:
    client = _client()

    client.close()

    assert client._client.is_closed


def test_base_fetch_series_is_abstract() -> None:
    client = _client()

    with pytest.raises(NotImplementedError):
        client.fetch_series(
            "weather.temperature_2m",
            "DE-BERLIN",
            datetime(2026, 10, 1, tzinfo=UTC),
            datetime(2026, 10, 2, tzinfo=UTC),
        )


@respx.mock
def test_transport_error_after_retries_raises_http_error() -> None:
    settings = FetchSettings(http_backoff_base_seconds=0.0, http_max_retries=1)
    route = respx.get(f"{BASE_URL}/data").mock(
        side_effect=[httpx.ConnectError("boom")] * 2,
    )
    client = _client(settings=settings)

    with pytest.raises(FetchHTTPError, match="transport error after retries"):
        client.get_json("/data")
    assert route.call_count == 2

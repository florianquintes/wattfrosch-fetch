"""Base HTTP layer for all clients (retry, backoff, rate limiting, caching).

Filled in by the next issue; currently it defines the typed exception
hierarchy so clients and the CLI can map failures to exit codes:

- 0 success; 1 config/usage error (:class:`FetchConfigError`);
- 2 HTTP/transport error after retries (:class:`FetchHTTPError`);
- 3 response validation error (:class:`FetchValidationError`).
"""

from __future__ import annotations


class FetchError(Exception):
    """Base class for all wattfrosch-fetch errors."""


class FetchConfigError(FetchError):
    """Configuration/usage error -> CLI exit code 1."""


class FetchHTTPError(FetchError):
    """HTTP/transport error after retries exhausted -> CLI exit code 2."""


class FetchValidationError(FetchError):
    """Response deviates from the expected API format -> CLI exit code 3."""


__all__ = [
    "FetchConfigError",
    "FetchError",
    "FetchHTTPError",
    "FetchValidationError",
]

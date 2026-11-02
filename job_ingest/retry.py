"""Exponential-backoff retry decorator for network calls.

Retry policy (config-free, fixed by design): retry ONLY on timeouts,
connection errors, HTTP 429 and HTTP 5xx. Never retry on HTTP 400, 401,
403, 404, or on jobspy's "Invalid country string" ValueError.

Any other exception type (generic RequestException without a retryable
status, ValueError, parsing errors, ...) fails fast with no retry so a
run is never slowed by hopeless attempts.
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Callable, TypeVar

import requests

log = logging.getLogger(__name__)

T = TypeVar("T")

# HTTP statuses that are never worth retrying.
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404})


def _http_status(exc: BaseException) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) is not None:
        try:
            return int(resp.status_code)
        except (TypeError, ValueError):
            pass
    # requests.HTTPError string often embeds the code ("404 Client Error").
    import re as _re
    m = _re.search(r"\b(429|5\d\d|[45]0\d)\b", str(exc))
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def is_retryable(exc: BaseException) -> bool:
    """True only for timeouts, connection errors, HTTP 429 and HTTP 5xx."""
    # The jobspy unsupported-country error must never be retried: it is
    # deterministic per location, not transient.
    if isinstance(exc, ValueError) and "Invalid country string" in str(exc):
        return False
    # Timeouts (connect/read) and DNS/connection resets are transient.
    if isinstance(exc, (requests.Timeout, requests.ConnectionError,
                        TimeoutError, ConnectionError)):
        return True
    status = _http_status(exc)
    if status is not None:
        if status == 429:
            return True
        if 500 <= status <= 599:
            return True
        return False
    # Any other RequestException subtype (TooManyRedirects, generic
    # RequestException raised for API error payloads, SSLError, ...) is
    # not in the retryable set: fail fast.
    return False


def with_retry(
    max_attempts: int = 3,
    backoff_seconds: float = 2.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    source_name: str = "unknown",
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorate a function with retry + exponential backoff.

    Only retryable failures (see is_retryable) are retried; anything else
    is logged once and re-raised immediately with no further attempts.
    After max_attempts the last error is re-raised so callers can record
    a per-source failure without crashing the whole run.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:  # noqa: BLE001
                    last_exc = exc
                    if not is_retryable(exc):
                        log.error(
                            "[%s] non-retryable failure (no retry): %s: %s",
                            source_name or fn.__name__,
                            type(exc).__name__, exc,
                        )
                        raise
                    if attempt >= max_attempts:
                        log.error(
                            "[%s] failed after %d attempts: %s: %s",
                            source_name or fn.__name__, attempt,
                            type(exc).__name__, exc,
                        )
                        raise
                    sleep_for = backoff_seconds * (2 ** (attempt - 1))
                    log.warning(
                        "[%s] attempt %d/%d failed (%s: %s); retrying in %.1fs",
                        source_name or fn.__name__, attempt, max_attempts,
                        type(exc).__name__, exc, sleep_for,
                    )
                    time.sleep(sleep_for)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator

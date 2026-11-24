"""Retries model calls that failed for temporary reasons, with backoff."""

import random

from openai import APIConnectionError, APIStatusError

from core.hooks import Hook, Retry
from core.llm import (
    CutShort, ModelConnectionError, ModelHTTPError, ModelTimeoutError, NoReply,
)


def _retry_after_seconds(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return float(text)
    return None


def retry_delay(error, attempt, base):
    """Seconds to wait before retrying a model call, or None if retrying can't help.

    Temporary failures (connection, timeout, no reply in time, 408/409/429,
    5xx, a reply the server cut short or whose text holds the model's
    tool-call markup) are retried.
    Anything else, like a bad request or a full context, would fail the same way
    again (Codex makes the same split).
    """
    if isinstance(error, APIStatusError):
        code = error.status_code
        if code not in (408, 409, 429) and code < 500:
            return None
        delay = _retry_after_seconds(error.response.headers.get("retry-after", ""))
        if delay is not None:
            return delay
    elif isinstance(error, ModelHTTPError):
        code = error.status_code
        if code not in (408, 409, 429) and code < 500:
            return None
        delay = _retry_after_seconds(error.retry_after)
        if delay is not None:
            return delay
    elif not isinstance(
        error,
        (APIConnectionError, ModelConnectionError, ModelTimeoutError,
         CutShort, NoReply),
    ):
        return None
    return base * 2**attempt * random.uniform(0.9, 1.1)


class ModelRetry(Hook):
    """Up to `model_retries` retries, waiting about base, 2 x base, 4 x base
    seconds (`model_retry_seconds`), from the session's `limits` as they
    stand at each failure."""

    def __init__(self, limits):
        self._limits = limits

    def model_error(self, failure):
        if failure.attempt >= self._limits["model_retries"]:
            return None
        delay = retry_delay(failure.error, failure.attempt,
                            self._limits["model_retry_seconds"])
        return Retry(delay) if delay is not None else None

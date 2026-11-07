"""Retries model calls that failed for temporary reasons, with backoff."""

import random

from openai import APIConnectionError, APIStatusError

from core.hooks import Hook, Retry
from core.llm import CutShort, NoReply


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
        after = error.response.headers.get("retry-after", "")
        if after.isdigit():
            return float(after)
    elif not isinstance(error, (APIConnectionError, CutShort, NoReply)):
        return None
    return base * 2**attempt * random.uniform(0.9, 1.1)


class ModelRetry(Hook):
    """Up to `retries` retries, waiting about base, 2 x base, 4 x base seconds."""

    def __init__(self, retries, base):
        self._retries = retries
        self._base = base

    def model_error(self, failure):
        if failure.attempt >= self._retries:
            return None
        delay = retry_delay(failure.error, failure.attempt, self._base)
        if delay is None:
            return None
        error = f"{type(failure.error).__name__}: {failure.error}"
        attempt = failure.attempt + 1
        print(f"(model call failed: {error}; retry {attempt} in {delay:.1f} s)")
        return Retry(delay)

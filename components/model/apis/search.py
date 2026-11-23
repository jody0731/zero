"""A search API's shared result and single-request HTTP transport."""

from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urlsplit

import httpx2


class SearchError(Exception):
    """An error that can be shown without the request, headers, or response body."""


@dataclass(frozen=True)
class Source:
    url: str
    title: str = ""
    snippet: str = ""
    date: str = ""
    site: str = ""
    summary: str = ""


@dataclass(frozen=True)
class SearchResponse:
    sources: list[Source]
    summary: str = ""
    billed: bool | None = None


def is_http_url(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme in ("http", "https") and parts.hostname
                    and parts.username is None and parts.password is None)
    except ValueError:
        return False


def http_error(status):
    """The status and its standard meaning, without upstream text."""
    if 300 <= status < 400:
        return SearchError(f"HTTP {status}. The service redirected the lookup.")
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "unexpected status"
    return SearchError(f"HTTP {status} ({reason}).")


def client(seconds, transport):
    return httpx2.Client(
        timeout=seconds, transport=transport, follow_redirects=False,
    )


def post_json(url, body, headers, seconds, transport, check):
    """Send once and read JSON. Auth and any task data stay at this URL."""
    try:
        with client(seconds, transport) as http:
            check()
            reply = http.post(url, json=body, headers=headers)
            if not reply.is_success:
                raise http_error(reply.status_code)
            return reply.json()
    except httpx2.TimeoutException:
        raise SearchError(f"The service gave no response within {seconds:g} s.") \
            from None
    except httpx2.HTTPError:
        raise SearchError("The connection to the search service failed.") from None
    except ValueError:
        raise SearchError("The service returned invalid JSON.") from None


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def source_value(row, name):
    value = row.get(name)
    return value.strip() if isinstance(value, str) else ""


def result_rows(answer, field):
    rows = answer.get(field) if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        raise SearchError("The response did not contain a list of sources.")
    sources = [row for row in rows if isinstance(row, dict)
               and is_http_url(row.get("url"))]
    if rows and not sources:
        raise SearchError("The response did not contain usable source URLs.")
    return sources

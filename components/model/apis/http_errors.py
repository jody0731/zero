"""Safe HTTP status and structured provider error details for utility requests."""

from http import HTTPStatus

from core.llm import error_body_fields, safe_error_text


def json_body(response):
    """Read only structured JSON, leaving out response headers and raw bodies."""
    try:
        body = response.json()
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, (dict, list)) else None


def error_detail(body, *, key=None):
    """Render the named fields after redacting each one."""
    code, message = error_body_fields(body)
    fields = (safe_error_text(code, key), safe_error_text(message, key))
    return safe_error_text(" · ".join(field for field in fields if field))


def status_error_text(status, redirect, *, body=None, key=None):
    if 300 <= status < 400:
        return f"HTTP {status}. {redirect}"
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "unexpected status"
    text = f"HTTP {status} ({reason})."
    detail = error_detail(body, key=key)
    return f"{text} {detail}" if detail else text


def response_error_text(response, redirect, *, key=None):
    status = response.status_code
    body = None if 300 <= status < 400 else json_body(response)
    return status_error_text(status, redirect, body=body, key=key)

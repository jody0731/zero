"""Video generation utility API adapter: Google Gemini Omni via Interactions API.

OpenAI Videos API (Sora) was permanently shut down on Sept 24, 2026 and stays
undeclared in `catalog.toml`. Google Gemini Omni (`gemini-omni-1.2-flash`)
runs over `/v1beta/interactions` with `response_format={"type": "video", ...}`
and `delivery="uri"` for >4 MiB clips, zero transport retries on POST,
validated same-origin downloads before attaching API credentials, and
interruptible `stop`-aware POST/GET/polling.
"""

import base64
from dataclasses import dataclass
from http import HTTPStatus
import re
import threading
import time
from urllib.parse import urlsplit

import httpx2

from core.control import Stopped

MIN_VIDEO_DURATION_SECONDS = 3
MAX_VIDEO_DURATION_SECONDS = 15
_DURATION_STR_RE = re.compile(r"^\s*(\d+)\s*s?\s*$", re.IGNORECASE)


class VideoError(Exception):
    """A video generation error safe to show without keys or raw headers."""


@dataclass(frozen=True)
class VideoResponse:
    """Result from a video generation call."""

    data: bytes | None
    media_type: str
    filename: str
    uri: str | None = None
    interaction_id: str | None = None
    model: str | None = None
    billed: bool | None = None


def _http_error(status):
    if 300 <= status < 400:
        return VideoError(f"HTTP {status}. The video service redirected the request.")
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "unexpected status"
    return VideoError(f"HTTP {status} ({reason}).")


def normalize_video_duration(duration):
    """Validate duration (3-15s) and encode as Google Omni `'<N>s'` string."""
    if duration is None:
        return None
    if type(duration) is int:
        secs = duration
    elif isinstance(duration, str):
        match = _DURATION_STR_RE.match(duration)
        if not match:
            raise VideoError(
                f"Video duration must be an integer or '<N>s' string between "
                f"{MIN_VIDEO_DURATION_SECONDS} and {MAX_VIDEO_DURATION_SECONDS} "
                "seconds."
            )
        secs = int(match.group(1))
    else:
        raise VideoError(
            f"Video duration must be an integer between "
            f"{MIN_VIDEO_DURATION_SECONDS} and {MAX_VIDEO_DURATION_SECONDS} seconds."
        )
    if secs < MIN_VIDEO_DURATION_SECONDS or secs > MAX_VIDEO_DURATION_SECONDS:
        raise VideoError(
            f"Video duration must be between {MIN_VIDEO_DURATION_SECONDS} "
            f"and {MAX_VIDEO_DURATION_SECONDS} seconds (got {secs}s)."
        )
    return f"{secs}s"


def _normalized_origin(parts):
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


def _validate_download_uri(uri, service_url):
    """Validate video download URI scheme and origin before attaching credentials."""
    if not isinstance(uri, str) or not uri.strip():
        raise VideoError("Video download URI is empty.")
    uri_parts = urlsplit(uri)
    if uri_parts.username is not None or uri_parts.password is not None:
        raise VideoError("Video download URI must not include embedded credentials.")
    uri_origin = _normalized_origin(uri_parts)
    if uri_origin is None:
        raise VideoError("Video download URI must be an absolute HTTP(S) URL.")
    base_parts = urlsplit(service_url or "")
    base_origin = _normalized_origin(base_parts)
    if base_origin is None:
        raise VideoError("Configured video service URL is invalid.")
    uri_scheme = uri_origin[0]
    base_scheme = base_origin[0]
    if uri_scheme != "https" and not (
        uri_scheme == "http" and base_scheme == "http"
    ):
        raise VideoError(
            f"Video download URI scheme {uri_scheme!r} is not allowed."
        )
    if uri_origin != base_origin:
        raise VideoError(
            "Video download URI origin does not match the configured video "
            "service; refusing to forward API credentials."
        )
    return uri


def _iter_video_containers(payload):
    """Yield Google Interactions output items from `steps[].content` then `outputs`."""
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("type") != "model_output":
                continue
            yield step
            content = step.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        yield part
    outputs = payload.get("outputs")
    if isinstance(outputs, list):
        for item in outputs:
            if not isinstance(item, dict):
                continue
            yield item
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        yield part


def _extract_video_output(payload):
    """Return `(raw_bytes_or_None, mime, uri_or_None)` from an Interactions payload."""
    if not isinstance(payload, dict):
        return None, "video/mp4", None
    for part in _iter_video_containers(payload):
        if part.get("type") != "video":
            continue
        mime = (
            part.get("mime_type") or part.get("mimeType") or "video/mp4"
        ).split(";", 1)[0].strip().lower()
        uri = part.get("uri") if isinstance(part.get("uri"), str) else None
        b64 = part.get("data")
        if isinstance(b64, str) and b64:
            return base64.b64decode(b64), mime, uri
        if uri:
            return None, mime, uri
    return None, "video/mp4", None


def _ext_for_video_mime(mime):
    return {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }.get(mime, ".mp4")


def _sleep_cancellable(stop, seconds):
    if stop is not None:
        if stop.wait(seconds):
            raise Stopped()
    else:
        time.sleep(seconds)


def _call_interruptible(
    fn, *, stop, timeout_seconds, on_late_result=None, client=None,
):
    """Run one blocking HTTP call while remaining interruptible by `stop`.

    If `stop` fires while the call is still in flight, raises `Stopped` promptly
    without closing `client` out from under the background thread; once the late
    response lands, invokes `on_late_result(reply)` and closes `client`.
    """
    if stop is None:
        return fn(), False
    if stop.is_set():
        raise Stopped()
    done = threading.Event()
    lock = threading.Lock()
    state = {"done": False, "abandoned": False, "late_handled": False}

    def finish_late(close_client):
        if on_late_result is not None and "value" in state:
            try:
                on_late_result(state["value"])
            except Exception:
                pass
        if close_client and client is not None:
            try:
                client.close()
            except Exception:
                pass

    def worker():
        try:
            state["value"] = fn()
        except BaseException as exc:
            state["error"] = exc
        finally:
            with lock:
                state["done"] = True
                run_late = state["abandoned"] and not state["late_handled"]
                if run_late:
                    state["late_handled"] = True
            done.set()
            if run_late:
                finish_late(close_client=True)

    thread = threading.Thread(target=worker, name="video-http", daemon=True)
    thread.start()
    deadline = time.monotonic() + float(timeout_seconds) + 5.0
    while not done.is_set():
        if stop.is_set():
            with lock:
                state["abandoned"] = True
                already_done = state["done"] and not state["late_handled"]
                if already_done:
                    state["late_handled"] = True
            if already_done:
                finish_late(close_client=True)
            raise Stopped()
        if time.monotonic() >= deadline:
            with lock:
                state["abandoned"] = True
                already_done = state["done"] and not state["late_handled"]
                if already_done:
                    state["late_handled"] = True
            if already_done:
                finish_late(close_client=True)
            raise httpx2.TimeoutException("video HTTP request exceeded deadline")
        stop.wait(0.05)
    if "error" in state:
        raise state["error"]
    return state["value"], False


class GoogleVideos:
    """Google Gemini Omni (`gemini-omni-1.2-flash`) over Interactions API."""

    NAME = "google_videos"
    REQUIRES_MODEL = True

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/interactions"

    @classmethod
    def _download_uri(
        cls,
        client,
        service_url,
        uri,
        headers,
        *,
        seconds,
        stop=None,
    ):
        safe_uri = _validate_download_uri(uri, service_url)
        if stop is not None and stop.is_set():
            raise Stopped()
        reply, _ = _call_interruptible(
            lambda: client.get(safe_uri, headers=headers),
            stop=stop,
            timeout_seconds=seconds,
            client=client,
        )
        if not reply.is_success:
            raise _http_error(reply.status_code)
        if not reply.content:
            raise VideoError("Video URI download returned empty content.")
        ctype = (
            reply.headers.get("content-type", "")
            .split(";", 1)[0].strip().lower()
        )
        return reply.content, ctype or None

    @classmethod
    def _poll_until_done(
        cls,
        client,
        base_url,
        interaction_id,
        headers,
        *,
        poll_interval,
        deadline,
        stop=None,
        on_submitted=None,
    ):
        poll_url = f"{base_url.rstrip('/')}/{interaction_id}"

        def record_late_poll(reply):
            if on_submitted is None or not getattr(reply, "is_success", False):
                return
            try:
                payload = reply.json()
            except ValueError:
                return
            if not isinstance(payload, dict):
                return
            _, _, found_uri = _extract_video_output(payload)
            if found_uri:
                on_submitted(interaction_id=interaction_id, uri=found_uri)

        while True:
            if stop is not None and stop.is_set():
                raise Stopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise VideoError("Timed out waiting for video generation.")
            _sleep_cancellable(stop, poll_interval)
            remaining = max(1.0, deadline - time.monotonic())
            reply, _ = _call_interruptible(
                lambda: client.get(poll_url, headers=headers),
                stop=stop,
                timeout_seconds=remaining,
                on_late_result=record_late_poll,
                client=client,
            )
            if not reply.is_success:
                raise _http_error(reply.status_code)
            payload = reply.json()
            if not isinstance(payload, dict):
                raise VideoError("Video poll response was not a JSON object.")
            status = payload.get("status")
            if status in ("failed", "cancelled"):
                err = payload.get("error")
                detail = (
                    err.get("message")
                    if isinstance(err, dict)
                    and isinstance(err.get("message"), str)
                    else str(err or status)
                )
                raise VideoError(f"Video generation {status}: {detail}.")
            if status in (None, "completed", "succeeded", "done"):
                return payload

    @classmethod
    def resume(
        cls,
        service,
        *,
        interaction_id=None,
        uri=None,
        poll_interval=2.0,
        seconds=600,
        transport=None,
        stop=None,
        on_submitted=None,
    ):
        """Resume polling or URI download for an already-submitted video op."""
        headers = {"x-goog-api-key": service.key}
        effective_transport = (
            transport if transport is not None else httpx2.HTTPTransport(retries=0)
        )
        deadline = time.monotonic() + seconds
        client = httpx2.Client(
            timeout=seconds,
            transport=effective_transport,
            follow_redirects=False,
        )
        handed_off = False
        try:
            if uri:
                try:
                    raw, ctype = cls._download_uri(
                        client, service.url, uri, headers,
                        seconds=seconds, stop=stop,
                    )
                except (Stopped, httpx2.TimeoutException):
                    handed_off = stop is not None
                    raise
                mime = ctype or "video/mp4"
                return VideoResponse(
                    data=raw,
                    media_type=mime,
                    filename=f"generated-video{_ext_for_video_mime(mime)}",
                    uri=uri,
                    interaction_id=interaction_id,
                    model=service.model,
                    billed=True,
                )
            if not interaction_id:
                raise VideoError(
                    "No interaction handle or URI available to resume."
                )
            try:
                payload = cls._poll_until_done(
                    client,
                    service.url,
                    interaction_id,
                    headers,
                    poll_interval=poll_interval,
                    deadline=deadline,
                    stop=stop,
                    on_submitted=on_submitted,
                )
            except (Stopped, httpx2.TimeoutException):
                handed_off = stop is not None
                raise
            raw, mime, found_uri = _extract_video_output(payload)
            if on_submitted is not None and found_uri:
                on_submitted(interaction_id=interaction_id, uri=found_uri)
            if raw is None and found_uri:
                try:
                    raw, ctype = cls._download_uri(
                        client, service.url, found_uri, headers,
                        seconds=max(1.0, deadline - time.monotonic()),
                        stop=stop,
                    )
                except (Stopped, httpx2.TimeoutException):
                    handed_off = stop is not None
                    raise
                if ctype:
                    mime = ctype
            if not raw:
                raise VideoError("Completed video interaction contained no video.")
            return VideoResponse(
                data=raw,
                media_type=mime,
                filename=f"generated-video{_ext_for_video_mime(mime)}",
                uri=found_uri,
                interaction_id=interaction_id,
                model=service.model,
                billed=True,
            )
        except (VideoError, Stopped):
            raise
        except httpx2.TimeoutException:
            raise VideoError(
                f"The video service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise VideoError("The connection to the video service failed.") from None
        except ValueError:
            raise VideoError("The video service returned invalid JSON.") from None
        finally:
            if not handed_off:
                client.close()

    @classmethod
    def generate(
        cls,
        service,
        prompt,
        *,
        resolution=None,
        duration=None,
        aspect_ratio=None,
        reference_images=(),
        delivery="uri",
        poll_interval=2.0,
        seconds=600,
        transport=None,
        stop=None,
        on_submitted=None,
    ):
        if not isinstance(prompt, str) or not prompt.strip():
            raise VideoError("Video prompt must not be empty.")
        encoded_duration = normalize_video_duration(duration)
        parts = []
        for _, raw, mime in reference_images:
            parts.append({
                "type": "image",
                "data": base64.b64encode(raw).decode("ascii"),
                "mime_type": mime or "image/png",
            })
        parts.append({"type": "text", "text": prompt.strip()})
        response_format = {"type": "video"}
        if resolution:
            response_format["resolution"] = str(resolution)
        if encoded_duration is not None:
            response_format["duration"] = encoded_duration
        if aspect_ratio:
            response_format["aspect_ratio"] = str(aspect_ratio)
        if delivery:
            response_format["delivery"] = str(delivery)
        body = {
            "model": service.model,
            "store": False,
            "input": [{"type": "user_input", "content": parts}],
            "response_format": response_format,
        }
        headers = {"x-goog-api-key": service.key}
        effective_transport = (
            transport if transport is not None else httpx2.HTTPTransport(retries=0)
        )
        deadline = time.monotonic() + seconds
        client = httpx2.Client(
            timeout=seconds,
            transport=effective_transport,
            follow_redirects=False,
        )
        handed_off = False

        def record_late_post(reply):
            if on_submitted is None or not getattr(reply, "is_success", False):
                return
            try:
                payload = reply.json()
            except ValueError:
                return
            if not isinstance(payload, dict):
                return
            late_id = (
                payload.get("id") if isinstance(payload.get("id"), str) else None
            )
            _, _, late_uri = _extract_video_output(payload)
            if late_id or late_uri:
                on_submitted(interaction_id=late_id, uri=late_uri)

        try:
            if stop is not None and stop.is_set():
                raise Stopped()
            try:
                reply, _ = _call_interruptible(
                    lambda: client.post(service.url, json=body, headers=headers),
                    stop=stop,
                    timeout_seconds=seconds,
                    on_late_result=record_late_post,
                    client=client,
                )
            except (Stopped, httpx2.TimeoutException):
                handed_off = stop is not None
                raise
            if not reply.is_success:
                raise _http_error(reply.status_code)
            payload = reply.json()
            if not isinstance(payload, dict):
                raise VideoError("The video service returned invalid JSON.")
            interaction_id = (
                payload.get("id")
                if isinstance(payload.get("id"), str) else None
            )
            raw, mime, uri = _extract_video_output(payload)
            if on_submitted is not None:
                on_submitted(interaction_id=interaction_id, uri=uri)
            status = payload.get("status")
            if status in ("failed", "cancelled"):
                err = payload.get("error")
                detail = (
                    err.get("message")
                    if isinstance(err, dict)
                    and isinstance(err.get("message"), str)
                    else str(err or status)
                )
                raise VideoError(f"Video generation {status}: {detail}.")
            if status in ("in_progress", "queued", "pending"):
                if not interaction_id:
                    raise VideoError(
                        "Video generation is in progress but returned no id."
                    )
                try:
                    payload = cls._poll_until_done(
                        client,
                        service.url,
                        interaction_id,
                        headers,
                        poll_interval=poll_interval,
                        deadline=deadline,
                        stop=stop,
                        on_submitted=on_submitted,
                    )
                except (Stopped, httpx2.TimeoutException):
                    handed_off = stop is not None
                    raise
                raw, mime, uri = _extract_video_output(payload)
                if on_submitted is not None and uri:
                    on_submitted(interaction_id=interaction_id, uri=uri)
            if raw is None and uri:
                try:
                    raw, ctype = cls._download_uri(
                        client, service.url, uri, headers,
                        seconds=max(1.0, deadline - time.monotonic()),
                        stop=stop,
                    )
                except (Stopped, httpx2.TimeoutException):
                    handed_off = stop is not None
                    raise
                if ctype:
                    mime = ctype
            if not raw:
                raise VideoError("The video service returned no video data.")
            return VideoResponse(
                data=raw,
                media_type=mime,
                filename=f"generated-video{_ext_for_video_mime(mime)}",
                uri=uri,
                interaction_id=interaction_id,
                model=service.model,
                billed=True,
            )
        except (VideoError, Stopped):
            raise
        except httpx2.TimeoutException:
            raise VideoError(
                f"The video service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise VideoError("The connection to the video service failed.") from None
        except ValueError:
            raise VideoError("The video service returned invalid JSON.") from None
        finally:
            if not handed_off:
                client.close()

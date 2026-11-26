"""Video generation utility API adapter: Google Gemini Omni via Interactions API.

OpenAI Videos API (Sora) was permanently shut down on Sept 24, 2026 and stays
undeclared in `catalog.toml`. Google Gemini Omni (`gemini-omni-1.2-flash`)
runs over `/v1beta/interactions` with `response_format={"type": "video", ...}`
and `delivery="uri"` for >4 MiB clips. URI delivery stores the interaction
for retrieval. POST has no transport retries. Files API reads and downloads
share the interaction's deadline and keep credentials at the configured origin.
"""

import base64
from concurrent.futures import Future, wait
from contextlib import suppress
from dataclasses import dataclass
import re
import threading
import time
from urllib.parse import unquote, unquote_plus, urljoin, urlsplit

import httpx2

from components.model.apis.http_errors import (
    error_detail,
    json_body,
    response_error_text,
)
from core.control import Stopped
from core.llm import contains_key

MIN_VIDEO_DURATION_SECONDS = 3
MAX_VIDEO_DURATION_SECONDS = 15
_DURATION_PATTERN = re.compile(r"^\s*(\d+)\s*s?\s*$", re.IGNORECASE)
_FILE_PATH = re.compile(r"/files/([A-Za-z0-9_-]+)(?::download)?$")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class VideoError(Exception):
    """A video generation error safe to show without keys or raw headers."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


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


def _http_error(reply, key):
    text = response_error_text(
        reply, "The video service redirected the request.", key=key,
    )
    return VideoError(text, status_code=reply.status_code)


def normalize_video_duration(duration):
    """Validate duration (3-15s) and encode as Google Omni `'<N>s'` string."""
    if duration is None:
        return None
    if type(duration) is int:
        secs = duration
    elif isinstance(duration, str):
        match = _DURATION_PATTERN.match(duration)
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


def _origin(uri):
    try:
        parts = urlsplit(uri)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if "%" in parts.hostname:
            return None
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None:
        raise VideoError("Video URLs must not include embedded credentials.")
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname, port


def _validate_download_uri(uri, configured_origin, previous_origin):
    if not isinstance(uri, str) or any(
        char.isspace() or ord(char) < 32 or char == "\\" for char in uri
    ):
        raise VideoError("Video download URI must be an absolute HTTP(S) URL.")
    origin = _origin(uri)
    if origin is None:
        raise VideoError("Video download URI must be an absolute HTTP(S) URL.")
    scheme, host, _ = origin
    if previous_origin[0] == "https" and scheme == "http":
        raise VideoError("Video download URI downgrades HTTPS to HTTP.")
    if origin == configured_origin and origin == previous_origin:
        return origin
    if (
        scheme == "https"
        and _is_subdomain(configured_origin[1], "googleapis.com")
        and any(_is_subdomain(host, domain) for domain in (
            "googleapis.com", "googleusercontent.com",
        ))
    ):
        return origin
    raise VideoError(
        "Video download URI origin is outside the configured video service "
        "and its allowed media hosts."
    )


def _is_subdomain(host, domain):
    host = host.removesuffix(".")
    suffix = f".{domain}"
    return host.endswith(suffix) and all(
        _HOST_LABEL.fullmatch(label) for label in host.removesuffix(suffix).split(".")
    )


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


def _video_output(payload):
    for part in _iter_video_containers(payload):
        data = part.get("data")
        if part.get("type") == "video" and (
            (isinstance(data, str) and data) or _output_uri(part)
        ):
            return part
    return {}


def _output_uri(output):
    uri = output.get("uri")
    return uri if isinstance(uri, str) and uri else None


def _ext_for_video_mime(mime):
    return {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    }.get(mime, ".mp4")


def _time_left(deadline, stop):
    if stop is not None and stop.is_set():
        raise Stopped()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise httpx2.TimeoutException("video HTTP request exceeded deadline")
    return remaining


def _sleep_cancellable(stop, seconds):
    if stop is not None:
        if stop.wait(seconds):
            raise Stopped()
    else:
        time.sleep(seconds)


def _call_interruptible(call, *, stop, deadline, abandon):
    """Bound a blocking call by the deadline even without a stop event."""
    _time_left(deadline, stop)
    task = Future()

    def worker():
        try:
            task.set_result(call())
        except BaseException as error:
            task.set_exception(error)

    threading.Thread(target=worker, name="video-http", daemon=True).start()
    try:
        while not task.done():
            remaining = _time_left(deadline, stop)
            wait_seconds = min(remaining, 0.05) if stop is not None else remaining
            wait((task,), timeout=wait_seconds)
        _time_left(deadline, stop)
    except (Stopped, httpx2.TimeoutException):
        abandon(task)
        raise
    return task.result()


def _content_type(reply):
    return reply.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def _is_document(reply, content_type):
    if (
        content_type in {"text/html", "application/xhtml+xml", "text/json"}
        or content_type == "application/json" or content_type.endswith("+json")
    ):
        return True
    content = reply.content.removeprefix(b"\xef\xbb\xbf").lstrip()
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16-le" if content.startswith(b"\xff\xfe") else "utf-16-be"
        opening = ""
        for start in range(2, len(content), 4096):
            chunk = content[start:start + 4096].decode(encoding, "replace")
            opening = chunk.lstrip()[:1]
            if opening:
                break
    else:
        opening = content[:1].decode("ascii", errors="ignore")
    if opening in ("<", "{", "["):
        return True
    if opening and opening in '"-0123456789tfn':
        try:
            reply.json()
        except (ValueError, RecursionError):
            return False
        return True
    return False


def _video_bytes(reply, mime):
    content_type = _content_type(reply)
    if not reply.content:
        raise VideoError("Video URI download returned empty content.")
    if _is_document(reply, content_type):
        raise VideoError("Video URI download returned a document instead of video.")
    return reply.content, content_type if content_type.startswith("video/") else mime


def _check_interaction(payload, key):
    status = payload.get("status")
    if status in ("failed", "cancelled"):
        detail = error_detail({"error": payload.get("error")}, key=key)
        raise VideoError(
            f"Video generation {status}." + (f" {detail}" if detail else "")
        )
    return status


class _VideoRequests:
    """One Google video operation's HTTP client, trust boundary and deadline."""

    def __init__(self, service, *, seconds, transport, stop):
        self.interactions_url = service.url.rstrip("/")
        self.files_base = self.interactions_url.removesuffix("/interactions")
        self.origin = _origin(self.interactions_url)
        if self.origin is None:
            raise VideoError("Configured video service URL is invalid.")
        self.key = service.key
        self.headers = {"x-goog-api-key": self.key}
        self.seconds = seconds
        self.stop = stop
        self.deadline = time.monotonic() + seconds
        options = {"transport": transport} if transport is not None else {}
        self.client = httpx2.Client(
            timeout=seconds, follow_redirects=False, **options,
        )
        self.abandoned = False

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if not self.abandoned:
            self.client.close()
        if isinstance(error, httpx2.TimeoutException):
            raise VideoError(
                f"The video service gave no response within {self.seconds:g} s."
            ) from None
        if isinstance(error, (httpx2.HTTPError, httpx2.InvalidURL)):
            raise VideoError("The connection to the video service failed.") from None

    def request(self, method, url, *, with_key=True, on_late_result=None, **kwargs):
        headers = self.headers if with_key else {}
        streaming = method == "GET"
        stream_lock = threading.Lock()
        active_response = None
        cancelled = False

        def close_response():
            nonlocal active_response
            with stream_lock:
                response, active_response = active_response, None
            if response is not None:
                with suppress(Exception):
                    response.close()

        def send():
            nonlocal active_response
            timeout = _time_left(self.deadline, self.stop)
            request = self.client.build_request(
                method, url, headers=headers, timeout=timeout, **kwargs,
            )
            if streaming:
                request.headers.pop("cookie", None)
            response = self.client.send(request, stream=streaming)
            if not streaming:
                return response
            try:
                with stream_lock:
                    active_response = response
                    if cancelled:
                        return response
                    timeout = _time_left(self.deadline, self.stop)
                if response.is_stream_consumed or 300 <= response.status_code < 400:
                    return response
                request.extensions["timeout"].update(httpx2.Timeout(timeout).as_dict())
                body = bytearray()
                for chunk in response.iter_raw():
                    _time_left(self.deadline, self.stop)
                    body.extend(chunk)
                return httpx2.Response(
                    response.status_code, headers=response.headers,
                    content=bytes(body), request=request,
                )
            finally:
                close_response()

        def abandon(task):
            nonlocal cancelled
            self.abandoned = True
            if streaming:
                with stream_lock:
                    cancelled = True
                close_response()
                with suppress(Exception):
                    self.client.close()
                return

            def finish(done):
                try:
                    if on_late_result is not None:
                        on_late_result(done.result())
                except Exception:
                    pass
                finally:
                    try:
                        self.client.close()
                    except Exception:
                        pass

            task.add_done_callback(finish)

        return _call_interruptible(
            send, stop=self.stop, deadline=self.deadline, abandon=abandon,
        )

    def pause(self, interval):
        remaining = _time_left(self.deadline, self.stop)
        _sleep_cancellable(self.stop, min(interval, remaining))

    def _check_uri_key(self, uri):
        if not isinstance(uri, str):
            return
        spellings = (uri, unquote(uri), unquote_plus(uri))
        if any(contains_key(text, self.key) for text in spellings):
            raise VideoError("Video download URI includes the video service API key.")

    def validated_uri(self, uri):
        self._check_uri_key(uri)
        if isinstance(uri, str) and uri.startswith(("files/", "/v1beta/files/")):
            uri = urljoin(f"{self.files_base}/", uri)
            self._check_uri_key(uri)
        _validate_download_uri(uri, self.origin, self.origin)
        return uri

    def submitted_uri(self, output, interaction_id, on_submitted):
        candidate = _output_uri(output)
        uri = None
        try:
            if candidate is not None:
                uri = self.validated_uri(candidate)
        finally:
            if on_submitted is not None:
                on_submitted(interaction_id=interaction_id, uri=uri)
        return uri

    def _get_media(self, uri):
        previous = self.origin
        for hop in range(_MAX_REDIRECTS + 1):
            self._check_uri_key(uri)
            origin = _validate_download_uri(uri, self.origin, previous)
            reply = self.request("GET", uri, with_key=origin == previous == self.origin)
            if reply.status_code not in _REDIRECT_STATUSES:
                return reply
            location = reply.headers.get("location")
            if not location or hop == _MAX_REDIRECTS:
                raise _http_error(reply, self.key)
            self._check_uri_key(location)
            try:
                uri = urljoin(uri, location)
            except ValueError:
                raise VideoError("Video redirect URI is invalid.") from None
            previous = origin

    def _download(self, uri, mime):
        reply = self._get_media(uri)
        if not reply.is_success:
            raise _http_error(reply, self.key)
        return _video_bytes(reply, mime)

    def retrieve(self, uri, mime, poll_interval):
        """Resolve a file handle through its metadata before downloading the media."""
        match = _FILE_PATH.search(urlsplit(uri).path)
        if match is None:
            return self._download(uri, mime)

        metadata_url = f"{self.files_base}/files/{match.group(1)}"
        while True:
            reply = self._get_media(metadata_url)
            if reply.status_code == 404 and uri != metadata_url:
                return self._download(uri, mime)
            if not reply.is_success:
                raise _http_error(reply, self.key)
            content_type = _content_type(reply)
            if reply.status_code == 200 and content_type.startswith("video/"):
                if not _is_document(reply, content_type):
                    return _video_bytes(reply, mime)
            payload = json_body(reply) if reply.status_code == 200 else None
            if not isinstance(payload, dict):
                raise VideoError("Video file metadata did not contain a state.")
            state = payload.get("state")
            if state in ("PROCESSING", "STATE_UNSPECIFIED"):
                self.pause(poll_interval)
            elif state == "FAILED":
                detail = error_detail(payload, key=self.key)
                raise VideoError("Video file processing failed." + (
                    f" {detail}" if detail else ""
                ))
            elif state == "ACTIVE":
                download_uri = payload.get("downloadUri")
                if download_uri is None:
                    download_uri = f"{metadata_url}:download?alt=media"
                return self._download(self.validated_uri(download_uri), mime)
            else:
                raise VideoError("Video file metadata returned an unknown state.")


def _poll_interaction(requests, interaction_id, poll_interval, on_submitted):
    poll_url = f"{requests.interactions_url}/{interaction_id}"

    while True:
        requests.pause(poll_interval)
        reply = requests.request("GET", poll_url)
        if not reply.is_success:
            raise _http_error(reply, requests.key)
        payload = json_body(reply)
        if not isinstance(payload, dict):
            raise VideoError("Video poll response was not a JSON object.")
        status = _check_interaction(payload, requests.key)
        if status in (None, "completed", "succeeded", "done"):
            output = _video_output(payload)
            uri = requests.submitted_uri(output, interaction_id, on_submitted)
            return output, uri


def _video_response(requests, service, output, uri, interaction_id, poll_interval):
    mime = output.get("mime_type") or output.get("mimeType") or "video/mp4"
    if not isinstance(mime, str):
        raise VideoError("The video service returned an invalid media type.")
    mime = mime.split(";", 1)[0].strip().lower()
    encoded = output.get("data")
    raw = None
    if isinstance(encoded, str) and encoded:
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise VideoError("The video service returned invalid video data.") from None
    if raw is None and uri:
        raw, mime = requests.retrieve(uri, mime, poll_interval)
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


class GoogleVideos:
    """Google Gemini Omni (`gemini-omni-1.2-flash`) over Interactions API."""

    NAME = "google_videos"
    REQUIRES_MODEL = True

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/interactions"

    @staticmethod
    def resume(
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
        with _VideoRequests(
            service, seconds=seconds, transport=transport, stop=stop,
        ) as requests:
            output = {}
            if uri:
                uri = requests.validated_uri(uri)
            else:
                if not interaction_id:
                    raise VideoError(
                        "No interaction handle or URI available to resume."
                    )
                output, uri = _poll_interaction(
                    requests, interaction_id, poll_interval, on_submitted,
                )
            return _video_response(
                requests, service, output, uri, interaction_id, poll_interval,
            )

    @staticmethod
    def generate(
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
            "store": delivery == "uri",
            "input": [{"type": "user_input", "content": parts}],
            "response_format": response_format,
        }

        def record_late_post(reply):
            if on_submitted is None or not reply.is_success:
                return
            payload = json_body(reply)
            if isinstance(payload, dict):
                interaction_id = payload.get("id")
                if not isinstance(interaction_id, str):
                    interaction_id = None
                output = _video_output(payload)
                if interaction_id or _output_uri(output):
                    requests.submitted_uri(output, interaction_id, on_submitted)

        with _VideoRequests(
            service, seconds=seconds, transport=transport, stop=stop,
        ) as requests:
            reply = requests.request(
                "POST", service.url, json=body, on_late_result=record_late_post,
            )
            if not reply.is_success:
                raise _http_error(reply, service.key)
            payload = json_body(reply)
            if not isinstance(payload, dict):
                raise VideoError("The video service returned invalid JSON.")
            interaction_id = payload.get("id")
            interaction_id = interaction_id if isinstance(interaction_id, str) else None
            output = _video_output(payload)
            uri = requests.submitted_uri(output, interaction_id, on_submitted)
            status = _check_interaction(payload, service.key)
            if status in ("in_progress", "queued", "pending"):
                if not interaction_id:
                    raise VideoError(
                        "Video generation is in progress but returned no id."
                    )
                output, uri = _poll_interaction(
                    requests, interaction_id, poll_interval, on_submitted,
                )
            return _video_response(
                requests, service, output, uri, interaction_id, poll_interval,
            )

"""Text-to-speech (TTS) utility API adapters: OpenAI, Google, DeepSeek.

OpenAI Realtime (`gpt-realtime-2.1-mini`) synthesizes audio in a single
WebSocket turn (ahead of `gpt-4o-mini-tts`'s Jan 6, 2027 shutdown);
`openai_speech_compat` serves standard `POST /v1/audio/speech` for custom
OpenAI-compatible servers. Google Gemini TTS uses `generateContent` with
`responseModalities=["AUDIO"]`. DeepSeek uses `POST /v1/tools/tts` with no
`model` parameter.
"""

import base64
from dataclasses import dataclass
from http import HTTPStatus
import json
import re
import struct
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx2
from websockets.sync.client import connect as ws_connect_default


class TTSError(Exception):
    """A text-to-speech error safe to show without keys or raw headers."""


@dataclass(frozen=True)
class SpeechResponse:
    """Synthesized speech audio from one TTS service."""

    audio: bytes
    media_type: str
    model: str | None = None
    voice: str | None = None
    billed: bool | None = None


_RATE_RE = re.compile(r"rate=(\d+)", re.IGNORECASE)


def pcm16_to_wav(pcm_bytes, *, sample_rate=24000, channels=1, sample_width=2):
    """Wrap raw little-endian PCM16 samples into a standard RIFF WAV container."""
    data_size = len(pcm_bytes)
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
        b"data",
        data_size,
    )
    return header + pcm_bytes


def _http_error(status):
    if 300 <= status < 400:
        return TTSError(f"HTTP {status}. The speech service redirected the request.")
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "unexpected status"
    return TTSError(f"HTTP {status} ({reason}).")


def _ws_endpoint(base_url, path_suffix):
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((scheme, parts.netloc, f"{path}{path_suffix}", "", ""))


def _check_text(text):
    if not isinstance(text, str) or not text.strip():
        raise TTSError("Text to synthesize must not be empty.")
    return text.strip()


def _parse_pcm_rate(media_type, default=24000):
    match = _RATE_RE.search(media_type or "")
    if match:
        try:
            rate = int(match.group(1))
            if 8000 <= rate <= 192000:
                return rate
        except ValueError:
            pass
    return default


class OpenAISpeech:
    """OpenAI Realtime single-turn TTS (`gpt-realtime-2.1-mini`) over WebSocket."""

    NAME = "openai_speech"
    REQUIRES_MODEL = True
    DEFAULT_VOICE = "alloy"
    SAMPLE_RATE = 24000

    @staticmethod
    def endpoint(base_url):
        return _ws_endpoint(base_url, "/realtime")

    @classmethod
    def synthesize(
        cls,
        service,
        text,
        *,
        voice=None,
        instructions=None,
        seconds=60,
        transport=None,
        ws_connect=None,
        check=None,
    ):
        clean_text = _check_text(text)
        chosen_voice = (voice or cls.DEFAULT_VOICE).strip() or cls.DEFAULT_VOICE
        query = urlencode({"model": service.model})
        sep = "&" if "?" in service.url else "?"
        url = f"{service.url}{sep}{query}"
        headers = {"Authorization": f"Bearer {service.key}"}
        connector = ws_connect or ws_connect_default
        pcm_chunks = []
        speak_instructions = (
            instructions.strip()
            if isinstance(instructions, str) and instructions.strip()
            else (
                "Read the user's message aloud verbatim. Do not add, omit, "
                "or comment on the text."
            )
        )
        try:
            if check is not None:
                check()
            with connector(
                url,
                additional_headers=headers,
                open_timeout=seconds,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
            ) as sock:
                sock.send(json.dumps({
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "output_modalities": ["audio"],
                        "instructions": speak_instructions,
                        "audio": {
                            "output": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": cls.SAMPLE_RATE,
                                },
                                "voice": chosen_voice,
                            },
                        },
                    },
                }))
                sock.send(json.dumps({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": clean_text}],
                    },
                }))
                sock.send(json.dumps({
                    "type": "response.create",
                    "response": {
                        "output_modalities": ["audio"],
                        "instructions": speak_instructions,
                        "audio": {
                            "output": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": cls.SAMPLE_RATE,
                                },
                                "voice": chosen_voice,
                            },
                        },
                    },
                }))
                deadline = time.monotonic() + float(seconds)
                completed = False
                while True:
                    if check is not None:
                        check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    try:
                        raw_msg = sock.recv(timeout=min(1.0, max(0.01, remaining)))
                    except TimeoutError:
                        if time.monotonic() >= deadline:
                            raise
                        continue
                    if raw_msg is None:
                        break
                    if isinstance(raw_msg, bytes):
                        raw_msg = raw_msg.decode("utf-8", errors="replace")
                    event = json.loads(raw_msg)
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    if kind in (
                        "response.audio.delta",
                        "response.output_audio.delta",
                    ):
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            pcm_chunks.append(base64.b64decode(delta))
                    elif kind == "response.done":
                        resp = event.get("response")
                        status = resp.get("status") if isinstance(resp, dict) else None
                        if status != "completed":
                            details = (
                                resp.get("status_details")
                                if isinstance(resp, dict) else None
                            )
                            err = (
                                details.get("error")
                                if isinstance(details, dict) else None
                            )
                            reason = (
                                details.get("reason")
                                if isinstance(details, dict) else None
                            )
                            detail = (
                                err.get("message")
                                if isinstance(err, dict)
                                and isinstance(err.get("message"), str)
                                else reason
                                if isinstance(reason, str) and reason
                                else status or "unknown status"
                            )
                            raise TTSError(
                                f"OpenAI Realtime speech did not complete "
                                f"({status or 'unknown'}): {detail}."
                            )
                        completed = True
                        break
                    elif kind == "error":
                        err = event.get("error")
                        detail = (
                            err.get("message")
                            if isinstance(err, dict)
                            and isinstance(err.get("message"), str)
                            else "realtime error"
                        )
                        raise TTSError(f"OpenAI Realtime speech error: {detail}.")
                if not completed:
                    raise TTSError(
                        "OpenAI Realtime speech closed before sending a "
                        "completed response.done."
                    )
        except TTSError:
            raise
        except TimeoutError:
            raise TTSError(
                f"The speech service gave no response within {seconds:g} s."
            ) from None
        except (OSError, ValueError) as exc:
            raise TTSError(f"OpenAI Realtime speech failed: {exc}.") from None
        except Exception as exc:
            if exc.__class__.__name__ == "Stopped":
                raise
            raise TTSError(
                f"The connection to the speech service failed ({type(exc).__name__})."
            ) from None

        pcm = b"".join(pcm_chunks)
        if not pcm:
            raise TTSError("OpenAI Realtime returned no audio data.")
        wav = pcm16_to_wav(pcm, sample_rate=cls.SAMPLE_RATE)
        return SpeechResponse(
            audio=wav,
            media_type="audio/wav",
            model=service.model,
            voice=chosen_voice,
            billed=True,
        )


class OpenAISpeechCompat:
    """OpenAI-compatible `POST /v1/audio/speech` for custom local/3P servers."""

    NAME = "openai_speech_compat"
    REQUIRES_MODEL = True
    DEFAULT_VOICE = "alloy"

    @staticmethod
    def endpoint(base_url):
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/audio/speech"
        return f"{base}/v1/audio/speech"

    @classmethod
    def synthesize(
        cls,
        service,
        text,
        *,
        voice=None,
        instructions=None,
        seconds=60,
        transport=None,
        ws_connect=None,
        check=None,
    ):
        clean_text = _check_text(text)
        chosen_voice = (voice or cls.DEFAULT_VOICE).strip() or cls.DEFAULT_VOICE
        body = {
            "model": service.model,
            "input": clean_text,
            "voice": chosen_voice,
            "response_format": "wav",
        }
        if isinstance(instructions, str) and instructions.strip():
            body["instructions"] = instructions.strip()
        headers = {"Authorization": f"Bearer {service.key}"}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(service.url, json=body, headers=headers)
                if not reply.is_success:
                    raise _http_error(reply.status_code)
                audio = reply.content
                ctype = (
                    reply.headers.get("content-type", "audio/wav")
                    .split(";", 1)[0].strip().lower()
                ) or "audio/wav"
        except TTSError:
            raise
        except httpx2.TimeoutException:
            raise TTSError(
                f"The speech service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise TTSError("The connection to the speech service failed.") from None
        if not audio:
            raise TTSError("The speech service returned empty audio.")
        return SpeechResponse(
            audio=audio,
            media_type=ctype,
            model=service.model,
            voice=chosen_voice,
            billed=True,
        )


class GoogleSpeech:
    """Google Gemini TTS (`gemini-3.8-flash-lite-tts`, `responseModalities=AUDIO`)."""

    NAME = "google_speech"
    REQUIRES_MODEL = True
    DEFAULT_VOICE = "Kore"

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/models"

    @classmethod
    def synthesize(
        cls,
        service,
        text,
        *,
        voice=None,
        instructions=None,
        seconds=60,
        transport=None,
        ws_connect=None,
        check=None,
    ):
        clean_text = _check_text(text)
        chosen_voice = (voice or cls.DEFAULT_VOICE).strip() or cls.DEFAULT_VOICE
        prompt = (
            f"{instructions.strip()}\n\n{clean_text}"
            if isinstance(instructions, str) and instructions.strip()
            else clean_text
        )
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": chosen_voice},
                    },
                },
            },
        }
        url = f"{service.url.rstrip('/')}/{service.model}:generateContent"
        headers = {"x-goog-api-key": service.key}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(url, json=body, headers=headers)
                if not reply.is_success:
                    raise _http_error(reply.status_code)
                payload = reply.json()
        except TTSError:
            raise
        except httpx2.TimeoutException:
            raise TTSError(
                f"The speech service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise TTSError("The connection to the speech service failed.") from None
        except ValueError:
            raise TTSError("The speech service returned invalid JSON.") from None

        audio_bytes = None
        raw_mime = "audio/wav"
        for candidate in (payload.get("candidates") or ()):
            parts = ((candidate or {}).get("content") or {}).get("parts") or ()
            for part in parts:
                inline = (
                    part.get("inlineData") or part.get("inline_data")
                    if isinstance(part, dict) else None
                )
                if isinstance(inline, dict) and isinstance(inline.get("data"), str):
                    raw_mime = (
                        inline.get("mimeType")
                        or inline.get("mime_type")
                        or "audio/wav"
                    )
                    audio_bytes = base64.b64decode(inline["data"])
                    break
            if audio_bytes is not None:
                break
        if not audio_bytes:
            raise TTSError("Google TTS returned no audio data.")

        clean_mime = raw_mime.split(";", 1)[0].strip().lower()
        if clean_mime in ("audio/l16", "audio/pcm", "audio/raw"):
            rate = _parse_pcm_rate(raw_mime, default=24000)
            audio_bytes = pcm16_to_wav(audio_bytes, sample_rate=rate)
            clean_mime = "audio/wav"
        return SpeechResponse(
            audio=audio_bytes,
            media_type=clean_mime or "audio/wav",
            model=service.model,
            voice=chosen_voice,
            billed=True,
        )


class DeepSeekSpeech:
    """DeepSeek direct TTS (`POST /v1/tools/tts`, no model param, WAV output)."""

    NAME = "deepseek_speech"
    REQUIRES_MODEL = False
    DEFAULT_VOICE = "stella"
    VOICES = ("stella", "tide")

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/").removesuffix("/v1") + "/v1/tools/tts"

    @classmethod
    def synthesize(
        cls,
        service,
        text,
        *,
        voice=None,
        instructions=None,
        seconds=60,
        transport=None,
        ws_connect=None,
        check=None,
    ):
        clean_text = _check_text(text)
        chosen_voice = (voice or cls.DEFAULT_VOICE).strip() or cls.DEFAULT_VOICE
        body = {
            "text": clean_text,
            "voice_id": chosen_voice,
            "format": "wav",
            "filter_markdown": True,
        }
        headers = {"Authorization": f"Bearer {service.key}"}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(service.url, json=body, headers=headers)
                if not reply.is_success:
                    raise _http_error(reply.status_code)
                ctype = (
                    reply.headers.get("content-type", "")
                    .split(";", 1)[0].strip().lower()
                )
                if "json" in ctype:
                    payload = reply.json()
                    b64 = (
                        payload.get("audio") or payload.get("data")
                        if isinstance(payload, dict) else None
                    )
                    if not isinstance(b64, str) or not b64:
                        raise TTSError("DeepSeek TTS JSON contained no audio.")
                    audio = base64.b64decode(b64)
                    out_mime = (
                        payload.get("media_type")
                        or payload.get("mime_type")
                        or "audio/wav"
                    )
                else:
                    audio = reply.content
                    out_mime = ctype or "audio/wav"
        except TTSError:
            raise
        except httpx2.TimeoutException:
            raise TTSError(
                f"The speech service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise TTSError("The connection to the speech service failed.") from None
        except ValueError:
            raise TTSError("The speech service returned invalid JSON.") from None
        if not audio:
            raise TTSError("DeepSeek TTS returned empty audio.")
        return SpeechResponse(
            audio=audio,
            media_type=out_mime,
            model=None,
            voice=chosen_voice,
            billed=True,
        )

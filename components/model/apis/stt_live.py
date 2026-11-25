"""Live WebSocket streaming STT adapters: OpenAI Realtime & Google Gemini Live.

Both adapters accept raw 16-bit little-endian mono PCM chunks, emit
replaceable interim text and finalized segments, and support clean cancel and
bounded same-route batch fallback helpers.
"""

from array import array
import base64
from dataclasses import dataclass
import json
import struct
import sys
from urllib.parse import urlsplit, urlunsplit

from websockets.sync.client import connect as ws_connect_default

from components.model.apis.stt import STTError


@dataclass(frozen=True)
class TranscriptEvent:
    """One live transcription event emitted toward a dictation subscriber."""

    kind: str
    text: str = ""
    item_id: str | None = None
    error: str | None = None


def pcm16_to_wav(pcm_bytes, *, sample_rate=16000, channels=1, sample_width=2):
    """Wrap raw PCM16 LE samples into a RIFF WAV byte buffer."""
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


MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000


def resampled_pcm16_bytes(pcm_len, src_rate, dst_rate):
    """Compute resampled PCM16 mono output byte length without allocating."""
    if pcm_len <= 0 or src_rate <= 0 or dst_rate <= 0:
        return 0
    if src_rate == dst_rate:
        return pcm_len
    usable = pcm_len - (pcm_len % 2)
    src_len = usable // 2
    if src_len <= 0:
        return 0
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    return dst_len * 2


def resample_pcm16_mono(
    pcm_bytes, src_rate, dst_rate, *, max_output_bytes=None,
):
    """Linear-interpolation resampler for mono 16-bit little-endian PCM."""
    if not pcm_bytes or src_rate == dst_rate or src_rate <= 0 or dst_rate <= 0:
        return pcm_bytes
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    src_len = usable // 2
    dst_len = max(1, int(round(src_len * dst_rate / src_rate)))
    if max_output_bytes is not None and dst_len * 2 > max_output_bytes:
        raise ValueError(
            f"resampled audio ({dst_len * 2} bytes) exceeds "
            f"{max_output_bytes} byte limit"
        )
    samples = array("h")
    samples.frombytes(pcm_bytes[:usable])
    if sys.byteorder != "little":
        samples.byteswap()
    ratio = src_len / dst_len
    out = array("h", [0] * dst_len)
    last_idx = src_len - 1
    for i in range(dst_len):
        pos = i * ratio
        idx = int(pos)
        if idx >= last_idx:
            out[i] = samples[last_idx]
        else:
            frac = pos - idx
            val = samples[idx] + (samples[idx + 1] - samples[idx]) * frac
            out[i] = max(-32768, min(32767, int(round(val))))
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _http_to_ws_base(base_url):
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return scheme, parts.netloc, parts.path.rstrip("/")


class OpenAIItemOrderTracker:
    """Track OpenAI Realtime transcription `item_id`s so completions emit in order."""

    def __init__(self):
        self._order = []
        self._interim = {}
        self._completed = {}
        self._emitted = set()

    def register(self, item_id, previous_item_id=None):
        if not item_id or item_id in self._order:
            return
        if previous_item_id and previous_item_id in self._order:
            idx = self._order.index(previous_item_id) + 1
            self._order.insert(idx, item_id)
        else:
            self._order.append(item_id)

    def add_delta(self, item_id, delta):
        if not item_id:
            return ""
        self.register(item_id)
        self._interim[item_id] = self._interim.get(item_id, "") + delta
        return self._interim[item_id]

    def complete(self, item_id, transcript):
        """Record completion for `item_id`; return ready finalized `(id, text)` list."""
        if not item_id:
            clean = (transcript or "").strip()
            return [(None, clean)] if clean else []
        self.register(item_id)
        self._completed[item_id] = (transcript or "").strip()
        self._interim.pop(item_id, None)
        return self._drain_ready()

    def _drain_ready(self):
        ready = []
        for item_id in self._order:
            if item_id in self._emitted:
                continue
            if item_id not in self._completed:
                break
            self._emitted.add(item_id)
            text = self._completed[item_id]
            if text:
                ready.append((item_id, text))
        return ready

    def flush_remaining(self):
        """Flush any completed items still buffered behind an incomplete predecessor."""
        ready = []
        for item_id in self._order:
            if item_id in self._emitted:
                continue
            if item_id in self._completed:
                self._emitted.add(item_id)
                text = self._completed[item_id]
                if text:
                    ready.append((item_id, text))
        return ready

    def current_interim(self):
        pieces = [
            self._interim[item_id]
            for item_id in self._order
            if item_id not in self._emitted and item_id in self._interim
        ]
        return " ".join(p.strip() for p in pieces if p.strip())


class OpenAILiveSTT:
    """OpenAI Realtime transcription (`gpt-live-transcribe`, 24 kHz PCM16 LE)."""

    NAME = "openai_audio"
    SAMPLE_RATE = 24000
    DEFAULT_LIVE_MODEL = "gpt-live-transcribe"

    @staticmethod
    def ws_url(base_url):
        scheme, netloc, path = _http_to_ws_base(base_url)
        if path.endswith("/audio/transcriptions"):
            path = path.removesuffix("/audio/transcriptions")
        if not path.endswith("/v1"):
            path = f"{path}/v1"
        return urlunsplit((
            scheme, netloc, f"{path}/realtime", "intent=transcription", "",
        ))

    def __init__(
        self,
        service,
        *,
        language=None,
        ws_connect=None,
        open_timeout=15,
    ):
        self.service = service
        self.language = language
        self.live_model = (
            getattr(service, "live_model", None) or self.DEFAULT_LIVE_MODEL
        )
        self._ws_connect = ws_connect or ws_connect_default
        self._open_timeout = open_timeout
        self._sock = None
        self._tracker = OpenAIItemOrderTracker()

    def connect(self):
        url = self.ws_url(self.service.url)
        headers = {"Authorization": f"Bearer {self.service.key}"}
        try:
            self._sock = self._ws_connect(
                url,
                additional_headers=headers,
                open_timeout=self._open_timeout,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            )
            transcription = {"model": self.live_model}
            if self.language:
                transcription["languages"] = [self.language]
            self._sock.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {
                                "type": "audio/pcm",
                                "rate": self.SAMPLE_RATE,
                            },
                            "transcription": transcription,
                        },
                    },
                },
            }))
        except Exception as exc:
            self.close()
            raise STTError(
                f"Could not start live transcription ({type(exc).__name__})."
            ) from None

    def send_pcm(self, pcm_bytes):
        if self._sock is None or not pcm_bytes:
            return
        self._sock.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm_bytes).decode("ascii"),
        }))

    def commit(self):
        if self._sock is None:
            return
        self._sock.send(json.dumps({"type": "input_audio_buffer.commit"}))

    def recv_events(self, timeout=0.25):
        """Read one WebSocket frame and return zero or more TranscriptEvents."""
        if self._sock is None:
            return [TranscriptEvent("done")]
        try:
            raw = self._sock.recv(timeout=timeout)
        except TimeoutError:
            return []
        except Exception as exc:
            if "ConnectionClosed" in type(exc).__name__:
                flushed = [
                    TranscriptEvent("final", text=text, item_id=item_id)
                    for item_id, text in self._tracker.flush_remaining()
                ]
                return [*flushed, TranscriptEvent("done")]
            return [TranscriptEvent("error", error=str(exc))]
        if raw is None:
            flushed = [
                TranscriptEvent("final", text=text, item_id=item_id)
                for item_id, text in self._tracker.flush_remaining()
            ]
            return [*flushed, TranscriptEvent("done")]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(msg, dict):
            return []
        kind = msg.get("type")
        if kind == "input_audio_buffer.committed":
            self._tracker.register(
                msg.get("item_id"), msg.get("previous_item_id"),
            )
            return []
        if kind == "conversation.item.input_audio_transcription.delta":
            item_id = msg.get("item_id")
            delta = msg.get("delta") or ""
            if isinstance(delta, str) and delta:
                self._tracker.add_delta(item_id, delta)
                return [TranscriptEvent(
                    "interim",
                    text=self._tracker.current_interim(),
                    item_id=item_id,
                )]
            return []
        if kind == "conversation.item.input_audio_transcription.completed":
            item_id = msg.get("item_id")
            transcript = msg.get("transcript") or ""
            ready = self._tracker.complete(item_id, transcript)
            events = [
                TranscriptEvent("final", text=text, item_id=iid)
                for iid, text in ready
            ]
            interim = self._tracker.current_interim()
            if ready and interim:
                events.append(TranscriptEvent("interim", text=interim))
            return events
        if kind == "conversation.item.input_audio_transcription.failed":
            err = msg.get("error")
            detail = (
                err.get("message")
                if isinstance(err, dict) and isinstance(err.get("message"), str)
                else "transcription item failed"
            )
            return [TranscriptEvent("error", error=detail)]
        if kind == "error":
            err = msg.get("error")
            detail = (
                err.get("message")
                if isinstance(err, dict) and isinstance(err.get("message"), str)
                else "realtime transcription error"
            )
            return [TranscriptEvent("error", error=detail)]
        return []

    def flush(self):
        return [
            TranscriptEvent("final", text=text, item_id=item_id)
            for item_id, text in self._tracker.flush_remaining()
        ]

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class GoogleLiveSTT:
    """Google Gemini Live transcription (`gemini-3.5-transcribe-live`, 16 kHz)."""

    NAME = "google_audio"
    SAMPLE_RATE = 16000
    DEFAULT_LIVE_MODEL = "gemini-3.5-transcribe-live"
    WS_PATH = (
        "/ws/google.ai.generativelanguage.v1beta."
        "GenerativeService.BidiGenerateContent"
    )

    @classmethod
    def ws_url(cls, base_url):
        scheme, netloc, _ = _http_to_ws_base(base_url)
        return urlunsplit((scheme, netloc, cls.WS_PATH, "", ""))

    def __init__(
        self,
        service,
        *,
        language=None,
        ws_connect=None,
        open_timeout=15,
    ):
        self.service = service
        self.language = language
        self.live_model = (
            getattr(service, "live_model", None) or self.DEFAULT_LIVE_MODEL
        )
        self._ws_connect = ws_connect or ws_connect_default
        self._open_timeout = open_timeout
        self._sock = None
        self._seq = 0

    def connect(self):
        url = self.ws_url(self.service.url)
        headers = {"x-goog-api-key": self.service.key}
        try:
            self._sock = self._ws_connect(
                url,
                additional_headers=headers,
                open_timeout=self._open_timeout,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            )
            setup = {
                "model": f"models/{self.live_model}",
                "generationConfig": {"responseModalities": ["TEXT"]},
                "inputAudioTranscription": {},
            }
            if self.language:
                setup["inputAudioTranscription"]["languageCodes"] = [
                    self.language,
                ]
            self._sock.send(json.dumps({"setup": setup}))
        except Exception as exc:
            self.close()
            raise STTError(
                f"Could not start live transcription ({type(exc).__name__})."
            ) from None

    def send_pcm(self, pcm_bytes):
        if self._sock is None or not pcm_bytes:
            return
        self._sock.send(json.dumps({
            "realtimeInput": {
                "audio": {
                    "mimeType": f"audio/pcm;rate={self.SAMPLE_RATE}",
                    "data": base64.b64encode(pcm_bytes).decode("ascii"),
                },
            },
        }))

    def commit(self):
        if self._sock is None:
            return
        self._sock.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

    def recv_events(self, timeout=0.25):
        """Read one WebSocket frame; emits interim & finalized without turnComplete."""
        if self._sock is None:
            return [TranscriptEvent("done")]
        try:
            raw = self._sock.recv(timeout=timeout)
        except TimeoutError:
            return []
        except Exception as exc:
            if "ConnectionClosed" in type(exc).__name__:
                return [TranscriptEvent("done")]
            return [TranscriptEvent("error", error=str(exc))]
        if raw is None:
            return [TranscriptEvent("done")]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            msg = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(msg, dict):
            return []
        if isinstance(msg.get("error"), dict):
            detail = msg["error"].get("message") or "live transcription error"
            return [TranscriptEvent("error", error=str(detail))]
        sc = msg.get("serverContent")
        if not isinstance(sc, dict):
            return []
        events = []
        interim = sc.get("interimInputTranscription")
        if isinstance(interim, dict):
            text = interim.get("text")
            if isinstance(text, str) and text.strip():
                events.append(TranscriptEvent("interim", text=text.strip()))
        final = sc.get("inputTranscription")
        if isinstance(final, dict):
            text = final.get("text")
            if isinstance(text, str) and text.strip():
                self._seq += 1
                events.append(TranscriptEvent(
                    "final", text=text.strip(), item_id=f"seg-{self._seq}",
                ))
        return events

    def flush(self):
        return []

    def close(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


LIVE_STT_APIS = {
    OpenAILiveSTT.NAME: OpenAILiveSTT,
    GoogleLiveSTT.NAME: GoogleLiveSTT,
}


def live_stt_class(api_or_name):
    """Return the live WebSocket STT adapter class for `api_or_name`, or None."""
    name = (
        api_or_name if isinstance(api_or_name, str)
        else getattr(api_or_name, "NAME", None)
    )
    return LIVE_STT_APIS.get(name)


def live_sample_rate(api_or_name):
    """Preferred PCM16 mono sample rate for live streaming or batch fallback."""
    cls = live_stt_class(api_or_name)
    return cls.SAMPLE_RATE if cls is not None else 16000

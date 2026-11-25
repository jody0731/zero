"""Pre-session live dictation manager with frozen routing and batch fallback.

Works before a session exists (draft model + stt_service) as well as inside an
existing session. On `start()`, resolves and freezes the effective STT service,
endpoint, and mode under the authenticated principal before any audio leaves so
concurrent setting edits cannot redirect an active stream or fallback.
"""

import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path
import queue
import secrets
import shutil
import tempfile
import threading
import time

from components.media import transcribe
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF, servers
from components.model.apis.stt import LocalWhisper, STTError
from components.model.apis.stt_live import (
    MAX_SAMPLE_RATE,
    MIN_SAMPLE_RATE,
    live_sample_rate,
    live_stt_class,
    pcm16_to_wav,
    resample_pcm16_mono,
    resampled_pcm16_bytes,
)
from components.model.servers import ModelServerError
from components.sandbox.sandbox import harness_free_env

DEFAULT_INACTIVITY_SECONDS = 60.0
# Bound in-memory buffered PCM for batch fallback (8 MiB raw PCM ~ 256s @ 16kHz).
MAX_DICTATION_BYTES = 8 * 1024 * 1024
MAX_CHUNK_PCM_BYTES = 512 * 1024
EVENT_QUEUE_MAX = 256


class DictationError(Exception):
    """A dictation error safe to return to the caller."""


@dataclass
class _DictationSession:
    dictation_id: str
    owner_key: str
    service: object
    mode: str
    target_rate: int
    language: str | None
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)
    next_seq: int = 0
    closed: bool = False
    cancelled: bool = False
    live_adapter: object = None
    reader_thread: threading.Thread | None = None
    pcm_buffer: bytearray = field(default_factory=bytearray)
    final_segments: list[str] = field(default_factory=list)
    interim_text: str = ""
    error: str | None = None
    done_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    outbound_lock: threading.Lock = field(default_factory=threading.Lock)
    subscribers: list[queue.Queue] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)


class DictationManager:
    """Owner-scoped live and batch-fallback dictation manager."""

    def __init__(
        self,
        *,
        inactivity_seconds=DEFAULT_INACTIVITY_SECONDS,
        transport=None,
        ws_connect=None,
        services_provider=None,
    ):
        self.inactivity_seconds = float(inactivity_seconds)
        self.transport = transport
        self.ws_connect = ws_connect
        self._services_provider = services_provider or transcribe.available_stt_services
        self._lock = threading.Lock()
        self._sessions: dict[str, _DictationSession] = {}
        self._stop_reaper = threading.Event()
        self._reaper = None

    def begin(self):
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._stop_reaper.clear()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="dictation-reaper", daemon=True,
        )
        self._reaper.start()

    def shutdown(self):
        self._stop_reaper.set()
        with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for item in items:
            self._abort_session(item, reason="server stopping")

    def busy_with(self):
        with self._lock:
            count = sum(1 for item in self._sessions.values() if not item.closed)
        if not count:
            return {}
        return {"dictation": [f"active-{i}" for i in range(1, count + 1)]}

    def _reap_loop(self):
        interval = max(0.05, min(2.0, self.inactivity_seconds / 4.0))
        while not self._stop_reaper.wait(interval):
            self.reap_inactive()

    def reap_inactive(self):
        now = time.monotonic()
        expired = []
        with self._lock:
            for did, item in list(self._sessions.items()):
                idle = now - item.last_activity
                if not item.closed and idle >= self.inactivity_seconds:
                    expired.append(item)
                elif item.closed and idle >= max(10.0, self.inactivity_seconds):
                    self._sessions.pop(did, None)
        for item in expired:
            self._abort_session(item, reason="dictation expired after inactivity")

    def _resolve_frozen_service(self, *, model, stt_service):
        saved = SERVICE_CURRENT_SERVER if stt_service is None else stt_service
        if saved == SERVICE_OFF:
            raise DictationError("Speech-to-text is off in settings.")
        model_server = None
        if isinstance(model, str) and model.strip():
            try:
                model_server, _ = servers.model_address(
                    model.strip(), servers.table(),
                )
            except ModelServerError:
                model_server = None
        try:
            available = self._services_provider()
        except ModelServerError as exc:
            raise DictationError(str(exc)) from None
        chosen, reason = transcribe.select_stt_service(
            available, model_server, saved,
        )
        if chosen is None:
            raise DictationError(reason)
        return chosen

    def start(
        self,
        owner_key,
        *,
        model=None,
        stt_service=None,
        language=None,
        mode=None,
    ):
        """Resolve and freeze the dictation route before any audio leaves."""
        frozen = self._resolve_frozen_service(model=model, stt_service=stt_service)
        live_cls = live_stt_class(frozen.api)
        want_live = (mode != "batch") and live_cls is not None
        target_rate = live_sample_rate(frozen.api) if want_live else 16000
        did = f"dict-{secrets.token_hex(16)}"
        clean_lang = (
            language if isinstance(language, str) and language.strip() else None
        )
        sess = _DictationSession(
            dictation_id=did,
            owner_key=owner_key,
            service=frozen,
            mode="live" if want_live else "batch",
            target_rate=target_rate,
            language=clean_lang,
        )
        if want_live:
            adapter = live_cls(
                frozen, language=sess.language, ws_connect=self.ws_connect,
            )
            try:
                adapter.connect()
            except STTError:
                # Automatic bounded same-route batch fallback on the frozen service.
                sess.mode = "batch"
                sess.target_rate = 16000
                sess.live_adapter = None
            else:
                sess.live_adapter = adapter
                sess.reader_thread = threading.Thread(
                    target=self._read_live,
                    args=(sess,),
                    name=f"dictation-{did}",
                    daemon=True,
                )
                sess.reader_thread.start()

        with self._lock:
            self._sessions[did] = sess
        self.begin()
        return {
            "dictation_id": did,
            "service": frozen.name,
            "model": (
                frozen.live_model
                if sess.mode == "live" and frozen.live_model
                else frozen.model
            ),
            "mode": sess.mode,
            "sample_rate": sess.target_rate,
        }

    def _get_owned(self, dictation_id, owner_key, *, allow_closed=False):
        if not isinstance(dictation_id, str) or not dictation_id:
            raise DictationError("dictation_id must be a nonempty string")
        with self._lock:
            sess = self._sessions.get(dictation_id)
        if sess is None:
            raise DictationError(f"unknown dictation {dictation_id!r}")
        if sess.owner_key != owner_key:
            raise DictationError("dictation belongs to another caller")
        if sess.closed and not allow_closed:
            raise DictationError("dictation has already ended")
        return sess

    def _publish(self, sess, event):
        with sess.lock:
            sess.history.append(event)
            for box in list(sess.subscribers):
                try:
                    box.put_nowait(event)
                except queue.Full:
                    pass

    def _read_live(self, sess):
        adapter = sess.live_adapter
        try:
            while not sess.cancelled and not sess.done_event.is_set():
                events = adapter.recv_events(timeout=0.2)
                for ev in events:
                    if ev.kind == "interim":
                        with sess.lock:
                            sess.interim_text = ev.text
                        self._publish(sess, {
                            "event": "interim",
                            "dictation_id": sess.dictation_id,
                            "text": ev.text,
                            "full_text": self._full_text(sess),
                        })
                    elif ev.kind == "final":
                        with sess.lock:
                            if ev.text:
                                sess.final_segments.append(ev.text)
                            sess.interim_text = ""
                        self._publish(sess, {
                            "event": "final",
                            "dictation_id": sess.dictation_id,
                            "text": ev.text,
                            "item_id": ev.item_id,
                            "full_text": self._full_text(sess),
                        })
                    elif ev.kind == "error":
                        with sess.lock:
                            sess.error = ev.error or "live transcription error"
                        self._publish(sess, {
                            "event": "error",
                            "dictation_id": sess.dictation_id,
                            "error": sess.error,
                        })
                        sess.done_event.set()
                        return
                    elif ev.kind == "done":
                        sess.done_event.set()
                        return
        finally:
            if adapter is not None:
                for ev in adapter.flush():
                    if ev.kind == "final" and ev.text:
                        with sess.lock:
                            sess.final_segments.append(ev.text)
                        self._publish(sess, {
                            "event": "final",
                            "dictation_id": sess.dictation_id,
                            "text": ev.text,
                            "item_id": ev.item_id,
                            "full_text": self._full_text(sess),
                        })
            sess.done_event.set()

    @staticmethod
    def _full_text(sess):
        with sess.lock:
            parts = list(sess.final_segments)
            if sess.interim_text:
                parts.append(sess.interim_text)
        return " ".join(p.strip() for p in parts if p and p.strip())

    def chunk(self, dictation_id, owner_key, *, seq, data_b64, sample_rate=None):
        """Accept one ordered PCM16 mono chunk for an active dictation."""
        sess = self._get_owned(dictation_id, owner_key)
        if type(seq) is not int or seq < 0:
            raise DictationError("seq must be a nonnegative integer")
        if not isinstance(data_b64, str):
            raise DictationError("data must be a base64 string")
        try:
            pcm = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError):
            raise DictationError("data must be valid base64") from None
        if len(pcm) > MAX_CHUNK_PCM_BYTES:
            raise DictationError(
                f"chunk exceeds {MAX_CHUNK_PCM_BYTES} bytes limit"
            )
        if sample_rate is None:
            src_rate = sess.target_rate
        elif (
            type(sample_rate) is not int
            or sample_rate < MIN_SAMPLE_RATE
            or sample_rate > MAX_SAMPLE_RATE
        ):
            raise DictationError(
                f"sample_rate must be an integer between {MIN_SAMPLE_RATE} "
                f"and {MAX_SAMPLE_RATE}"
            )
        else:
            src_rate = sample_rate

        expected_bytes = (
            resampled_pcm16_bytes(len(pcm), src_rate, sess.target_rate)
            if pcm and src_rate != sess.target_rate
            else len(pcm)
        )
        if expected_bytes > MAX_DICTATION_BYTES:
            raise DictationError(
                "dictation audio buffer exceeded its 8 MiB limit"
            )

        with sess.outbound_lock:
            with sess.lock:
                if sess.closed:
                    raise DictationError("dictation has already ended")
                if seq != sess.next_seq:
                    raise DictationError(
                        f"out-of-order chunk seq {seq}; expected {sess.next_seq}"
                    )
                if (
                    sess.mode == "batch"
                    and len(sess.pcm_buffer) + expected_bytes > MAX_DICTATION_BYTES
                ):
                    raise DictationError(
                        "dictation audio buffer exceeded its 8 MiB limit"
                    )
                adapter = sess.live_adapter
                mode = sess.mode
            if pcm and src_rate != sess.target_rate:
                try:
                    pcm = resample_pcm16_mono(
                        pcm,
                        src_rate,
                        sess.target_rate,
                        max_output_bytes=MAX_DICTATION_BYTES,
                    )
                except ValueError:
                    raise DictationError(
                        "dictation audio buffer exceeded its 8 MiB limit"
                    ) from None
            if mode == "live" and adapter is not None and pcm:
                try:
                    adapter.send_pcm(pcm)
                except Exception as exc:
                    raise DictationError(
                        f"failed to send audio chunk: {exc}"
                    ) from None
            with sess.lock:
                if sess.closed:
                    raise DictationError("dictation has already ended")
                if mode == "batch":
                    if len(sess.pcm_buffer) + len(pcm) > MAX_DICTATION_BYTES:
                        raise DictationError(
                            "dictation audio buffer exceeded its 8 MiB limit"
                        )
                    sess.pcm_buffer.extend(pcm)
                sess.next_seq += 1
                sess.last_activity = time.monotonic()
        return {"dictation_id": dictation_id, "seq": seq, "bytes": len(pcm)}

    def _run_batch_transcription(self, sess):
        pcm = bytes(sess.pcm_buffer)
        if not pcm:
            return ""
        wav = pcm16_to_wav(pcm, sample_rate=sess.target_rate)
        duration = len(pcm) / float(sess.target_rate * 2)
        tmp_dir = tempfile.mkdtemp(prefix="zero-dictation-wav-")
        try:
            wav_path = Path(tmp_dir) / "dictation.wav"
            wav_path.write_bytes(wav)
            kwargs = {
                "media_type": "audio/wav",
                "language": sess.language,
                "duration_seconds": duration,
                "seconds": transcribe.DEFAULT_STT_TIMEOUT_SECONDS,
                "transport": self.transport,
            }
            if sess.service.api is LocalWhisper:
                kwargs["env"] = harness_free_env()
            answer = sess.service.api.transcribe(
                sess.service, str(wav_path), **kwargs,
            )
        except STTError as exc:
            raise DictationError(str(exc)) from None
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        text = answer.text
        if sess.service.key:
            text = text.replace(sess.service.key, "[redacted]")
        return text

    def stop(self, dictation_id, owner_key, *, drain_seconds=3.0):
        """Finish dictation and return raw verbatim transcript text."""
        sess = self._get_owned(dictation_id, owner_key)
        with sess.outbound_lock:
            with sess.lock:
                if sess.closed:
                    raise DictationError("dictation has already ended")
                sess.closed = True
                sess.last_activity = time.monotonic()
                adapter = sess.live_adapter
                reader = sess.reader_thread
                mode = sess.mode
            if mode == "live" and adapter is not None:
                try:
                    adapter.commit()
                except Exception:
                    pass

        if mode == "live" and adapter is not None:
            sess.done_event.wait(timeout=drain_seconds)
            adapter.close()
            if reader is not None and reader.is_alive():
                reader.join(timeout=1.0)
            final_text = self._full_text(sess)
            err = sess.error
        else:
            err = None
            try:
                final_text = self._run_batch_transcription(sess)
            except DictationError as exc:
                err = str(exc)
                final_text = ""
                self._publish(sess, {
                    "event": "error",
                    "dictation_id": dictation_id,
                    "error": err,
                })
                self._publish(sess, {
                    "event": "done",
                    "dictation_id": dictation_id,
                    "text": "",
                    "bye": "error",
                })
                raise
            if final_text:
                with sess.lock:
                    sess.final_segments = [final_text]
                    sess.interim_text = ""
                self._publish(sess, {
                    "event": "final",
                    "dictation_id": dictation_id,
                    "text": final_text,
                    "full_text": final_text,
                })

        self._publish(sess, {
            "event": "done",
            "dictation_id": dictation_id,
            "text": final_text,
            "bye": "done",
        })
        if err and not final_text:
            raise DictationError(err)
        return {
            "dictation_id": dictation_id,
            "text": final_text,
            "service": sess.service.name,
            "mode": mode,
        }

    def _abort_session(self, sess, *, reason):
        with sess.lock:
            if sess.closed:
                return
            sess.closed = True
            sess.cancelled = True
            sess.last_activity = time.monotonic()
            adapter = sess.live_adapter
        if adapter is not None:
            adapter.close()
        sess.done_event.set()
        self._publish(sess, {
            "event": "done",
            "dictation_id": sess.dictation_id,
            "text": "",
            "cancelled": True,
            "reason": reason,
            "bye": "cancelled",
        })

    def cancel(self, dictation_id, owner_key):
        """Abort an active dictation and discard any buffered audio."""
        sess = self._get_owned(dictation_id, owner_key, allow_closed=True)
        self._abort_session(sess, reason="cancelled by caller")
        return {"dictation_id": dictation_id, "cancelled": True}

    def subscribe(self, dictation_id, owner_key):
        """Return `(history_events, live_queue, unsubscribe_fn)` for streaming."""
        sess = self._get_owned(dictation_id, owner_key, allow_closed=True)
        box = queue.Queue(maxsize=EVENT_QUEUE_MAX)
        with sess.lock:
            history = list(sess.history)
            if not any("bye" in ev for ev in history):
                sess.subscribers.append(box)

        def unsubscribe():
            with sess.lock:
                if box in sess.subscribers:
                    sess.subscribers.remove(box)

        return history, box, unsubscribe

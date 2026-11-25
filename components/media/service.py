"""Server service for live dictation, deduplicated speech, and durable video ops."""

import base64
import os
from pathlib import Path
import queue
import select
import stat
import threading
import time
import weakref

from components.attachments import metadata as attachment_metadata
from components.attachments.service import SessionAttachments
from components.media import dictation, speech, transcribe
from components.media.dictation import DictationError, DictationManager
from components.media.speech import SpeechError, SpeechManager, extract_reply_text
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF, servers
from components.model.apis.stt import LocalWhisper, STTError
from components.model.apis.stt_live import MAX_SAMPLE_RATE, MIN_SAMPLE_RATE
from components.model.servers import ModelServerError
from components.sandbox.sandbox import harness_free_env
from server.services import Principal, Refused, Service

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_video_ops (
    op_key TEXT PRIMARY KEY,
    session TEXT,
    service TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    interaction_id TEXT,
    uri TEXT,
    attachment_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class MediaService(Service):
    """Server-side media service: dictation streams, Listen/Auto-speak, video ops."""

    name = "media"
    schema = MEDIA_SCHEMA
    tables = ("media_video_ops",)
    features = ("dictation", "speech")
    looking_messages = ()
    principal_messages = (
        "dictation/start",
        "dictation/chunk",
        "dictation/stop",
        "dictation/cancel",
        "dictation/watch",
        "speech/synthesize",
        "media/transcribe",
    )

    def __init__(
        self,
        *,
        inactivity_seconds=dictation.DEFAULT_INACTIVITY_SECONDS,
        transport=None,
        ws_connect=None,
        stt_services_provider=None,
        tts_services_provider=None,
    ):
        self.dictation = DictationManager(
            inactivity_seconds=inactivity_seconds,
            transport=transport,
            ws_connect=ws_connect,
            services_provider=stt_services_provider,
        )
        self.speech = SpeechManager(
            transport=transport,
            ws_connect=ws_connect,
            services_provider=tts_services_provider,
        )
        self.transport = transport
        self._sessions = weakref.WeakValueDictionary()

    def begin(self):
        self.dictation.begin()

    def shutdown(self):
        self.dictation.shutdown()

    def busy_with(self):
        reasons = {}
        reasons.update(self.dictation.busy_with())
        reasons.update(self.speech.busy_with())
        return reasons

    def messages(self):
        return {
            "dictation/start": self.dictation_start,
            "dictation/chunk": self.dictation_chunk,
            "dictation/stop": self.dictation_stop,
            "dictation/cancel": self.dictation_cancel,
            "speech/synthesize": self.speech_synthesize,
            "media/transcribe": self.media_transcribe,
        }

    def streaming_messages(self):
        return {
            "dictation/watch": self.watch_dictation,
        }

    def _check_user_caller(self, principal, action):
        if (
            principal.scope is not None
            or principal.is_command
            or principal.command_session is not None
        ):
            raise Refused(f"{action} requires a user client")

    def _session_attachments(self, sid):
        attachments_svc = self.runtime.services.get("attachments")
        if attachments_svc is None:
            raise Refused("attachments service is unavailable on this server")
        return SessionAttachments(attachments_svc, sid)

    def _session_model_and_setting(self, sid, setting_name, *, override_model=None,
                                   override_setting=None):
        cfg = self.runtime.config()
        if sid is not None:
            row = self.runtime.store.session(sid)
            if row is None:
                raise Refused(f"no session {sid!r}")
            runs = self.runtime.runs_with(sid)
            model = runs.get("model") or row.get("model") or self.runtime.model
            setting_val = runs.get(setting_name)
            if setting_val is None:
                setting_val = cfg.get(setting_name, SERVICE_CURRENT_SERVER)
            return model, setting_val
        model = override_model or self.runtime.model
        setting_val = (
            override_setting
            if override_setting is not None
            else cfg.get(setting_name, SERVICE_CURRENT_SERVER)
        )
        return model, setting_val

    def _claim_video_op(self, sid, op_key, *, service, model, prompt_sha256):
        now = time.time()
        prior = {}

        def txn(db):
            row = db.execute(
                "SELECT * FROM media_video_ops WHERE op_key = ?", (op_key,),
            ).fetchone()
            if row is not None:
                prior["row"] = dict(row)
                return
            db.execute(
                "INSERT INTO media_video_ops "
                "(op_key, session, service, model, prompt_sha256, status, "
                "interaction_id, uri, attachment_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'claimed', NULL, NULL, NULL, ?, ?)",
                (op_key, sid, service, model, prompt_sha256, now, now),
            )

        self.runtime.store.write(txn)
        return prior.get("row")

    def _update_video_op(
        self,
        op_key,
        *,
        status,
        interaction_id=None,
        uri=None,
        attachment_id=None,
    ):
        now = time.time()

        def txn(db):
            db.execute(
                "UPDATE media_video_ops SET status = ?, "
                "interaction_id = COALESCE(?, interaction_id), "
                "uri = COALESCE(?, uri), "
                "attachment_id = COALESCE(?, attachment_id), "
                "updated_at = ? WHERE op_key = ?",
                (status, interaction_id, uri, attachment_id, now, op_key),
            )

        self.runtime.store.write(txn)

    def session_built(self, sid, session):
        """Inject durable video op claim/update callbacks without importing server."""
        self._sessions[sid] = session
        session.tool_context["video_op_claim"] = (
            lambda op_key, **kw: self._claim_video_op(sid, op_key, **kw)
        )
        session.tool_context["video_op_update"] = self._update_video_op

    def turn_ended(self, sid, outcome):
        """Trigger deduplicated Auto-speak when an answered turn has auto_speak on."""
        if outcome != "answered":
            return
        session = self._sessions.get(sid)
        if session is not None:
            if session.settings.get("auto_speak") != "on":
                return
            model = getattr(session.model, "name", None) or self.runtime.model
            tts_setting = session.settings.get(
                "tts_service", SERVICE_CURRENT_SERVER,
            )
        else:
            try:
                runs = self.runtime.runs_with(sid)
            except Exception:
                return
            if runs.get("auto_speak") != "on":
                return
            model = runs.get("model") or self.runtime.model
            tts_setting = runs.get("tts_service", SERVICE_CURRENT_SERVER)
        if tts_setting == SERVICE_OFF:
            return
        records = self.runtime.read_branch(sid)
        reply_id, text = extract_reply_text(records)
        if not reply_id or not text:
            return

        def worker():
            try:
                sess_att = self._session_attachments(sid)
                self.speech.synthesize_reply(
                    sid=sid,
                    reply_entry_id=reply_id,
                    text=text,
                    model=model,
                    tts_service=tts_setting,
                    session_attachments=sess_att,
                    read_records=lambda: self.runtime.read_branch(sid),
                    append_cache_entry=lambda entry: self.runtime.append_side_record(
                        sid, entry,
                    ),
                    auto_speak=True,
                    limits=self.runtime.config(),
                    allow_over_limit=False,
                )
            except Exception:
                pass

        threading.Thread(
            target=worker, name=f"auto-speak-{sid}", daemon=True,
        ).start()

    # Dictation messages

    def dictation_start(self, request, principal=None):
        principal = principal or Principal()
        self._check_user_caller(principal, "dictation/start")
        sid = request.get("session")
        if sid is not None and not isinstance(sid, str):
            raise Refused("session must be a string")
        model = request.get("model")
        if model is not None and not isinstance(model, str):
            raise Refused("model must be a string")
        stt_setting = request.get("stt_service")
        if stt_setting is not None and not isinstance(stt_setting, str):
            raise Refused("stt_service must be a string")
        language = request.get("language")
        if language is not None and not isinstance(language, str):
            raise Refused("language must be a string")
        mode = request.get("mode")
        if mode is not None and mode not in ("live", "batch"):
            raise Refused("mode must be 'live' or 'batch'")
        if sid is not None and (model is not None or stt_setting is not None):
            raise Refused(
                "draft model and stt_service are only accepted when session is omitted"
            )
        eff_model, eff_stt = self._session_model_and_setting(
            sid, "stt_service", override_model=model, override_setting=stt_setting,
        )
        try:
            started = self.dictation.start(
                principal.owner_key,
                model=eff_model,
                stt_service=eff_stt,
                language=language,
                mode=mode,
            )
        except DictationError as exc:
            raise Refused(str(exc)) from None
        return {"ok": True, **started}

    def dictation_chunk(self, request, principal=None):
        principal = principal or Principal()
        self._check_user_caller(principal, "dictation/chunk")
        did = request.get("dictation_id")
        seq = request.get("seq")
        data = request.get("data")
        sample_rate = request.get("sample_rate")
        if sample_rate is not None and (
            type(sample_rate) is not int
            or sample_rate < MIN_SAMPLE_RATE
            or sample_rate > MAX_SAMPLE_RATE
        ):
            raise Refused(
                f"sample_rate must be an integer between {MIN_SAMPLE_RATE} "
                f"and {MAX_SAMPLE_RATE}"
            )
        try:
            ack = self.dictation.chunk(
                did,
                principal.owner_key,
                seq=seq,
                data_b64=data,
                sample_rate=sample_rate,
            )
        except DictationError as exc:
            raise Refused(str(exc)) from None
        return {"ok": True, **ack}

    def dictation_stop(self, request, principal=None):
        principal = principal or Principal()
        self._check_user_caller(principal, "dictation/stop")
        did = request.get("dictation_id")
        try:
            done = self.dictation.stop(did, principal.owner_key)
        except DictationError as exc:
            raise Refused(str(exc)) from None
        return {"ok": True, **done}

    def dictation_cancel(self, request, principal=None):
        principal = principal or Principal()
        self._check_user_caller(principal, "dictation/cancel")
        did = request.get("dictation_id")
        try:
            done = self.dictation.cancel(did, principal.owner_key)
        except DictationError as exc:
            raise Refused(str(exc)) from None
        return {"ok": True, **done}

    def watch_dictation(self, conn, request, protocol, principal, send_event):
        """Stream live dictation deltas/final segments over the Unix socket."""
        principal = principal or Principal()
        self._check_user_caller(principal, "dictation/watch")
        did = request.get("dictation_id")
        try:
            history, box, unsubscribe = self.dictation.subscribe(
                did, principal.owner_key,
            )
        except DictationError as exc:
            send_event({"ok": False, "error": str(exc)})
            return
        peer = select.poll()
        peer.register(conn, select.POLLIN | select.POLLHUP | select.POLLERR)
        try:
            for ev in history:
                send_event(ev)
                if "bye" in ev:
                    return
            while True:
                if peer.poll(0):
                    return
                try:
                    ev = box.get(timeout=0.25)
                except queue.Empty:
                    continue
                send_event(ev)
                if "bye" in ev:
                    return
        except OSError:
            return
        finally:
            unsubscribe()

    # Spoken replies & direct CLI transcription

    def speech_synthesize(self, request, principal=None):
        principal = principal or Principal()
        self._check_user_caller(principal, "speech/synthesize")
        sid = request.get("session")
        if sid is not None and not isinstance(sid, str):
            raise Refused("session must be a string")
        reply_entry_id = request.get("reply_entry_id")
        if reply_entry_id is not None and not isinstance(reply_entry_id, str):
            raise Refused("reply_entry_id must be a string")
        text = request.get("text")
        if text is not None and not isinstance(text, str):
            raise Refused("text must be a string")
        voice = request.get("voice")
        if voice is not None and not isinstance(voice, str):
            raise Refused("voice must be a string")
        service = request.get("service")
        if service is not None and not isinstance(service, str):
            raise Refused("service must be a string")
        model = request.get("model")
        if model is not None and not isinstance(model, str):
            raise Refused("model must be a string")
        allow_over_limit = request.get("allow_over_limit", False)
        if not isinstance(allow_over_limit, bool):
            raise Refused("allow_over_limit must be a boolean")

        if sid is not None and model is not None:
            raise Refused("draft model is only accepted when session is omitted")
        eff_model, eff_tts = self._session_model_and_setting(
            sid, "tts_service", override_model=model,
        )
        if sid is not None:
            records = self.runtime.read_branch(sid)
            found_id, found_text = extract_reply_text(records, reply_entry_id)
            use_text = text if (text and text.strip()) else found_text
            use_id = reply_entry_id or found_id
            if not use_id or not use_text:
                raise Refused("no assistant reply text found to speak")
            sess_att = self._session_attachments(sid)
            try:
                result = self.speech.synthesize_reply(
                    sid=sid,
                    reply_entry_id=use_id,
                    text=use_text,
                    model=eff_model,
                    tts_service=eff_tts,
                    session_attachments=sess_att,
                    read_records=lambda: self.runtime.read_branch(sid),
                    append_cache_entry=lambda entry: self.runtime.append_side_record(
                        sid, entry,
                    ),
                    service=service,
                    voice=voice,
                    limits=self.runtime.config(),
                    allow_over_limit=allow_over_limit,
                )
            except SpeechError as exc:
                raise Refused(str(exc)) from None
            return {"ok": True, **result}

        if not text or not text.strip():
            raise Refused("speech/synthesize needs a session or text")
        try:
            spoken = self.speech.synthesize_text(
                text,
                model=eff_model,
                tts_service=eff_tts,
                service=service,
                voice=voice,
            )
        except SpeechError as exc:
            raise Refused(str(exc)) from None
        return {
            "ok": True,
            "audio_base64": base64.b64encode(spoken["audio"]).decode("ascii"),
            "media_type": spoken["media_type"],
            "size_bytes": spoken["size_bytes"],
            "service": spoken["service"],
            "model": spoken["model"],
            "voice": spoken["voice"],
            "cached": False,
        }

    def media_transcribe(self, request, principal=None):
        """Transcribe a local audio path or attachment directly (for CLI/TUI)."""
        principal = principal or Principal()
        self._check_user_caller(principal, "media/transcribe")
        raw_path = request.get("path")
        attachment_id = request.get("attachment_id")
        if bool(raw_path) == bool(attachment_id):
            raise Refused(
                "media/transcribe requires exactly one of path or attachment_id"
            )
        sid = request.get("session")
        model = request.get("model")
        service_req = request.get("service")
        language = request.get("language")
        if raw_path is not None:
            if not principal.can_add_host_path:
                raise Refused(
                    "media/transcribe with a host path requires a direct local client"
                )
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise Refused("path must be a nonempty string")
            target = Path(raw_path).expanduser().resolve()
            try:
                with target.open("rb") as fh:
                    st = os.fstat(fh.fileno())
                    if not stat.S_ISREG(st.st_mode):
                        raise Refused(f"{raw_path} is not a readable file")
                    file_size = st.st_size
                    head = fh.read(4096)
            except OSError:
                raise Refused(f"{raw_path} is not a readable file") from None
            media_type = attachment_metadata.media_type(str(target), head)
            audio_target = target
            audio_path = str(target)
        else:
            if not isinstance(attachment_id, str) or not attachment_id:
                raise Refused("attachment_id must be a nonempty string")
            att_svc = self.runtime.services.get("attachments")
            if att_svc is None:
                raise Refused("attachments service is unavailable")
            row, _ = att_svc._authorize_read(attachment_id, principal, session=sid)
            blob = att_svc.store.blob_path(row["sha256"])
            try:
                st = blob.stat()
                if not stat.S_ISREG(st.st_mode):
                    raise Refused("attachment blob is missing")
                file_size = st.st_size
            except OSError:
                raise Refused("attachment blob is missing") from None
            audio_target = blob
            audio_path = str(blob)
            media_type = row["media_type"] or "application/octet-stream"

        if sid is not None and model is not None:
            raise Refused("draft model is only accepted when session is omitted")
        eff_model, eff_stt = self._session_model_and_setting(
            sid, "stt_service", override_model=model,
        )
        model_server = None
        if eff_model:
            try:
                model_server, _ = servers.model_address(eff_model, servers.table())
            except ModelServerError:
                model_server = None
        try:
            available = self.dictation._services_provider()
        except ModelServerError as exc:
            raise Refused(str(exc)) from None
        chosen, reason = transcribe.select_stt_service(
            available, model_server, eff_stt, service_req,
        )
        if chosen is None:
            raise Refused(reason)
        max_bytes = getattr(chosen.api, "MAX_BYTES", None)
        if max_bytes is not None and file_size > max_bytes:
            limit_mb = max_bytes / (1024 * 1024)
            actual_mb = file_size / (1024 * 1024)
            raise Refused(
                f"{chosen.name} accepts audio up to {limit_mb:g} MiB; "
                f"this file is {actual_mb:.1f} MiB."
            )
        probed = attachment_metadata.probe(audio_target, media_type)
        kwargs = {
            "media_type": media_type,
            "language": language,
            "duration_seconds": probed.get("duration_seconds"),
            "seconds": transcribe.DEFAULT_STT_TIMEOUT_SECONDS,
            "transport": self.transport,
        }
        if chosen.api is LocalWhisper:
            kwargs["env"] = harness_free_env()
        try:
            answer = chosen.api.transcribe(chosen, audio_path, **kwargs)
        except STTError as exc:
            raise Refused(str(exc)) from None
        text = answer.text
        if chosen.key:
            text = text.replace(chosen.key, "[redacted]")
        reply = {
            "ok": True,
            "text": text,
            "service": chosen.name,
            "media_type": media_type,
        }
        if answer.model:
            reply["model"] = answer.model
        if answer.language:
            reply["language"] = answer.language
        if answer.duration_seconds is not None:
            reply["duration_seconds"] = round(float(answer.duration_seconds), 3)
        return reply

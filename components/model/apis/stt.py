"""Speech-to-text (STT) utility API adapters: OpenAI, Google, DeepSeek, local.

Every adapter returns raw verbatim transcript text directly with no extra LLM
rewrite pass, enforces explicit provider size/format/duration caps before
upload, and raises `STTError` without leaking keys or raw auth headers.
"""

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

import httpx2

import paths as home_paths
from components.model.apis.http_errors import error_detail, response_error_text


class STTError(Exception):
    """A transcription error safe to show without keys or raw headers."""


@dataclass(frozen=True)
class TranscriptionResponse:
    """Raw verbatim speech-to-text output from one STT service."""

    text: str
    language: str | None = None
    duration_seconds: float | None = None
    model: str | None = None
    billed: bool | None = None


def _http_error(reply, key):
    return STTError(response_error_text(
        reply, "The service redirected the request.", key=key,
    ))


def _read_audio_bytes(audio_path, *, max_bytes, label):
    path = Path(audio_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise STTError(f"Could not read audio file: {exc}.") from None
    if size <= 0:
        raise STTError("Audio file is empty.")
    if max_bytes is not None and size > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        actual_mb = size / (1024 * 1024)
        raise STTError(
            f"{label} accepts audio up to {limit_mb:g} MiB; "
            f"this file is {actual_mb:.1f} MiB."
        )
    return path.read_bytes()


def _normalize_mime(media_type):
    if not isinstance(media_type, str) or not media_type.strip():
        return "application/octet-stream"
    return media_type.split(";", 1)[0].strip().lower()


def _extract_interactions_text(payload):
    """Extract verbatim text from Google Interactions `steps[].content` or `outputs`.

    Distinguishes an explicit empty transcript (`""`) from a missing model output
    (which raises `STTError` so a billed/malformed response never looks like an
    empty success).
    """
    if not isinstance(payload, dict):
        raise STTError("The transcription response was not a JSON object.")
    if isinstance(payload.get("text"), str):
        return payload["text"].strip()
    pieces = []
    found = False
    steps = payload.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict) or step.get("type") != "model_output":
                continue
            if isinstance(step.get("text"), str):
                found = True
                pieces.append(step["text"])
                continue
            content = step.get("content")
            if isinstance(content, str):
                found = True
                pieces.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        found = True
                        pieces.append(part["text"])
    if not found:
        outputs = payload.get("outputs")
        if isinstance(outputs, list):
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    found = True
                    pieces.append(item["text"])
                    continue
                content = item.get("content")
                if isinstance(content, str):
                    found = True
                    pieces.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(
                            part.get("text"), str,
                        ):
                            found = True
                            pieces.append(part["text"])
    if not found:
        raise STTError("The transcription interaction returned no model output.")
    return "".join(pieces).strip()


class OpenAIAudio:
    """OpenAI batch transcription (`POST /v1/audio/transcriptions`, multipart)."""

    NAME = "openai_audio"
    REQUIRES_MODEL = True
    MAX_BYTES = 25 * 1024 * 1024

    @staticmethod
    def endpoint(base_url):
        base = base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/audio/transcriptions"
        return f"{base}/v1/audio/transcriptions"

    @classmethod
    def transcribe(
        cls,
        service,
        audio_path,
        *,
        media_type,
        language=None,
        prompt=None,
        duration_seconds=None,
        seconds=120,
        transport=None,
        check=None,
    ):
        raw = _read_audio_bytes(
            audio_path, max_bytes=cls.MAX_BYTES, label="OpenAI Audio API",
        )
        mime = _normalize_mime(media_type)
        filename = Path(audio_path).name or "audio.wav"
        data = {"model": service.model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {service.key}"}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(
                    service.url,
                    data=data,
                    files={"file": (filename, raw, mime)},
                    headers=headers,
                )
                if not reply.is_success:
                    raise _http_error(reply, service.key)
                ctype = reply.headers.get("content-type", "").lower()
                if "json" in ctype or reply.text.lstrip().startswith("{"):
                    payload = reply.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("text"), str,
                    ):
                        raise STTError(
                            "The transcription response did not include text."
                        )
                    dur = payload.get("duration")
                    out_dur = (
                        float(dur)
                        if isinstance(dur, (int, float)) and not isinstance(dur, bool)
                        else duration_seconds
                    )
                    lang = payload.get("language")
                    return TranscriptionResponse(
                        text=payload["text"].strip(),
                        language=lang if isinstance(lang, str) and lang else language,
                        duration_seconds=out_dur,
                        model=service.model,
                        billed=True,
                    )
                return TranscriptionResponse(
                    text=reply.text.strip(),
                    language=language,
                    duration_seconds=duration_seconds,
                    model=service.model,
                    billed=True,
                )
        except STTError:
            raise
        except httpx2.TimeoutException:
            raise STTError(
                f"The transcription service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise STTError(
                "The connection to the transcription service failed."
            ) from None
        except ValueError:
            raise STTError(
                "The transcription service returned invalid JSON."
            ) from None


class GoogleAudio:
    """Google batch transcription (`gemini-3.5-transcribe` via Interactions API)."""

    NAME = "google_audio"
    REQUIRES_MODEL = True
    MAX_BYTES = 20 * 1024 * 1024

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/interactions"

    @classmethod
    def transcribe(
        cls,
        service,
        audio_path,
        *,
        media_type,
        language=None,
        prompt=None,
        duration_seconds=None,
        seconds=120,
        transport=None,
        check=None,
    ):
        raw = _read_audio_bytes(
            audio_path, max_bytes=cls.MAX_BYTES, label="Google Audio Interactions API",
        )
        mime = _normalize_mime(media_type)
        instruction = "Transcribe the audio verbatim. Output only the transcript text."
        if language:
            instruction += f" Language hint: {language}."
        if prompt:
            instruction += f" Context: {prompt}"
        body = {
            "model": service.model,
            "store": False,
            "input": [{
                "type": "user_input",
                "content": [
                    {
                        "type": "audio",
                        "data": base64.b64encode(raw).decode("ascii"),
                        "mime_type": mime,
                    },
                    {"type": "text", "text": instruction},
                ],
            }],
        }
        headers = {"x-goog-api-key": service.key}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(service.url, json=body, headers=headers)
                if not reply.is_success:
                    raise _http_error(reply, service.key)
                payload = reply.json()
        except STTError:
            raise
        except httpx2.TimeoutException:
            raise STTError(
                f"The transcription service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise STTError(
                "The connection to the transcription service failed."
            ) from None
        except ValueError:
            raise STTError(
                "The transcription service returned invalid JSON."
            ) from None
        if isinstance(payload, dict) and payload.get("status") in (
            "failed", "cancelled",
        ):
            status = payload.get("status")
            detail = error_detail({"error": payload.get("error")}, key=service.key)
            raise STTError(
                f"The transcription interaction did not complete ({status})."
                + (f" {detail}" if detail else "")
            )
        text = _extract_interactions_text(payload)
        return TranscriptionResponse(
            text=text,
            language=language,
            duration_seconds=duration_seconds,
            model=service.model,
            billed=True,
        )


class DeepSeekAudio:
    """DeepSeek direct ASR (`POST /v1/tools/asr`, no model param, WAV/Opus)."""

    NAME = "deepseek_audio"
    REQUIRES_MODEL = False
    MAX_BYTES = 8 * 1024 * 1024
    MAX_DURATION_SECONDS = 120.0
    ACCEPTED_MEDIA_TYPES = frozenset({
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/ogg",
        "audio/opus",
    })

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/").removesuffix("/v1") + "/v1/tools/asr"

    @classmethod
    def transcribe(
        cls,
        service,
        audio_path,
        *,
        media_type,
        language=None,
        prompt=None,
        duration_seconds=None,
        seconds=120,
        transport=None,
        check=None,
    ):
        mime = _normalize_mime(media_type)
        if mime not in cls.ACCEPTED_MEDIA_TYPES:
            raise STTError(
                f"DeepSeek ASR accepts WAV or Opus audio (got {mime}); "
                "convert the file or select another STT service."
            )
        if (
            isinstance(duration_seconds, (int, float))
            and duration_seconds > cls.MAX_DURATION_SECONDS
        ):
            raise STTError(
                f"DeepSeek ASR accepts audio up to {cls.MAX_DURATION_SECONDS:g}s; "
                f"this file is {duration_seconds:g}s."
            )
        raw = _read_audio_bytes(
            audio_path, max_bytes=cls.MAX_BYTES, label="DeepSeek ASR",
        )
        filename = Path(audio_path).name or "audio.wav"
        headers = {"Authorization": f"Bearer {service.key}"}
        try:
            with httpx2.Client(
                timeout=seconds, transport=transport, follow_redirects=False,
            ) as client:
                if check is not None:
                    check()
                reply = client.post(
                    service.url,
                    files={"file": (filename, raw, mime)},
                    headers=headers,
                )
                if not reply.is_success:
                    raise _http_error(reply, service.key)
                payload = reply.json()
        except STTError:
            raise
        except httpx2.TimeoutException:
            raise STTError(
                f"The transcription service gave no response within {seconds:g} s."
            ) from None
        except httpx2.HTTPError:
            raise STTError(
                "The connection to the transcription service failed."
            ) from None
        except ValueError:
            raise STTError(
                "The transcription service returned invalid JSON."
            ) from None
        if not isinstance(payload, dict):
            raise STTError("The transcription response was not a JSON object.")
        raw_text = (
            payload.get("text")
            if isinstance(payload.get("text"), str)
            else payload.get("transcript")
            if isinstance(payload.get("transcript"), str)
            else payload.get("result")
            if isinstance(payload.get("result"), str)
            else None
        )
        if raw_text is None:
            raise STTError("The transcription response did not include text.")
        lang = payload.get("language")
        dur_ms = payload.get("duration_ms")
        if isinstance(dur_ms, (int, float)) and not isinstance(dur_ms, bool):
            out_dur = float(dur_ms) / 1000.0
        else:
            out_dur = duration_seconds
        return TranscriptionResponse(
            text=raw_text.strip(),
            language=lang if isinstance(lang, str) and lang else None,
            duration_seconds=out_dur,
            model=None,
            billed=True,
        )


class LocalWhisper:
    """Opportunistic local offline transcription (`whisper-cli` or `whisper`)."""

    NAME = "local_whisper"
    TITLE = "Local Whisper"
    URL = ""
    REQUIRES_MODEL = False
    _BIN_CACHE = None

    @classmethod
    def binary(cls, which=None):
        if which is None:
            now = time.monotonic()
            key = (os.environ.get("PATH", ""), id(shutil.which))
            cached = cls._BIN_CACHE
            if cached is not None and cached[0] == key and now - cached[1] < 5.0:
                return cached[2]
            found_pair = (None, None)
            for name in ("whisper-cli", "whisper"):
                found = shutil.which(name)
                if found:
                    found_pair = (name, found)
                    break
            cls._BIN_CACHE = (key, now, found_pair)
            return found_pair
        for name in ("whisper-cli", "whisper"):
            found = which(name)
            if found:
                return name, found
        return None, None

    @classmethod
    def available(cls, which=None):
        return cls.binary(which)[0] is not None

    MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024

    @staticmethod
    def _safe_name(path, fallback):
        raw = Path(path).name or fallback
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).lstrip(".")
        return cleaned[:120] or fallback

    @classmethod
    def _read_output_text(cls, out_dir, filename, *, max_bytes=None):
        """Read a regular output file under `out_dir` without following symlinks."""
        limit = cls.MAX_TRANSCRIPT_BYTES if max_bytes is None else max_bytes
        if (
            not filename
            or "/" in filename
            or "\\" in filename
            or filename in (".", "..")
        ):
            raise STTError(f"Local Whisper output {filename!r} is invalid.")
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(str(out_dir), dir_flags)
        except OSError as exc:
            raise STTError(
                f"Local Whisper output directory unavailable: {exc}."
            ) from None
        try:
            file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(filename, file_flags, dir_fd=dir_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise STTError(
                    f"Local Whisper output {filename!r} is not a regular file: {exc}."
                ) from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise STTError(
                        f"Local Whisper output {filename!r} is not a regular file."
                    )
                if st.st_size > limit:
                    raise STTError(
                        f"Local Whisper output {filename!r} exceeded {limit:,} bytes."
                    )
                chunks = []
                total = 0
                with os.fdopen(fd, "rb") as fh:
                    fd = -1
                    while True:
                        chunk = fh.read(min(65536, limit - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > limit:
                            raise STTError(
                                f"Local Whisper output {filename!r} exceeded "
                                f"{limit:,} bytes."
                            )
                        chunks.append(chunk)
            finally:
                if fd >= 0:
                    os.close(fd)
        finally:
            os.close(dir_fd)
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    @classmethod
    def _build_bwrap_command(
        cls,
        *,
        bwrap_exe,
        kind,
        exe,
        audio_path,
        out_dir,
        model_path=None,
        language=None,
    ):
        """Wrap local Whisper inside a bounded, networkless bwrap sandbox."""
        resolved_audio = Path(audio_path).resolve()
        if not resolved_audio.is_file():
            raise STTError(f"Audio file does not exist: {audio_path}.")
        resolved_exe = Path(exe).resolve()
        in_name = cls._safe_name(resolved_audio, "audio.wav")
        guest_audio = f"/tmp/in/{in_name}"
        guest_out = "/tmp/out"

        bwrap_cmd = [
            bwrap_exe,
            "--ro-bind", "/", "/",
            "--dev", "/dev",
            "--proc", "/proc",
        ]
        home_dir = Path.home().resolve()
        zero_home = home_paths.home().resolve()
        excluded_roots = {
            Path("/"),
            Path("/usr"),
            home_dir,
            (home_dir / ".local").resolve(),
            zero_home,
        }

        def _is_excluded(cand):
            if cand in excluded_roots:
                return True
            if home_dir.is_relative_to(cand) or zero_home.is_relative_to(cand):
                return True
            if cand.is_relative_to(zero_home):
                return True
            return False

        covered_dirs = []
        for folder in (home_dir, Path("/tmp"), Path("/var/tmp"), Path("/run")):
            if folder.exists():
                bwrap_cmd += ["--tmpfs", str(folder)]
                covered_dirs.append(str(folder))
        # Re-expose the Whisper executable (and isolated venv prefix) RO without
        # ever re-exposing $HOME, ~/.local, ZERO_HOME, or any ancestor of them.
        for candidate in dict.fromkeys((str(resolved_exe), str(exe))):
            cand_path = Path(candidate)
            if cand_path.exists() and not _is_excluded(cand_path.resolve()):
                bwrap_cmd += ["--ro-bind-try", str(cand_path), str(cand_path)]
        exe_prefix = resolved_exe.parent.parent
        if (
            exe_prefix.exists()
            and exe_prefix.is_relative_to(home_dir)
            and not _is_excluded(exe_prefix)
        ):
            bwrap_cmd += ["--ro-bind-try", str(exe_prefix), str(exe_prefix)]
        if zero_home.exists() and str(zero_home) not in covered_dirs:
            bwrap_cmd += ["--tmpfs", str(zero_home)]
            covered_dirs.append(str(zero_home))
        for mount in reversed(covered_dirs):
            bwrap_cmd += ["--remount-ro", mount]

        # Mount only the private output dir writable and audio/model RO.
        bwrap_cmd += [
            "--bind", str(out_dir), guest_out,
            "--ro-bind", str(resolved_audio), guest_audio,
        ]
        guest_model = None
        if model_path:
            resolved_model = Path(model_path).expanduser().resolve()
            if not resolved_model.is_file():
                raise STTError(
                    f"Local Whisper model file does not exist: {model_path}."
                )
            model_name = cls._safe_name(resolved_model, "model.bin")
            guest_model = f"/tmp/model/{model_name}"
            bwrap_cmd += ["--ro-bind", str(resolved_model), guest_model]

        if kind == "whisper-cli":
            inner_cmd = [
                exe, "-f", guest_audio, "-nt", "-otxt",
                "-of", f"{guest_out}/transcript",
            ]
            if guest_model:
                inner_cmd += ["-m", guest_model]
            if language:
                inner_cmd += ["-l", language]
            expected_txt = out_dir / "transcript.txt"
        else:
            inner_cmd = [
                exe, guest_audio, "--output_format", "txt",
                "--output_dir", guest_out,
            ]
            if language:
                inner_cmd += ["--language", language]
            expected_txt = out_dir / f"{Path(in_name).stem}.txt"

        bwrap_cmd += [
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-net",
            "--cap-drop",
            "ALL",
            "--new-session",
            "--die-with-parent",
            "--disable-userns",
            "--chdir",
            guest_out,
            "--",
            *inner_cmd,
        ]
        return bwrap_cmd, expected_txt

    @classmethod
    def transcribe(
        cls,
        service,
        audio_path,
        *,
        media_type,
        language=None,
        prompt=None,
        duration_seconds=None,
        seconds=120,
        transport=None,
        check=None,
        run=None,
        which=None,
        env=None,
    ):
        run = run or subprocess.run
        kind, exe = cls.binary(which)
        if not exe:
            raise STTError(
                "Neither `whisper-cli` nor `whisper` is installed on PATH."
            )
        bwrap_exe = which("bwrap") if which is not None else shutil.which("bwrap")
        if not bwrap_exe:
            raise STTError(
                "bwrap is not installed; refusing to run local Whisper unsandboxed."
            )
        if check is not None:
            check()
        model_path = (
            os.environ.get("ZERO_WHISPER_MODEL")
            or os.environ.get("WHISPER_MODEL")
        )
        with tempfile.TemporaryDirectory(prefix="zero-whisper-") as tmp_dir:
            out_dir = Path(tmp_dir) / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd, expected_txt = cls._build_bwrap_command(
                bwrap_exe=bwrap_exe,
                kind=kind,
                exe=exe,
                audio_path=audio_path,
                out_dir=out_dir,
                model_path=model_path if kind == "whisper-cli" else None,
                language=language,
            )
            run_env = dict(env) if env is not None else {}
            run_env["HOME"] = "/tmp/out"
            run_env["TMPDIR"] = "/tmp/out"
            run_env["XDG_CACHE_HOME"] = "/tmp/out"
            run_env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
            try:
                done = run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=seconds,
                    env=run_env,
                )
            except subprocess.TimeoutExpired:
                raise STTError(
                    f"Local {kind} did not finish within {seconds:g} s."
                ) from None
            except OSError as exc:
                raise STTError(f"Local {kind} could not run: {exc}.") from None
            if done.returncode != 0:
                err = (
                    (done.stderr or "").strip()[-240:]
                    or f"exit code {done.returncode}"
                )
                raise STTError(f"Local {kind} failed: {err}")
            file_text = cls._read_output_text(out_dir, expected_txt.name)
            if file_text is not None:
                text = file_text
            else:
                text = (done.stdout or "").strip()
        return TranscriptionResponse(
            text=text,
            language=language,
            duration_seconds=duration_seconds,
            model=kind,
            billed=False,
        )

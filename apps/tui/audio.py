"""Opportunistic OS audio capture and playback helpers for TUI/CLI.

Uses native Linux binaries when installed (`pw-record` / `arecord` for capture;
`pw-play` / `paplay` / `ffplay` / `aplay` for playback) with graceful None
returns on SSH or headless hosts where no local audio stack is available.
"""

from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

CAPTURE_BINARIES = ("pw-record", "arecord")
PLAYBACK_BINARIES = ("pw-play", "paplay", "ffplay", "aplay")


def capture_binary(which=shutil.which):
    """Return `(name, path)` for an available PCM capture command, or `(None, None)`."""
    for name in CAPTURE_BINARIES:
        found = which(name)
        if found:
            return name, found
    return None, None


def playback_binary(which=shutil.which):
    """Return `(name, path)` for an available audio player, or `(None, None)`."""
    for name in PLAYBACK_BINARIES:
        found = which(name)
        if found:
            return name, found
    return None, None


def capture_command(kind, exe, *, sample_rate=16000):
    """Build argv producing raw 16-bit LE mono PCM on stdout."""
    rate = str(int(sample_rate))
    if kind == "pw-record":
        return [
            exe, "--format", "s16", "--rate", rate, "--channels", "1", "-",
        ]
    return [
        exe, "-q", "-f", "S16_LE", "-r", rate, "-c", "1", "-t", "raw", "-",
    ]


def playback_command(kind, exe, audio_path):
    """Build argv playing a WAV or audio file cleanly without UI windows."""
    target = str(audio_path)
    if kind == "ffplay":
        return [exe, "-nodisp", "-autoexit", "-loglevel", "quiet", target]
    if kind == "aplay":
        return [exe, "-q", target]
    return [exe, target]


class MicCapture:
    """Non-blocking subprocess PCM16 mono mic capture delivering chunks."""

    def __init__(
        self,
        *,
        sample_rate=16000,
        chunk_bytes=6400,
        which=shutil.which,
        popen=subprocess.Popen,
    ):
        self.sample_rate = int(sample_rate)
        self.chunk_bytes = int(chunk_bytes)
        self._which = which
        self._popen = popen
        self._proc = None
        self._stop = threading.Event()
        self._thread = None

    def start(self, on_chunk, on_error=None):
        """Start capturing PCM chunks on a daemon thread; return binary name."""
        kind, exe = capture_binary(self._which)
        if not exe:
            return None
        cmd = capture_command(kind, exe, sample_rate=self.sample_rate)
        try:
            self._proc = self._popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            if on_error is not None:
                on_error(exc)
            return None

        def reader():
            try:
                stream = self._proc.stdout
                while not self._stop.is_set() and stream is not None:
                    data = stream.read(self.chunk_bytes)
                    if not data:
                        break
                    on_chunk(data)
            except Exception as exc:
                if on_error is not None and not self._stop.is_set():
                    on_error(exc)

        self._thread = threading.Thread(
            target=reader, name="tui-mic-capture", daemon=True,
        )
        self._thread.start()
        return kind

    def stop(self):
        """Stop capturing and terminate the subprocess cleanly."""
        self._stop.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)


def play_audio_bytes(
    audio_bytes,
    *,
    suffix=".wav",
    which=shutil.which,
    run=subprocess.run,
):
    """Play audio bytes synchronously via an OS player; return player name or None."""
    kind, exe = playback_binary(which)
    if not exe:
        return None
    tmp_dir = tempfile.mkdtemp(prefix="zero-audio-play-")
    try:
        target = Path(tmp_dir) / f"reply{suffix}"
        target.write_bytes(audio_bytes)
        cmd = playback_command(kind, exe, target)
        done = run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        return kind if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

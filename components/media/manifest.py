"""Component manifest for media (speech, dictation, and media generation)."""

import shutil

import settings
from components.media import SETTINGS, entries, tools as media_tools, transcribe
from components.media.service import MediaService
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF
from components.model import servers as model_servers
from components.model.servers import ModelServerError
from core import tools
from core.components import ComponentManifest, ServiceBuild, SessionBuild

AUDIO_CAPTURE_BINARIES = ("pw-record", "arecord")
AUDIO_PLAYBACK_BINARIES = ("pw-play", "paplay", "ffplay", "aplay")


def register():
    tools.add(*transcribe.TOOLS, *media_tools.TOOLS)


def prepare_session(build: SessionBuild):
    media = build.options.get("media")
    build.tool_context["media"] = (
        media if media is not None else transcribe.Media()
    )
    build.tool_context["media_ask"] = build.ask
    for key in ("stt_service", "tts_service", "image_service", "video_service"):
        if key in build.options:
            chosen = build.options[key]
        elif key in build.settings:
            chosen = build.settings[key]
        else:
            chosen = build.limits[key]
        if chosen is None:
            chosen = SERVICE_CURRENT_SERVER
        build.settings[key] = chosen
    if "auto_speak" in build.options:
        auto_speak = build.options["auto_speak"]
    elif "auto_speak" in build.settings:
        auto_speak = build.settings["auto_speak"]
    else:
        auto_speak = build.limits["auto_speak"]
    build.settings["auto_speak"] = auto_speak or "off"


def after_session(session, build: SessionBuild):
    session.tool_context["media_session"] = session
    media = session.tool_context.get("media")
    if media is not None:
        media.bind_session(session)


def service(build: ServiceBuild):
    return MediaService(
        transport=build.options.get("media_transport"),
        ws_connect=build.options.get("media_ws_connect"),
    )


def stt_service_status():
    """Default session speech-to-text selection without contacting any service."""
    try:
        config = settings.load()
        model = config["model"]
        server = (
            model_servers.model_address(model, model_servers.table())[0]
            if model
            else None
        )
        available = transcribe.available_stt_services()
        chosen_setting = config["stt_service"]
        chosen, reason = transcribe.select_stt_service(
            available, server, chosen_setting,
        )
    except (settings.SettingsError, ModelServerError):
        return False, "Speech-to-text service could not read its configuration."
    choices = ", ".join(available) or "none"
    if chosen_setting == SERVICE_OFF:
        return True, f"Speech-to-text is off. Available services are {choices}."
    if chosen is None:
        return False, f"Speech-to-text. {reason} Available services are {choices}."
    return True, (
        f"Speech-to-text uses {chosen.label}. {reason}. "
        f"Available services are {choices}."
    )


def audio_binary_notes(which=shutil.which):
    """Report optional OS audio capture and playback binaries for TUI/CLI."""
    lines = []
    cap_found = next(
        ((name, path) for name in AUDIO_CAPTURE_BINARIES if (path := which(name))),
        None,
    )
    if cap_found:
        lines.append((
            "✓",
            f"Optional {cap_found[0]} (terminal audio capture): {cap_found[1]}",
        ))
    else:
        lines.append((
            "!",
            "Optional pw-record / arecord not found (terminal mic capture "
            "unavailable; file-path transcription and browser mic still work)",
        ))
    play_found = next(
        ((name, path) for name in AUDIO_PLAYBACK_BINARIES if (path := which(name))),
        None,
    )
    if play_found:
        lines.append((
            "✓",
            f"Optional {play_found[0]} (terminal audio playback): {play_found[1]}",
        ))
    else:
        lines.append((
            "!",
            "Optional pw-play / paplay / ffplay / aplay not found (terminal "
            "audio playback unavailable; browser Listen and --output still work)",
        ))
    return lines


def doctor(workspace=None):
    """Report optional local STT and OS audio binaries plus STT routing."""
    lines = []
    kind, found = transcribe.local_stt_binary(shutil.which)
    if found:
        lines.append((
            "✓",
            f"Optional {kind} (local offline speech-to-text): {found}",
        ))
    else:
        lines.append((
            "!",
            "Optional whisper-cli / whisper not found (local offline "
            "speech-to-text unavailable; configured server STT still works)",
        ))
    lines.extend(audio_binary_notes(shutil.which))
    ready, status = stt_service_status()
    lines.append(("✓" if ready else "!", status))
    return lines


MANIFEST = ComponentManifest(
    name="media",
    order=78,
    settings=SETTINGS,
    entries=entries.ENTRY_SPECS,
    prepare_session=prepare_session,
    after_session=after_session,
    service=service,
    doctor=doctor,
)

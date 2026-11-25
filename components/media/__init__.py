"""Media component: speech-to-text, text-to-speech, and media generation."""

from settings import Setting

AUTO_SPEAK_CHOICES = ("off", "on")

SETTINGS = (
    Setting(
        "auto_speak",
        "Default",
        "Auto-speak replies",
        "off",
        "choice",
        choices=AUTO_SPEAK_CHOICES,
        switchable=True,
        shown=True,
    ),
)

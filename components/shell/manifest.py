"""Component manifest for shell."""

from core.components import ComponentManifest
from components.shell import SETTINGS

MANIFEST = ComponentManifest(
    name="shell",
    order=65,
    settings=SETTINGS,
)

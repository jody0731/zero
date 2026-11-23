"""Vault component manifest: encrypted credential storage and private asks."""

import paths
from core.components import ComponentManifest
from components.vault.service import VaultService
from components.vault.store import Vault


def _service(build):
    make_vault = build.options.get("make_vault")
    return VaultService() if make_vault is None else VaultService(make_vault)


def _doctor(workspace=None):
    try:
        return [("!", Vault(paths.home()).status())]
    except Exception:
        return [("!", "Vault: unavailable; its existing file was left unchanged.")]


MANIFEST = ComponentManifest(
    name="vault",
    order=5,
    system_sources=("hooks.py",),
    service=_service,
    doctor=_doctor,
)

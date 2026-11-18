"""One authenticated encrypted file, including labels, replaced atomically."""

import copy
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from paths import changing, write_whole
from vault.keyring import Keyring

FILE_NAME = "vault.enc"
KEY_NAME = "vault.key"
FILE_FORMAT = 1
KINDS = ("login", "key", "card", "identity", "note")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")


class VaultError(Exception):
    """A failure safe to show: neither values nor cryptographic details."""


def now():
    return datetime.now(timezone.utc).isoformat()


def site(value):
    parsed = urlsplit(value if "://" in value else "//" + value)
    return (parsed.hostname or value).casefold().rstrip(".")


def check_labels(labels):
    if not isinstance(labels, dict):
        raise VaultError("The entry's labels must be an object.")
    if set(labels) != {"name", "kind", "description", "sites", "username",
                       "parts"}:
        raise VaultError("The entry needs name, kind, description, sites, "
                         "username and parts.")
    if not isinstance(labels["name"], str) or not NAME.fullmatch(labels["name"]):
        raise VaultError("An entry name uses letters, numbers, dots, dashes "
                         "and underscores, up to 80 characters.")
    if labels["kind"] not in KINDS:
        raise VaultError("The entry kind is login, key, card, identity or note.")
    if any(not isinstance(labels[k], str) for k in ("description", "username")):
        raise VaultError("Description and username must be text.")
    for key in ("sites", "parts"):
        items = labels[key]
        if (not isinstance(items, list)
                or any(not isinstance(item, str) or not item.strip()
                       for item in items) or len(set(items)) != len(items)):
            raise VaultError("Sites and parts must be lists of distinct nonempty text.")
    if not labels["parts"]:
        raise VaultError("At least one private field is required.")
    if any(not NAME.fullmatch(part) for part in labels["parts"]):
        raise VaultError("Part names use the same characters as entry names.")
    if len(json.dumps(labels)) > 16000 or len(labels["parts"]) > 32:
        raise VaultError("The entry's labels are too large.")


class Vault:
    def __init__(self, home, keyring=None):
        self.home = Path(home)
        self.path = self.home / FILE_NAME
        self.key_path = self.home / KEY_NAME
        self.keyring = keyring if keyring is not None else Keyring(self.home)
        self.lock = threading.RLock()
        self.cipher = None
        self.key_storage = None
        self.key_value = None
        self.retired = {}
        self.cached_stamp = None
        self.cached_data = None
        self.cached_values = None

    def _cipher(self, create=False):
        if self.cipher is not None:
            return self.cipher
        if self.key_path.exists():
            key = self.key_path.read_bytes()
            self.key_storage = "fallback key file"
        else:
            try:
                key = self.keyring.get()
            except Exception:
                key = None
            if key:
                self.key_storage = "OS keyring"
            elif self.path.exists() or not create:
                raise VaultError("The vault key is unavailable; unlock the OS "
                                 "keyring or restore its original key file.")
            else:
                key = Fernet.generate_key()
                try:
                    self.keyring.set(key.decode())
                    if self.keyring.get() != key.decode():
                        raise OSError("keyring write could not be verified")
                    self.key_storage = "OS keyring"
                except Exception:
                    write_whole(self.key_path, key, mode=0o600)
                    self.key_storage = "fallback key file"
        try:
            self.cipher = Fernet(key)
            self.key_value = key.decode() if isinstance(key, bytes) else key
        except (ValueError, TypeError):
            raise VaultError("The vault key file is invalid.") from None
        return self.cipher

    def _read(self):
        if not self.path.exists():
            return {"file_format": FILE_FORMAT, "entries": {}, "receipts": {}}
        stat = self.path.stat()
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if stamp == self.cached_stamp:
            return copy.deepcopy(self.cached_data)
        try:
            data = json.loads(self._cipher().decrypt(self.path.read_bytes()))
            if data.get("file_format") != FILE_FORMAT:
                raise VaultError("This vault format is not supported; "
                                 "the file was left unchanged.")
            if not isinstance(data.get("entries"), dict):
                raise ValueError()
            self.cached_data = copy.deepcopy(data)
            self.cached_stamp = stamp
            return data
        except (InvalidToken, ValueError, TypeError):
            raise VaultError("The vault could not be opened; its file was "
                             "left unchanged.") from None

    def _write(self, data):
        cipher = self._cipher(create=True)
        raw = json.dumps(data, ensure_ascii=False).encode()
        write_whole(self.path, cipher.encrypt(raw), mode=0o600)
        self.cached_stamp = None

    def labels(self):
        with self.lock:
            return [copy.deepcopy(entry["labels"])
                    for entry in self._read()["entries"].values()]

    def saved(self, receipt):
        with self.lock:
            return receipt in self._read()["receipts"]

    def check(self, action, labels):
        check_labels(labels)
        entries = self._read()["entries"]
        name = labels["name"]
        if action == "add" and name in entries:
            raise VaultError("That entry name already exists; update it instead.")
        if action == "update" and name not in entries:
            raise VaultError("That entry does not exist.")
        sites = {site(value) for value in labels["sites"]}
        for existing, entry in entries.items():
            other = entry["labels"]
            if (existing != name and labels["username"] == other["username"]
                    and sites.intersection(map(site, other["sites"]))):
                raise VaultError(f"Entry {existing} already has that site "
                                 "and username.")

    def save(self, action, labels, values, receipt):
        # Provisioning can wait for the desktop. While a new vault has no
        # entries, other sessions can still redact against its empty snapshot.
        with changing(self.path):
            if not self.path.exists():
                check_labels(labels)
                self._cipher(create=True)
            return self._save(action, labels, values, receipt)

    def _save(self, action, labels, values, receipt):
        with self.lock:
            data = self._read()
            if receipt in data["receipts"]:
                return data["receipts"][receipt]
            self.check(action, labels)
            if (not isinstance(values, dict) or set(values) != set(labels["parts"])
                    or any(not isinstance(value, str) or not value
                           or len(value) > 65536 for value in values.values())):
                raise VaultError("Supply a nonempty value for each named part "
                                 "(at most 65,536 characters each).")
            name = labels["name"]
            previous = data["entries"].get(name)
            stamp = now()
            kept = {**labels, "added_at": previous["labels"]["added_at"]
                    if previous else stamp, "changed_at": stamp,
                    "last_used_at": None}
            data["entries"][name] = {"labels": kept, "values": dict(values)}
            data["receipts"][receipt] = name
            self._write(data)
            if previous:
                self._retire(name, previous["values"])
            return name

    def _retire(self, name, values):
        retired = dict(self.retired)
        for part, value in values.items():
            retired[value] = f"⟦vault:{name}/{part}⟧"
        self.retired = retired

    def delete(self, name):
        with changing(self.path), self.lock:
            data = self._read()
            entry = data["entries"].pop(name, None)
            if entry is None:
                raise VaultError("That entry does not exist.")
            self._write(data)
            self._retire(name, entry["values"])

    def values(self):
        # Output readers use an immutable snapshot while the file is unchanged.
        # A shared lock per stream event would serialize unrelated sessions.
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return dict(self.retired)
        stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        snapshot = self.cached_values
        if snapshot is not None and snapshot[0] == stamp:
            return self.retired | snapshot[1]
        with self.lock:
            found = {}
            for name, entry in self._read()["entries"].items():
                for part, value in entry["values"].items():
                    found[value] = f"⟦vault:{name}/{part}⟧"
            self.cached_values = (self.cached_stamp, found)
            return self.retired | found

    def resolve(self, references):
        with changing(self.path), self.lock:
            data = self._read()
            found = {}
            for reference in references:
                name, separator, part = reference.partition("/")
                entry = data["entries"].get(name)
                if entry is None:
                    raise VaultError("A vault stand-in names a missing entry.")
                values = entry["values"]
                if not separator:
                    if len(values) != 1:
                        raise VaultError("This entry has several parts; "
                                         "the stand-in needs name/part.")
                    part = next(iter(values))
                if part not in values:
                    raise VaultError("A vault stand-in names a missing part.")
                found[reference] = values[part]
                entry["labels"]["last_used_at"] = now()
            if references:
                self._write(data)
            return found

    def status(self):
        if not self.path.exists():
            return "Vault: empty; its key store is selected on the first save."
        with self.lock:
            count = len(self._read()["entries"])
            return f"Vault: {count} entries; encryption key in {self.key_storage}."

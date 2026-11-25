"""The conversation envelope and the component declarations that describe it."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

ORIGINS = {
    "user": {"user", "model", "tool"},
    "system": {"system", "model", "tool"},
    "model": {"model", "tool"},
    "tool": {"tool"},
}
TYPE_NAME = re.compile(r"[^/\s]+/[^/\s]+")
ENVELOPE = ("type", "id", "origin", "created_at", "links", "fields", "entries")


class EntryError(ValueError):
    """An entry violates the conversation contract."""


class UnsupportedCondition(EntryError):
    """A reply carries an execution condition or pending obligation ZERO won't run."""


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


@dataclass(frozen=True, slots=True)
class Link:
    relation: str
    target_id: str

    def __post_init__(self):
        if not nonempty(self.relation) or not nonempty(self.target_id):
            raise EntryError("a link needs a relation and a target id")

    def to_dict(self):
        return {"relation": self.relation, "target_id": self.target_id}


def freeze(value):
    """Own an immutable copy of a JSON value."""
    if isinstance(value, Mapping):
        if not all(type(key) is str for key in value):
            raise EntryError("field keys must be strings")
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(map(freeze, value))
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise EntryError("fields must contain JSON values")


def thaw(value):
    """Return a JSON value with independently writable containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return list(map(thaw, value))
    return value


@dataclass(frozen=True, slots=True)
class Entry:
    type: str
    id: str | None = None
    origin: str | None = None
    created_at: str | None = None
    links: tuple[Link, ...] | None = None
    fields: Mapping | None = None
    entries: tuple["Entry", ...] | None = None
    extra: Mapping | None = None
    # Preserve explicit JSON nulls as well as omitted keys on read.
    _nulls: frozenset = field(default_factory=frozenset, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.type, str) or not TYPE_NAME.fullmatch(self.type):
            raise EntryError("entry type must have an owner and a name")
        if self.id is not None and not nonempty(self.id):
            raise EntryError("entry id must be a nonempty string")
        if self.origin is not None and (
            not isinstance(self.origin, str) or self.origin not in ORIGINS
        ):
            raise EntryError(f"unknown entry origin: {self.origin!r}")
        if self.created_at is not None:
            try:
                datetime.fromisoformat(self.created_at)
                if len(self.created_at) <= 10:
                    raise ValueError
            except (TypeError, ValueError):
                raise EntryError(
                    "entry created_at must be an ISO-8601 timestamp"
                ) from None
        for name, item_type in (("links", Link), ("entries", Entry)):
            values = getattr(self, name)
            if values is not None:
                if not isinstance(values, (list, tuple)) or not all(
                    isinstance(value, item_type) for value in values
                ):
                    raise EntryError(f"invalid {name} in entry")
                object.__setattr__(self, name, tuple(values))
        if self.fields is not None:
            if not isinstance(self.fields, Mapping):
                raise EntryError("entry fields must be an object")
            object.__setattr__(self, "fields", freeze(self.fields))
        if self.extra is not None:
            if not isinstance(self.extra, Mapping) or self.extra.keys() & ENVELOPE:
                raise EntryError("entry extensions cannot replace envelope fields")
            object.__setattr__(self, "extra", freeze(self.extra))
        object.__setattr__(self, "_nulls", frozenset(self._nulls))
        for _ in walk(self):
            pass

    def require_root(self):
        if self.id is None or self.origin is None or self.created_at is None:
            raise EntryError("a timeline entry needs id, origin and created_at")
        return self

    @classmethod
    def from_dict(cls, data, *, root=True):
        if not isinstance(data, dict) or "type" not in data:
            raise EntryError("invalid entry envelope")
        values = {key: data[key] for key in ENVELOPE if key in data}
        extra = {key: data[key] for key in data.keys() - ENVELOPE}
        if (links := values.get("links")) is not None:
            if not isinstance(links, list) or not all(
                isinstance(link, dict) and link.keys() == {"relation", "target_id"}
                for link in links
            ):
                raise EntryError("invalid links in entry")
            values["links"] = tuple(Link(**link) for link in links)
        if (children := values.get("entries")) is not None:
            if not isinstance(children, list):
                raise EntryError("entry entries must be an array")
            values["entries"] = tuple(cls.from_dict(child, root=False)
                                      for child in children)
        entry = cls(**values, extra=extra or None,
                    _nulls=frozenset(k for k in values if values[k] is None))
        return entry.require_root() if root else entry

    def to_dict(self):
        values = {}
        for name in ENVELOPE:
            value = getattr(self, name)
            if value is None:
                if name in self._nulls:
                    values[name] = None
                continue
            if name in ("links", "entries"):
                value = [item.to_dict() for item in value]
            elif name == "fields":
                value = thaw(value)
            values[name] = value
        return {**values, **thaw(self.extra)} if self.extra else values

    def get(self, name, default=None):
        """A named field, if present."""
        return default if self.fields is None else self.fields.get(name, default)


def walk(entry, origin=None, created_at=None, depth=0, *, include_private=True,
         stop_at=None):
    """Yield nodes with inherited authority and time, stopping at `stop_at`."""
    if not include_private and has(entry, "private_continuation"):
        return
    effective_origin = entry.origin or origin
    if origin is not None and effective_origin not in ORIGINS[origin]:
        raise EntryError(f"{origin} entry cannot contain {effective_origin} entries")
    created_at = entry.created_at or created_at
    yield entry, effective_origin, created_at, depth
    if stop_at is not None and has(entry, stop_at):
        return
    for child in entry.entries or ():
        yield from walk(child, effective_origin, created_at, depth + 1,
                        include_private=include_private, stop_at=stop_at)


def _transform(value, fn):
    if isinstance(value, str):
        text = fn(value)
        if type(text) is not str:
            raise EntryError("a text transform must return a string")
        return value if value == text else text
    if isinstance(value, Mapping):
        changed = {key: _transform(item, fn) for key, item in value.items()}
        if any(changed[key] is not item for key, item in value.items()):
            return MappingProxyType(changed)
    elif isinstance(value, tuple):
        changed = tuple(_transform(item, fn) for item in value)
        if any(after is not before for before, after in zip(value, changed)):
            return changed
    return value


def transform_text(entry, fn):
    """Transform the strings in entry data, sharing everything unchanged.
    Keys, numbers and an entry whose spec says `redact=False` (its ids,
    digests and byte counts are structure, never prose) stay as they are."""
    spec = SPECS.get(entry.type)
    if spec is not None and not spec.redact:
        return entry
    fields = _transform(entry.fields, fn)
    extra = _transform(entry.extra, fn)
    children = tuple(transform_text(child, fn) for child in entry.entries or ())
    if not any(a is not b for a, b in zip(children, entry.entries or ())):
        children = entry.entries
    if fields is entry.fields and extra is entry.extra and children is entry.entries:
        return entry
    # Only JSON data changes here. Entry authority is assigned by the envelope.
    result = object.__new__(Entry)
    for name in (*ENVELOPE, "_nulls"):
        object.__setattr__(result, name, getattr(entry, name))
    object.__setattr__(result, "fields", fields)
    object.__setattr__(result, "extra", extra)
    object.__setattr__(result, "entries", children)
    return result


def text(entry):
    return entry.get("text", "")


def plain(entry):
    """Fallback for a type the consumer has never seen."""
    if isinstance(text(entry), str) and text(entry):
        return text(entry)
    return json.dumps(thaw(entry.fields), ensure_ascii=False) if entry.fields else ""


@dataclass(frozen=True, slots=True)
class EntrySpec:
    type: str
    traits: frozenset = field(default_factory=frozenset)
    project: object = plain
    fields_schema: Mapping | None = None
    role: str | None = None
    required_fields: tuple[str, ...] = ()
    public_fields: tuple[str, ...] = ()
    replay_notice: object = None
    redact: bool = True


SPECS = {}
_ROLES = {}


def add(*specs):
    """Register declarations at assembly time."""
    for spec in specs:
        if spec.type in SPECS or (spec.role is not None and spec.role in _ROLES):
            raise EntryError(f"entry already declared: {spec.type}")
        SPECS[spec.type] = spec
        if spec.role is not None:
            _ROLES[spec.role] = spec


def make(role, **envelope):
    return Entry(_ROLES[role].type, **envelope)


def root(role, origin, **fields):
    """An entry for a model request outside a saved timeline."""
    return make(role, id=uuid4().hex, origin=origin,
                created_at=datetime.now(timezone.utc).isoformat(), fields=fields)


def has(entry, trait):
    spec = SPECS.get(entry.type)
    return trait in spec.traits if spec else trait in ("model_visible", "searchable")


def project(entry):
    spec = SPECS.get(entry.type)
    return spec.project(entry) if spec else plain(entry)


def replay_notice(entry):
    """Replacement entry for a timeline record at its original position, or None."""
    spec = SPECS.get(entry.type)
    if spec is None or spec.replay_notice is None:
        return None
    return spec.replay_notice(entry)


def matching(entry, trait, *, origin=None, include_private=True):
    """All nodes carrying a trait, in their envelope order."""
    return (
        node for node, effective, _, _ in walk(entry, include_private=include_private)
        if has(node, trait) and (origin is None or effective == origin)
    )


def tool_calls(entry):
    return matching(entry, "tool_call", origin="model", include_private=False)


def combined_text(entry, trait="text", *, origin=None):
    return "\n\n".join(filter(None, (
        project(node)
        for node, effective_origin, _, _ in walk(entry, include_private=False)
        if has(node, trait) and (origin is None or effective_origin == origin)
    )))


def public(entry):
    """The app projection carries only fields its entry owner declares public."""
    if has(entry, "private_continuation"):
        return None
    found = {"type": entry.type}
    for name in ("id", "origin", "created_at"):
        if (value := getattr(entry, name)) is not None:
            found[name] = value
    spec = SPECS.get(entry.type)
    if spec is not None and entry.fields:
        fields = {name: thaw(entry.fields[name]) for name in spec.public_fields
                  if name in entry.fields}
        if fields:
            found["fields"] = fields
    if entry.links:
        found["links"] = [link.to_dict() for link in entry.links]
    children = [item for child in entry.entries or ()
                if (item := public(child)) is not None]
    if children:
        found["entries"] = children
    return found


def ensure_executable(entry, *, origin=None):
    """Validate the complete model reply before allowing any call to run."""
    if (entry.origin or origin) != "model":
        raise EntryError("an executable reply must come from the model")
    seen = set()
    for node, effective, _, _ in walk(entry, origin=origin):
        spec = SPECS.get(node.type)
        if spec is None or spec.fields_schema is None:
            details = []
            if isinstance(item_type := node.get("type"), str) and item_type:
                details.append(item_type)
            if isinstance(execution := node.get("execution"), str) and execution:
                details.append(f"execution={execution}")
            if (isinstance(status := node.get("status"), str) and status
                    and status != "completed"):
                details.append(f"status={status}")
            suffix = f" ({', '.join(details)})" if details else ""
            raise UnsupportedCondition(
                f"unsupported executable entry type: {node.type}{suffix}"
            )
        fields = node.fields or {}
        schema = spec.fields_schema
        extra_fields = sorted(fields.keys() - schema.keys())
        if node.extra or node.links is not None or extra_fields:
            reasons = []
            if node.extra:
                reasons.append(f"extra ({', '.join(sorted(node.extra))})")
            if node.links is not None:
                reasons.append("links")
            for name in extra_fields:
                value = fields.get(name)
                if name == "wire_fields" and isinstance(value, Mapping) and value:
                    reasons.append(f"wire_fields ({', '.join(sorted(value))})")
                else:
                    reasons.append(name)
            detail = f": {', '.join(reasons)}" if reasons else ""
            raise UnsupportedCondition(
                f"unsupported execution condition on {node.type}{detail}"
            )
        if any(name not in fields for name in spec.required_fields) or not all(
            isinstance(value, schema[name]) for name, value in fields.items()
        ):
            raise EntryError(f"invalid executable fields on {node.type}")
        if node.entries is not None and not (
            has(node, "assistant_reply") or has(node, "hosted_output")
        ):
            raise EntryError(f"executable entry cannot have children: {node.type}")
        if has(node, "hosted_output"):
            if (fields.get("execution") != "server"
                    or fields.get("status") != "completed"):
                raise UnsupportedCondition(
                    f"unsupported hosted output state on {node.type}: "
                    f"execution={fields.get('execution')!r}, "
                    f"status={fields.get('status')!r}"
                )
            for child in node.entries or ():
                if has(child, "executable") or has(child, "tool_call"):
                    raise UnsupportedCondition(
                        f"hosted output cannot contain executable entry: {child.type}"
                    )
        if has(node, "executable"):
            identifier = call_id(node)
            if (effective != "model" or not nonempty(identifier) or identifier in seen
                    or ("call_id" in fields and not nonempty(fields["call_id"]))):
                raise EntryError("tool calls need distinct nonempty model call IDs")
            seen.add(identifier)


def call_id(entry):
    """The call identifier sent to its model API."""
    return entry.get("call_id") or entry.id


def linked_to(entry, relation):
    return next((link.target_id for link in entry.links or ()
                 if link.relation == relation), None)

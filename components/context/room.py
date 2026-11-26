"""How big the context is, and how much room it has.

A call's size is known after it: the server counts the prompt's tokens.
What came since (the reply, the results, the user's next message) is
counted by the server where it can (/tokenize). Else, where the
server refuses a request longer than its context, it is estimated at the
bytes a token the session's own counted steps show (bytes_a_token): a guess
low there is caught by the refusal, one compaction and the call again.
Where a server may cut such a request silently, or before a step has
counted, a token for every byte of UTF-8, which never counts low: a
byte-level tokenizer gives no byte two tokens, and digits, hex and rare
characters come to a token a byte (measured on 23 kinds of text). It counts
English three or four times high, until the next call counts it.

The line is `compact_at_tokens`, or the model's window when it is unset, and
never past the window. The headroom is what must fit beside the context: the
next reply (`reply_room_tokens`), and a compaction's instructions and handoff
(`handoff_room_tokens`). A result ZERO didn't size gets a share
(`result_share_percent`) of the room left below the line minus the
headroom, or, near the line, of the room a compaction leaves, whichever is
more, up to `max_result_tokens` when it is positive; past it, the whole
goes to a file (hooks.py). Each is read from a session's limits as they
stand (settings.LiveSettings).
"""

import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass

from core import entries, replay

BYTES_PER_TOKEN = 1
# The steps a session's pace, and its bytes a token, are measured over: its
# newest
PACE_STEPS = 20
# A step with less between its two calls tells too little of the bytes a
# token: a message's own wrapping counts for more there
LEAST_STEP_BYTES = 1_000
_INLINE_IMAGES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
SVG_HEADER_BYTES = 65_536


# Keep the prolog check aligned with attachment metadata at ingest.
def _doctype_end(text, start):
    brackets = 0
    while start < len(text):
        char = text[start]
        if char in "\"'":
            marker, start = char, start + 1
        elif text.startswith("<!--", start):
            marker, start = "-->", start + 4
        elif text.startswith("<?", start):
            marker, start = "?>", start + 2
        else:
            if char == "[":
                brackets += 1
            elif char == "]" and brackets:
                brackets -= 1
            elif char == ">" and brackets == 0:
                return start + 1
            start += 1
            continue
        end = text.find(marker, start)
        if end == -1:
            break
        start = end + len(marker)
    return len(text)


def _has_svg_header(head):
    at_limit = len(head) >= SVG_HEADER_BYTES
    head = head[:SVG_HEADER_BYTES]
    encoding = "utf-16" if head.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = head.decode(encoding, "replace")
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("<?", index):
            marker, start = "?>", index + 2
        elif text.startswith("<!--", index):
            marker, start = "-->", index + 4
        elif text[index:index + 9].upper() == "<!DOCTYPE":
            index = _doctype_end(text, index + 9)
            continue
        else:
            if text[index] != "<":
                return False
            end = index + 1
            while end < len(text) and not (
                text[end].isspace() or text[end] in "/>"
            ):
                end += 1
            if end == len(text):
                break
            prefix, colon, name = text[index + 1:end].rpartition(":")
            return name.lower() == "svg" and (
                not colon or (bool(prefix) and ":" not in prefix)
            )
        end = text.find(marker, start)
        if end == -1:
            break
        index = end + len(marker)
    # An unresolved prefix may hide an SVG root beyond the scan limit.
    return at_limit


def _is_media_mime(media_type):
    if not isinstance(media_type, str):
        return False
    norm = media_type.split(";", 1)[0].strip().lower()
    return _modality_for_mime(norm) is not None


def _is_svg_attachment(node):
    name = node.get("name")
    if isinstance(name, str) and name.lower().endswith(".svg"):
        return True
    path = node.get("path")
    if not isinstance(path, str) or not path:
        return False
    # Old entries can carry a raster hint for XML. Inspect only a regular file.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            head = stream.read(SVG_HEADER_BYTES)
    except (OSError, ValueError):
        return False
    return _has_svg_header(head)


def has_attachments(conversation):
    """Fast check for whether any entry in `conversation` carries attachments."""
    for entry in conversation:
        if not entry.entries:
            continue
        for node, _, _, depth in entries.walk(entry, include_private=False):
            if depth and entries.has(node, "attachment"):
                return True
    return False


def entry_has_inline_media(entry):
    """True when `entry` carries a non-reference media attachment child."""
    if entry is None or not entry.entries:
        return False
    for node, _, _, depth in entries.walk(entry, include_private=False):
        if (depth and entries.has(node, "attachment")
                and node.get("delivery") != "reference"
                and _is_media_mime(node.get("media_type"))
                and not _is_svg_attachment(node)):
            return True
    return False


def _non_text_modality_tokens(entry):
    """Positive non-text token total from an assistant reply's modality usage."""
    by_mod = entry.get("input_tokens_by_modality")
    if not isinstance(by_mod, Mapping):
        return 0
    return sum(
        val for key, val in by_mod.items()
        if key != "text" and type(val) is int and val > 0
    )


def _prior_media_tokens(entry):
    """Native media tokens already included in `entry`'s prompt_tokens."""
    by_mod = _non_text_modality_tokens(entry)
    if by_mod > 0:
        return by_mod
    recorded = entry.get("native_media_tokens")
    if type(recorded) is int and recorded > 0:
        return recorded
    return 0


def _modality_for_mime(mime):
    if mime in _INLINE_IMAGES:
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf":
        return "pdf"
    return None


def _attachment_nodes(entry):
    if entry is None or not entry.entries:
        return ()
    return tuple(
        node for node, _, _, depth in entries.walk(entry, include_private=False)
        if depth and entries.has(node, "attachment")
    )


def attachment_tokens(node, count=None, *, capabilities=None, media_cap=None):
    """Estimate one attachment's tokens from validated modality metadata only.

    Reference attachments, unsupported modalities, attachments exceeding
    `media_cap`, and unvalidated/non-media attachments count only their
    compact text reference, never raw file bytes.
    """
    counter = count or tokens
    ref = lambda: counter(entries.project(node) or "")
    if node.get("delivery") == "reference":
        return ref()
    raw_mime = node.get("media_type")
    mime = (
        raw_mime.split(";", 1)[0].strip().lower()
        if isinstance(raw_mime, str) else ""
    )
    modality = _modality_for_mime(mime)
    if modality is None or _is_svg_attachment(node):
        return ref()
    if capabilities is not None:
        inp = getattr(capabilities, "input", None)
        if inp is None or inp.get(modality) is not True:
            return ref()
    est = None
    if modality == "image":
        width, height = node.get("width"), node.get("height")
        if (type(width) is int and width > 0
                and type(height) is int and height > 0):
            w, h = float(width), float(height)
            if max(w, h) > 2048:
                scale = 2048 / max(w, h)
                w *= scale
                h *= scale
            if min(w, h) > 768:
                scale = 768 / min(w, h)
                w *= scale
                h *= scale
            tiles = math.ceil(w / 512) * math.ceil(h / 512)
            est = 85 + 170 * max(1, tiles)
    elif modality == "audio":
        duration = node.get("duration_seconds")
        if (isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and math.isfinite(duration) and duration > 0):
            est = max(50, math.ceil(duration * 50))
    elif modality == "video":
        duration = node.get("duration_seconds")
        if (isinstance(duration, (int, float)) and not isinstance(duration, bool)
                and math.isfinite(duration) and duration > 0):
            est = max(300, math.ceil(duration * 300))
    elif modality == "pdf":
        pages = node.get("pages")
        if type(pages) is int and pages > 0:
            est = pages * 2000
    if est is None:
        return ref()
    if media_cap is not None and est > media_cap:
        return ref()
    return est


def changes_model(entry):
    return ((entries.has(entry, "system_prompt") and entry.get("why") == "model")
            or (entries.has(entry, "note")
                and entry.get("kind") in ("fallback", "main_again")))


def tokens(text, bytes_a_token=BYTES_PER_TOKEN):
    """`text`'s tokens with no count from the server: its bytes over
    `bytes_a_token`, rounded up; at one a byte, never fewer than the
    server's."""
    return int(-(-len(text.encode("utf-8")) // bytes_a_token))


def entry_text(entry):
    """A model entry's visible contents and call arguments for sizing."""
    parts = []
    for node, origin, _, _ in replay.model_nodes(entry, stop_at="tool_result"):
        if entries.has(node, "tool_call"):
            if origin == "model":
                parts.append({
                    "call": entries.call_id(node), "name": node.get("name"),
                    "arguments": node.get("arguments"),
                })
        elif entries.has(node, "tool_result"):
            if origin == "tool":
                parts.append({
                    "answer": entries.linked_to(node, "answers"),
                    "text": replay.tool_output(node, include_attachments=False),
                })
        elif (entries.has(node, "model_visible")
              and not entries.has(node, "assistant_reply")
              and not entries.has(node, "attachment")):
            parts.append({"origin": origin, "text": entries.project(node)})
    return json.dumps(parts, ensure_ascii=False) if parts else ""


def entry_tokens(entry, count=tokens, *, include_media=True, capabilities=None,
                 media_cap=None):
    """Measure visible text, validated media bounds, and opaque continuation."""
    public = entry_text(entry)
    private = sum(tokens(json.dumps(entries.thaw(node.fields), ensure_ascii=False))
                  for node in entries.matching(entry, "private_continuation"))
    attachments = _attachment_nodes(entry)
    media = 0
    for child in attachments:
        if include_media:
            media += attachment_tokens(
                child, count, capabilities=capabilities, media_cap=media_cap,
            )
        else:
            media += count(entries.project(child) or "")
    return (count(public) if public else 0) + private + media


@dataclass(frozen=True)
class Size:
    """The context's tokens, its system message and tools included, and how
    many of them are estimated rather than counted by the server."""

    tokens: int
    estimated: int


def _schema_tokens(schemas, tokens):
    return tokens(json.dumps(schemas() if callable(schemas) else schemas,
                             ensure_ascii=False))


def _owner_ids(entries_slice):
    entry_ids = {id(entry) for entry in entries_slice}
    tool_ids = set()
    for entry in entries_slice:
        for node, origin, _, _ in entries.walk(
            entry, include_private=False, stop_at="tool_result",
        ):
            if origin == "tool" and entries.has(node, "tool_result"):
                tool_ids.add(id(node))
    return entry_ids, tool_ids


def _prepared_prefix_ref_tokens(prefix, prepared, count):
    """Reference tokens for prefix attachments not delivered natively."""
    entry_ids, tool_ids = _owner_ids(prefix)
    total = 0
    for decision in prepared.plan.estimates:
        if decision.is_native:
            continue
        kind, owner_id, _ = decision.key
        if ((kind == "entry" and owner_id in entry_ids)
                or (kind == "tool" and owner_id in tool_ids)):
            total += count(decision.reference_text())
    return total


def measure(records, conversation, schemas, tokens=tokens, *, prepared=None,
            include_media=True, capabilities=None, media_cap=None):
    """The context's size: the last call's count plus what came after it
    (`tokens`: text's tokens, the server's count where there is one), when
    that call saw the conversation as it stands; else all of it so (no call
    yet, or a model switch or a compaction since). When `prepared` is supplied,
    accounts directly on the prepared request plan (using provider native count
    when available, else text baseline plus planned native media tokens)."""
    if prepared is not None and prepared.native_token_estimate is not None:
        exact = prepared.native_token_estimate.tokens
        return Size(exact, exact)
    last, rewritten = None, False
    for record in reversed(records):
        entry = record.entry
        if entries.has(entry, "system_prompt") and entry.get("why") == "code":
            rewritten = True
        elif replay.starts_context(entry) or changes_model(entry):
            break
        elif entries.has(entry, "assistant_reply") and entry.get("prompt_tokens"):
            last = entry
            break
    if last is not None:
        for index in range(len(conversation) - 1, -1, -1):
            if conversation[index].id == last.id:
                tail = conversation[index:]
                last_media = _prior_media_tokens(last)
                base_text = max(last.get("prompt_tokens") - last_media, 0)
                if prepared is not None:
                    guessed = sum(
                        entry_tokens(entry, tokens, include_media=False)
                        for entry in tail
                    )
                    if last_media > 0:
                        guessed += _prepared_prefix_ref_tokens(
                            conversation[:index], prepared, tokens,
                        )
                    guessed += prepared.media_tokens
                    if rewritten:
                        guessed += entry_tokens(
                            conversation[0], tokens, include_media=False,
                        )
                        guessed += _schema_tokens(schemas, tokens)
                    return Size(base_text + guessed, guessed)
                if not include_media:
                    guessed = sum(
                        entry_tokens(entry, tokens, include_media=False)
                        for entry in tail
                    )
                    if rewritten:
                        guessed += entry_tokens(
                            conversation[0], tokens, include_media=False,
                        )
                        guessed += _schema_tokens(schemas, tokens)
                    return Size(base_text + guessed, guessed)
                guessed = sum(
                    entry_tokens(
                        entry, tokens, include_media=True,
                        capabilities=capabilities, media_cap=media_cap,
                    )
                    for entry in tail
                )
                if rewritten:
                    guessed += entry_tokens(
                        conversation[0], tokens, include_media=False,
                    )
                    guessed += _schema_tokens(schemas, tokens)
                return Size(last.get("prompt_tokens") + guessed, guessed)
    use_local_media = include_media and prepared is None
    guessed = sum(
        entry_tokens(
            entry, tokens, include_media=use_local_media,
            capabilities=capabilities, media_cap=media_cap,
        )
        for entry in conversation
    )
    if prepared is not None:
        guessed += prepared.media_tokens
    guessed += _schema_tokens(schemas, tokens)
    return Size(guessed, guessed)


def fixed_tokens(records, conversation, schemas, tokens=tokens):
    """The tokens every context holds, its system message and tools: their
    count (`tokens`, a token a byte where the server counts nothing), but
    never more than the smallest prompt a call on this model has sent, which
    held them and more. A token a byte counted a start of 6.4k tokens as
    25k, and a fresh context's room as below zero (7.3's eval)."""
    found = tokens(entries.project(conversation[0])) + tokens(
        json.dumps(schemas, ensure_ascii=False))
    for record in reversed(records):
        entry = record.entry
        if changes_model(entry):
            break
        if entries.has(entry, "assistant_reply") and entry.get("prompt_tokens"):
            found = min(found, entry.get("prompt_tokens"))
    return found


def tokens_a_step(records):
    """The tokens a step adds to the context at the session's own pace: the
    mean growth of its server's count from one call to the next, over its
    newest PACE_STEPS steps, each pair of calls on one context (no
    compaction, model switch or new system message between); None before
    two calls have counted."""
    grown, last = [], None
    for record in reversed(records):
        entry = record.entry
        if replay.starts_context(entry) or changes_model(entry):
            last = None
        elif entries.has(entry, "assistant_reply") and (prompt := entry.get(
            "prompt_tokens"
        )):
            if last is not None and last >= prompt:
                grown.append(last - prompt)
                if len(grown) == PACE_STEPS:
                    break
            last = prompt
    return sum(grown) / len(grown) if grown else None


def entry_bytes(entry):
    """A message's bytes, as it is sent."""
    return len(entry_text(entry).encode("utf-8"))


def _media_signature(entry):
    count = entry.get("native_media_count") or 0
    if not count and entry.get("has_native_media"):
        count = 1
    return (count, _non_text_modality_tokens(entry))


def bytes_a_token(records):
    """The bytes a token of this session's text, as its model server counts
    it: what came between two counted calls in bytes, over the growth of the
    server's count, the least of it over the newest PACE_STEPS steps of
    LEAST_STEP_BYTES or more (the densest text a step held, so the estimate
    is high rather than low), never under one; each pair of calls on one
    context, since the latest model switch. Excludes step pairs contaminated
    by native media so media tokens never corrupt text calibration. None before
    a clean step has counted."""
    found, later, between = [], None, 0
    contaminated, later_media = False, None
    for record in reversed(records):
        entry = record.entry
        if entries.has(entry, "header") or changes_model(entry):
            break
        if replay.starts_context(entry):
            later, between = None, 0
            contaminated, later_media = False, None
            continue
        projected = replay.model_entry(record)
        if projected is not None:
            if entry_has_inline_media(projected):
                contaminated = True
            between += entry_bytes(projected)
        prompt = entry.get("prompt_tokens")
        if not entries.has(entry, "assistant_reply") or not prompt:
            continue
        cur_media = _media_signature(entry)
        pair_dirty = contaminated or (
            later_media is not None and later_media != cur_media
        )
        grown = (later or 0) - prompt
        if (not pair_dirty and later is not None
                and grown > 0 and between >= LEAST_STEP_BYTES):
            found.append(between / grown)
            if len(found) == PACE_STEPS:
                break
        later, between = prompt, 0
        contaminated, later_media = False, cur_media
    return max(min(found), 1) if found else None


def line(limits, window):
    """The tokens a session's context is kept under: its compact_at_tokens,
    or the model's window when unset, and never past the window."""
    chosen = limits.get("compact_at_tokens") or window
    return min(chosen, window)


def reply_room(at, limits, max_output_tokens=None):
    """The next reply's room under the line `at`: reply_room_tokens, at most
    an eighth of the line, so a small window keeps room for the talk."""
    return min(limits["reply_room_tokens"], at // 8,
               max_output_tokens if max_output_tokens is not None else at)


def handoff_room(at, limits, max_output_tokens=None):
    """A compaction handoff's room under the line `at`, its ceiling too:
    handoff_room_tokens, at most an eighth of the line."""
    return min(limits["handoff_room_tokens"], at // 8,
               max_output_tokens if max_output_tokens is not None else at)


def headroom(at, instructions, limits, max_output_tokens=None):
    """What must fit beside the context under the line `at`: the next reply,
    a compaction's `instructions` and its handoff. No margin: what the
    server didn't count is counted high."""
    return (reply_room(at, limits, max_output_tokens)
            + handoff_room(at, limits, max_output_tokens) + instructions)


def input_room(at, instructions, limits, *, input_limit=None, output_limit=None):
    chosen = at - headroom(at, instructions, limits, output_limit)
    return min(chosen, input_limit - instructions) if input_limit else chosen


def room_left(at, size, instructions, limits, **model_limits):
    """The tokens left before the context must be compacted; below zero, it
    must be now."""
    return input_room(at, instructions, limits, **model_limits) - size.tokens


def percent_share(room, limits):
    """The percentage of remaining room available to a source."""
    return max(room, 0) * limits["result_share_percent"] // 100


RESULT_SHARE_SETTINGS = ("result_share_percent", "max_result_tokens")
HEADROOM_SETTINGS = ("reply_room_tokens", "handoff_room_tokens")


def result_limits(limits, keys=RESULT_SHARE_SETTINGS):
    """Read related limits together when settings are live."""
    snapshot = getattr(limits, "snapshot", None)
    return snapshot(keys) if snapshot is not None else limits


def share(room, limits):
    """The share of remaining room for one unsized result, with its fixed cap."""
    limits = result_limits(limits)
    percent = percent_share(room, limits)
    maximum = limits["max_result_tokens"]
    return min(percent, maximum) if maximum > 0 else percent


def fresh_room(at, fixed, instructions, limits, **model_limits):
    """The most one result can be: what a fresh context has, after its
    system message and tools (`fixed` tokens) and a handoff, below the line
    and the headroom."""
    return (input_room(at, instructions, limits, **model_limits) - fixed
            - handoff_room(at, limits, model_limits.get("output_limit")))


def model_limits(model):
    """Caps to reserve space under, taken from the model used for this call."""
    server = getattr(model, "server", None)
    return {"input_limit": getattr(server, "max_input_tokens", None),
            "output_limit": getattr(server, "max_output_tokens", None)}


class ModelLimitExceeded(Exception):
    """An inference request could not fit the model's verified limits."""


def has_input_bound(model):
    server = getattr(model, "server", None)
    return (getattr(server, "request_context_window", None) is not None
            or getattr(server, "max_input_tokens", None) is not None)


def bound_output(model, input_tokens, output_tokens=None, *, operation):
    """Output room left for this request under the endpoint's verified limits."""
    server = getattr(model, "server", None)
    total = getattr(server, "request_context_window", None)
    maximum_input = getattr(server, "max_input_tokens", None)
    maximum_output = getattr(server, "max_output_tokens", None)
    minimum = getattr(getattr(model, "api", None), "MIN_OUTPUT_TOKENS", 1)
    caps = (output_tokens, maximum_output, total - input_tokens if total else None)
    cap = min((cap for cap in caps if cap is not None), default=None)
    blocking = None
    if maximum_input and input_tokens > maximum_input:
        blocking = f"{maximum_input:,}-token input limit"
    elif total and input_tokens + minimum > total:
        blocking = f"{total:,}-token total context window"
    elif cap is not None and cap < minimum:
        blocking = f"{cap:,}-token output limit"
    if blocking:
        raise ModelLimitExceeded(
            f"{operation} exceeds {model.name}'s {blocking}. "
            "Switch to a model with more room and compact the session there."
        )
    return cap

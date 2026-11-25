"""Shared capability-first multimodal delivery planning and token accounting.

Evaluates each attachment descriptor fresh per request against the active
model's effective ModelCapabilities, wire role, readable blob on disk, trusted
token bound, and remaining context room. Never caches base64 blobs or
per-model decisions across requests, and never uses file bytes/4 as a media
token estimate.
"""

import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re

import paths as home_paths

IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

OPENAI_AUDIO_FORMATS = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
}

GOOGLE_AUDIO_MIME_TYPES = frozenset({
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/aac",
    "audio/flac",
    "audio/aiff",
    "audio/mp4",
    "audio/webm",
})

VIDEO_MIME_TYPES = frozenset({
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/mpeg",
    "video/x-flv",
    "video/mpg",
    "video/wmv",
    "video/3gpp",
})

PDF_MIME_TYPES = frozenset({"application/pdf"})

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Per-API wire-role support: {(api_name, wire_role): frozenset(modalities)}.
WIRE_ROLE_MODALITIES = {
    ("responses", "user"): frozenset({"image", "audio", "pdf"}),
    ("responses", "tool"): frozenset({"image", "pdf"}),
    ("chat_completions", "user"): frozenset({"image", "audio", "pdf"}),
    ("chat_completions", "tool"): frozenset({"image", "audio", "pdf"}),
    ("google_interactions", "user"): frozenset({"image", "audio", "video", "pdf"}),
    ("google_interactions", "tool"): frozenset({"image", "audio", "video", "pdf"}),
}

# Conservative local upper-bound rates for validated media metadata.
OPENAI_IMAGE_BASE_TOKENS = 85
OPENAI_IMAGE_TILE_TOKENS = 170
OPENAI_IMAGE_MAX_SIDE = 2048
OPENAI_IMAGE_SHORT_SIDE = 768
OPENAI_IMAGE_TILE_SIZE = 512
GOOGLE_IMAGE_TILE_TOKENS = 258
GOOGLE_IMAGE_SMALL_SIDE = 384
GOOGLE_IMAGE_TILE_SIZE = 768
OPENAI_AUDIO_TOKENS_PER_SECOND = 50
GOOGLE_AUDIO_TOKENS_PER_SECOND = 32
GOOGLE_VIDEO_TOKENS_PER_SECOND = 300
OPENAI_PDF_TOKENS_PER_PAGE = 2000
GOOGLE_PDF_TOKENS_PER_PAGE = 1500
DEFAULT_IMAGE_DETAIL = "auto"


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Explicit token-bound accounting with method and uncertainty."""

    tokens: int | None
    method: str
    uncertainty: str
    native: bool = False
    margin_tokens: int = 0
    uncertain_local: bool = False


@dataclass(frozen=True, slots=True)
class AttachmentDecision:
    """Fresh per-request delivery verdict for one attachment occurrence."""

    key: tuple
    descriptor: object
    wire_role: str
    modality: str | None
    outcome: str
    reason: str | None = None
    blob_path: Path | None = None
    estimate: TokenEstimate | None = None
    detail: str = DEFAULT_IMAGE_DETAIL

    @property
    def is_native(self):
        return self.outcome == "native"

    def reference_text(self):
        return self.descriptor.reference(self.reason)


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    """Complete per-request attachment decisions for one prepared request."""

    api_name: str
    decisions: dict
    native_count: int
    media_tokens: int
    estimates: tuple
    by_entry: dict
    by_tool: dict

    def for_attachment(self, key):
        return self.decisions.get(key)

    def for_entry(self, entry):
        return self.by_entry.get(id(entry), ())

    def for_tool(self, result):
        return self.by_tool.get(id(result), ())


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """Route-specific prepared wire body paired with its exact accounting."""

    api_name: str
    body: dict
    plan: DeliveryPlan
    route: str | None = None
    plan_hash: str = ""
    native_token_estimate: TokenEstimate | None = None

    @property
    def has_native_media(self):
        return self.plan.native_count > 0

    @property
    def native_media_count(self):
        return self.plan.native_count

    @property
    def media_tokens(self):
        return self.plan.media_tokens


def normalize_media_type(media_type):
    if not isinstance(media_type, str):
        return "application/octet-stream"
    return media_type.split(";", 1)[0].strip().lower() or "application/octet-stream"


def modality_of(media_type):
    """Map a media type to its canonical native modality, or None."""
    normalized = normalize_media_type(media_type)
    if normalized in IMAGE_MIME_TYPES:
        return "image"
    if normalized in OPENAI_AUDIO_FORMATS or normalized in GOOGLE_AUDIO_MIME_TYPES:
        return "audio"
    if normalized in VIDEO_MIME_TYPES:
        return "video"
    if normalized in PDF_MIME_TYPES:
        return "pdf"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return None


def audio_format_of(media_type):
    """Wire audio format string for OpenAI input_audio ('wav' or 'mp3'), or None."""
    return OPENAI_AUDIO_FORMATS.get(normalize_media_type(media_type))


def _format_supported(api_name, modality, media_type):
    normalized = normalize_media_type(media_type)
    if modality == "image":
        return normalized in IMAGE_MIME_TYPES
    if modality == "pdf":
        return normalized in PDF_MIME_TYPES
    if modality == "audio":
        if api_name == "chat_completions":
            return normalized in OPENAI_AUDIO_FORMATS
        if api_name == "google_interactions":
            return normalized in GOOGLE_AUDIO_MIME_TYPES
        return False
    if modality == "video":
        return api_name == "google_interactions" and normalized in VIDEO_MIME_TYPES
    return False


def _file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_blob_path(descriptor):
    """Locate a readable non-empty attachment file on disk, or None.

    Prefers the immutable content-addressed blob store before `descriptor.path`,
    and verifies `sha256` whenever `descriptor.sha256` is set so a later
    workspace symlink swap cannot leak another host file.
    """
    digest = descriptor.sha256
    has_digest = isinstance(digest, str) and SHA256_RE.fullmatch(digest) is not None
    candidates = []
    if has_digest:
        candidates.append((
            home_paths.Paths().state / "attachments" / "blobs" / digest[:2] / digest,
            True,
        ))
    if descriptor.path:
        candidates.append((Path(descriptor.path), False))
    for candidate, is_store_blob in candidates:
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                continue
            if has_digest and not is_store_blob:
                if _file_sha256(candidate) != digest:
                    continue
            return candidate
        except OSError:
            continue
    return None


def read_base64(blob_path, *, expected_sha256=None):
    """Read raw attachment bytes from disk and base64-encode on demand."""
    raw = Path(blob_path).read_bytes()
    if (
        isinstance(expected_sha256, str)
        and SHA256_RE.fullmatch(expected_sha256)
        and sha256(raw).hexdigest() != expected_sha256
    ):
        raise ValueError("attachment content digest mismatch on disk")
    return base64.b64encode(raw).decode("ascii")


def _openai_image_tokens(width, height, detail=DEFAULT_IMAGE_DETAIL):
    if detail == "low":
        return TokenEstimate(
            OPENAI_IMAGE_BASE_TOKENS,
            "image_tiling",
            "fixed bound (detail=low)",
        )
    w, h = float(width), float(height)
    if max(w, h) > OPENAI_IMAGE_MAX_SIDE:
        scale = OPENAI_IMAGE_MAX_SIDE / max(w, h)
        w *= scale
        h *= scale
    if min(w, h) > OPENAI_IMAGE_SHORT_SIDE:
        scale = OPENAI_IMAGE_SHORT_SIDE / min(w, h)
        w *= scale
        h *= scale
    tiles = math.ceil(w / OPENAI_IMAGE_TILE_SIZE) * math.ceil(
        h / OPENAI_IMAGE_TILE_SIZE,
    )
    total = OPENAI_IMAGE_BASE_TOKENS + OPENAI_IMAGE_TILE_TOKENS * max(1, tiles)
    return TokenEstimate(
        total,
        "image_tiling",
        f"upper bound (detail={detail}, {max(1, tiles)}x512px tiles)",
    )


def _google_image_tokens(width, height):
    if width <= GOOGLE_IMAGE_SMALL_SIDE and height <= GOOGLE_IMAGE_SMALL_SIDE:
        return TokenEstimate(
            GOOGLE_IMAGE_TILE_TOKENS,
            "image_tiling",
            "upper bound (single <=384px tile)",
        )
    tiles = math.ceil(width / GOOGLE_IMAGE_TILE_SIZE) * math.ceil(
        height / GOOGLE_IMAGE_TILE_SIZE,
    )
    total = GOOGLE_IMAGE_TILE_TOKENS * max(1, tiles)
    return TokenEstimate(
        total,
        "image_tiling",
        f"upper bound ({max(1, tiles)}x768px tiles)",
    )


def estimate_attachment_tokens(descriptor, api_name, *, detail=DEFAULT_IMAGE_DETAIL):
    """Compute an explicit modality token bound from validated metadata only.

    Never derives token counts from raw file bytes. Missing or unvalidated
    metadata yields `tokens=None` ("unbounded") so unbounded sends are withheld
    unless a provider native counter supplies a trusted bound.
    """
    modality = modality_of(descriptor.media_type)
    if modality == "image":
        if descriptor.width is None or descriptor.height is None:
            return TokenEstimate(
                None,
                "unbounded",
                "missing validated image dimensions",
                uncertain_local=True,
            )
        if api_name == "google_interactions":
            return _google_image_tokens(descriptor.width, descriptor.height)
        return _openai_image_tokens(
            descriptor.width, descriptor.height, detail=detail,
        )
    if modality == "audio":
        duration = descriptor.duration_seconds
        if duration is None or not math.isfinite(duration) or duration <= 0:
            return TokenEstimate(
                None,
                "unbounded",
                "missing validated audio duration",
                uncertain_local=True,
            )
        rate = (
            GOOGLE_AUDIO_TOKENS_PER_SECOND
            if api_name == "google_interactions"
            else OPENAI_AUDIO_TOKENS_PER_SECOND
        )
        total = max(rate, math.ceil(duration * rate))
        return TokenEstimate(
            total,
            "audio_duration",
            f"upper bound ({rate} tok/s over {duration:g}s)",
        )
    if modality == "video":
        duration = descriptor.duration_seconds
        if duration is None or not math.isfinite(duration) or duration <= 0:
            return TokenEstimate(
                None,
                "unbounded",
                "missing validated video duration",
                uncertain_local=True,
            )
        total = max(GOOGLE_VIDEO_TOKENS_PER_SECOND,
                    math.ceil(duration * GOOGLE_VIDEO_TOKENS_PER_SECOND))
        return TokenEstimate(
            total,
            "video_duration",
            f"upper bound (1 fps + audio, 300 tok/s over {duration:g}s)",
        )
    if modality == "pdf":
        pages = descriptor.pages
        if pages is None or pages <= 0:
            return TokenEstimate(
                None,
                "unbounded",
                "missing validated PDF page count",
                uncertain_local=True,
            )
        per_page = (
            GOOGLE_PDF_TOKENS_PER_PAGE
            if api_name == "google_interactions"
            else OPENAI_PDF_TOKENS_PER_PAGE
        )
        return TokenEstimate(
            pages * per_page,
            "pdf_pages",
            f"upper bound ({per_page} tok/page x {pages} pages; dense pages vary)",
            uncertain_local=True,
        )
    return TokenEstimate(
        None,
        "reference_only",
        "non-media attachment delivered as text reference",
    )


def _check_static_eligibility(descriptor, *, api_name, wire_role, capabilities):
    """Validate delivery flag, modality, capability, format, and disk blob."""
    if descriptor.delivery == "reference":
        return AttachmentDecision(
            (), descriptor, wire_role, modality_of(descriptor.media_type),
            outcome="reference", reason=None,
        )
    modality = modality_of(descriptor.media_type)
    if modality is None:
        return AttachmentDecision(
            (), descriptor, wire_role, None, outcome="reference", reason=None,
        )
    if capabilities is None:
        return AttachmentDecision(
            (), descriptor, wire_role, modality, outcome="withheld",
            reason=f"not sent natively: {modality} input support is unknown "
                   "for this model",
        )
    raw_support = capabilities.input.get(modality)
    if raw_support is False:
        return AttachmentDecision(
            (), descriptor, wire_role, modality, outcome="withheld",
            reason=f"not sent natively: this model does not support "
                   f"{modality} input",
        )
    if raw_support is not True:
        return AttachmentDecision(
            (), descriptor, wire_role, modality, outcome="withheld",
            reason=f"not sent natively: {modality} input support is unknown "
                   "for this model",
        )
    allowed_roles = WIRE_ROLE_MODALITIES.get((api_name, wire_role), frozenset())
    if modality not in allowed_roles:
        return AttachmentDecision(
            (), descriptor, wire_role, modality, outcome="withheld",
            reason=f"not sent natively: {modality} attachments are not carried "
                   f"in {wire_role} turns on this API",
        )
    if not _format_supported(api_name, modality, descriptor.media_type):
        return AttachmentDecision(
            (), descriptor, wire_role, modality, outcome="withheld",
            reason=f"not sent natively: {descriptor.media_type} is not an "
                   "accepted inline format on this API",
        )
    blob_path = resolve_blob_path(descriptor)
    if blob_path is None:
        return AttachmentDecision(
            (), descriptor, wire_role, modality, outcome="withheld",
            reason="not sent natively: attachment file is unavailable on disk",
        )
    estimate = estimate_attachment_tokens(descriptor, api_name)
    return AttachmentDecision(
        (), descriptor, wire_role, modality, outcome="candidate",
        blob_path=blob_path, estimate=estimate,
    )


def collect_occurrences(conversation, projections):
    """Collect every attachment occurrence in chronological order."""
    found = []
    for entry in conversation:
        projected = projections[id(entry)]
        for idx, descriptor in enumerate(projected.attachments):
            role = "assistant" if projected.assistant else "user"
            found.append((("entry", id(entry), idx), descriptor, role))
        for result in projected.tools[1]:
            for idx, descriptor in enumerate(
                projected.tool_attachments.get(id(result), ()),
            ):
                found.append((("tool", id(result), idx), descriptor, "tool"))
    return found


def has_conversation_attachments(conversation, projections=None):
    """Fast check for whether any entry in `conversation` carries attachments."""
    if projections is not None:
        return any(
            projections[id(entry)].attachments
            or projections[id(entry)].tool_attachments
            for entry in conversation
        )
    for entry in conversation:
        if not entry.entries:
            continue
        for child in entry.entries:
            if child.type == "attachments/file":
                return True
            if child.entries:
                for grandchild in child.entries:
                    if grandchild.type == "attachments/file":
                        return True
    return False


def plan_delivery(
    conversation,
    projections,
    *,
    api_name,
    capabilities=None,
    media_budget=None,
    allow_uncertain_candidates=False,
    forced_withheld=None,
):
    """Decide native vs reference delivery for every attachment in `conversation`.

    Evaluates newest attachments first when `media_budget` is constrained so
    the most recent media gets priority over stale older turns. Never promotes
    `delivery == "reference"`, unsupported/unknown modalities, missing files,
    or unbounded media (unless `allow_uncertain_candidates` is True for a
    native-counting pass).
    """
    occurrences = collect_occurrences(conversation, projections)
    forced = forced_withheld or {}
    decisions = {}
    candidates = []

    for key, descriptor, wire_role in occurrences:
        if key in forced:
            decisions[key] = AttachmentDecision(
                key, descriptor, wire_role, modality_of(descriptor.media_type),
                outcome="withheld", reason=forced[key],
            )
            continue
        if wire_role == "assistant":
            decisions[key] = AttachmentDecision(
                key, descriptor, wire_role, modality_of(descriptor.media_type),
                outcome="reference", reason=None,
            )
            continue
        verdict = _check_static_eligibility(
            descriptor, api_name=api_name, wire_role=wire_role,
            capabilities=capabilities,
        )
        verdict = AttachmentDecision(
            key, verdict.descriptor, verdict.wire_role, verdict.modality,
            verdict.outcome, verdict.reason, verdict.blob_path, verdict.estimate,
            verdict.detail,
        )
        if verdict.outcome != "candidate":
            decisions[key] = verdict
            continue
        if verdict.estimate.tokens is None and not allow_uncertain_candidates:
            decisions[key] = AttachmentDecision(
                key, descriptor, wire_role, verdict.modality, outcome="withheld",
                reason=f"not sent natively: no trusted token bound "
                       f"({verdict.estimate.uncertainty})",
                estimate=verdict.estimate,
            )
            continue
        candidates.append(verdict)

    # Allocate context room newest-first so current-turn media wins under pressure.
    remaining = media_budget
    native_count = 0
    total_media_tokens = 0
    for verdict in reversed(candidates):
        cost = verdict.estimate.tokens
        if remaining is not None and cost is not None and cost > remaining:
            decisions[verdict.key] = AttachmentDecision(
                verdict.key, verdict.descriptor, verdict.wire_role,
                verdict.modality, outcome="withheld",
                reason=f"not sent natively: estimated {cost:,} tokens exceeds "
                       f"remaining context room ({remaining:,} tokens)",
                estimate=verdict.estimate,
            )
            continue
        if remaining is not None and cost is not None:
            remaining -= cost
        if cost is not None:
            total_media_tokens += cost
        native_count += 1
        decisions[verdict.key] = AttachmentDecision(
            verdict.key, verdict.descriptor, verdict.wire_role,
            verdict.modality, outcome="native", blob_path=verdict.blob_path,
            estimate=verdict.estimate, detail=verdict.detail,
        )

    ordered_estimates = tuple(
        decisions[key] for key, _, _ in occurrences if key in decisions
    )
    by_entry = {}
    by_tool = {}
    for decision in ordered_estimates:
        kind, owner_id, _ = decision.key
        target = by_entry if kind == "entry" else by_tool
        target.setdefault(owner_id, []).append(decision)
    return DeliveryPlan(
        api_name=api_name,
        decisions=decisions,
        native_count=native_count,
        media_tokens=total_media_tokens,
        estimates=ordered_estimates,
        by_entry={k: tuple(v) for k, v in by_entry.items()},
        by_tool={k: tuple(v) for k, v in by_tool.items()},
    )


def plan_needs_native_count(plan):
    """True when known-supported native media has uncertain local cost."""
    return any(
        item.is_native
        and item.estimate is not None
        and (item.estimate.tokens is None or item.estimate.uncertain_local)
        for item in plan.estimates
    )


def request_hash(body):
    """Stable SHA-256 digest of a prepared JSON request body."""
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(raw.encode("utf-8")).hexdigest()


def with_max_output_tokens(api_name, body, limit):
    """Return a shallow copy of a prepared wire body with `limit` applied."""
    if limit is None:
        return body
    updated = dict(body)
    if api_name == "responses":
        updated["max_output_tokens"] = limit
    elif api_name == "chat_completions":
        updated["max_completion_tokens"] = limit
    elif api_name == "google_interactions":
        gen = dict(updated.get("generation_config") or {})
        gen["max_output_tokens"] = limit
        updated["generation_config"] = gen
    return updated


def prepare_with_native_count(
    conversation,
    projections,
    *,
    api_name,
    capabilities=None,
    media_budget=None,
    total_budget=None,
    route=None,
    build_body,
    native_counter=None,
):
    """Prepare one route-specific request body, optionally invoking native counting.

    Native counting runs ONLY when `native_counter` is provided AND the
    candidate plan has known-supported native media with uncertain or missing
    local bounds. Counted and sent bodies are always identical.
    """
    if not has_conversation_attachments(conversation, projections):
        empty_plan = DeliveryPlan(api_name, {}, 0, 0, (), {}, {})
        body = build_body(empty_plan)
        return PreparedRequest(
            api_name=api_name, body=body, plan=empty_plan, route=route,
        )

    can_count = native_counter is not None
    forced = {}
    native_estimate = None

    while True:
        plan = plan_delivery(
            conversation,
            projections,
            api_name=api_name,
            capabilities=capabilities,
            media_budget=media_budget,
            allow_uncertain_candidates=can_count,
            forced_withheld=forced,
        )
        body = build_body(plan)
        if not (can_count and plan_needs_native_count(plan)):
            return PreparedRequest(
                api_name=api_name,
                body=body,
                plan=plan,
                route=route,
                plan_hash=request_hash(body),
                native_token_estimate=native_estimate,
            )

        counted = native_counter(body, route=route)
        if counted is None or counted.tokens is None:
            # Native counter unavailable or failed: fall back strictly to local bounds.
            can_count = False
            native_estimate = None
            continue

        native_estimate = counted
        if total_budget is None or counted.tokens <= total_budget:
            return PreparedRequest(
                api_name=api_name,
                body=body,
                plan=plan,
                route=route,
                plan_hash=request_hash(body),
                native_token_estimate=counted,
            )

        # Over budget per native count: withhold the oldest uncertain/native item.
        droppable = [
            item for item in plan.estimates
            if item.is_native and item.estimate is not None
            and (item.estimate.tokens is None or item.estimate.uncertain_local)
        ] or [item for item in plan.estimates if item.is_native]
        if not droppable:
            return PreparedRequest(
                api_name=api_name,
                body=body,
                plan=plan,
                route=route,
                plan_hash=request_hash(body),
                native_token_estimate=counted,
            )
        victim = droppable[0]
        forced[victim.key] = (
            f"not sent natively: counted request ({counted.tokens:,} tokens, "
            f"{counted.uncertainty}) exceeds available context room "
            f"({total_budget:,} tokens)"
        )
        native_estimate = None

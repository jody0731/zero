"""Normalize per-modality input token usage across model API responses."""

from collections.abc import Mapping

from components.model import MODALITIES

MODALITY_ALIASES = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "video": "video",
    "pdf": "pdf",
    "document": "pdf",
    "file": "pdf",
}

DETAIL_FIELD_MAP = (
    ("text_tokens", "text"),
    ("image_tokens", "image"),
    ("audio_tokens", "audio"),
    ("video_tokens", "video"),
    ("pdf_tokens", "pdf"),
    ("document_tokens", "pdf"),
)


def _positive_int(value):
    return value if type(value) is int and value > 0 else None


def _canonical_modality(raw):
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    if token.endswith("_tokens"):
        token = token.removesuffix("_tokens")
    if token.startswith("modality_"):
        token = token.removeprefix("modality_")
    return MODALITY_ALIASES.get(token)


def _field_of(obj, key):
    if isinstance(obj, Mapping):
        return obj.get(key)
    value = getattr(obj, key, None)
    if value is not None:
        return value
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra.get(key)
    return None


def _add_counts(totals, raw):
    if raw is None:
        return
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            modality = _canonical_modality(key)
            count = _positive_int(value)
            if modality is not None and count is not None:
                totals[modality] = totals.get(modality, 0) + count
        return
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            modality = _canonical_modality(
                item.get("modality") or item.get("type"),
            )
            count = _positive_int(
                item.get("token_count")
                if "token_count" in item
                else item.get("tokenCount", item.get("tokens")),
            )
            if modality is not None and count is not None:
                totals[modality] = totals.get(modality, 0) + count


def normalize_modality_tokens(*sources):
    """Return canonical `{modality: token_count}` for positive counts, or None."""
    totals = {}
    for source in sources:
        if source is None:
            continue
        if isinstance(source, (Mapping, list, tuple)):
            _add_counts(totals, source)
            continue
        for field_name, modality in DETAIL_FIELD_MAP:
            count = _positive_int(_field_of(source, field_name))
            if count is not None:
                totals[modality] = totals.get(modality, 0) + count
        for nested_key in (
            "input_tokens_by_modality",
            "total_input_tokens_by_modality",
            "prompt_tokens_details",
            "promptTokensDetails",
            "modality_details",
        ):
            nested = _field_of(source, nested_key)
            if nested is not None and nested is not source:
                _add_counts(totals, nested)
    ordered = {name: totals[name] for name in MODALITIES if totals.get(name, 0) > 0}
    return ordered or None


def extract_detail_modality_tokens(usage_obj, detail_attr):
    """Extract modality counts from a usage object and its token-detail child."""
    if usage_obj is None:
        return None
    details = _field_of(usage_obj, detail_attr)
    sources = []
    if details is not None:
        sources.append(details)
    for key in (
        "input_tokens_by_modality",
        "total_input_tokens_by_modality",
        "promptTokensDetails",
        "prompt_tokens_details",
    ):
        found = _field_of(usage_obj, key)
        if found is not None and found is not details:
            sources.append(found)
    return normalize_modality_tokens(*sources)


def non_text_tokens(by_modality):
    """Total positive non-text input tokens reported in a modality breakdown."""
    if not isinstance(by_modality, Mapping):
        return 0
    return sum(
        count for key, count in by_modality.items()
        if key != "text" and type(count) is int and count > 0
    )

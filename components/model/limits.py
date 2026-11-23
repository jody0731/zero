"""Token limits are separate facts about an exact model at an endpoint."""

from dataclasses import dataclass, fields


def token_count(value):
    """Accept positive whole tokens, including in data supplied by servers."""
    return value if type(value) is int and 0 < value < 2**53 else None


def model_id(value):
    return (
        isinstance(value, str) and 0 < len(value) <= 512
        and value == value.strip() and value.isprintable()
    )


def endpoint(url):
    """The configured route, treating a trailing slash as SDKs do."""
    return url.rstrip("/")


def smaller(*values):
    return min((value for value in values if value is not None), default=None)


@dataclass(frozen=True)
class TokenLimits:
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None

    @classmethod
    def read(cls, row):
        if not isinstance(row, dict):
            return cls()
        total = token_count(row.get("context_window"))
        input_tokens = token_count(row.get("max_input_tokens"))
        output_tokens = token_count(row.get("max_output_tokens"))
        return cls(
            total,
            input_tokens if total is None or (input_tokens or 0) <= total else None,
            output_tokens if total is None or (output_tokens or 0) <= total else None,
        )

    def capped_by(self, other):
        return TokenLimits(
            smaller(self.context_window, other.context_window),
            smaller(self.max_input_tokens, other.max_input_tokens),
            smaller(self.max_output_tokens, other.max_output_tokens),
        )

    def as_dict(self):
        return {f.name: value for f in fields(self)
                if (value := getattr(self, f.name)) is not None}


UNKNOWN = TokenLimits()

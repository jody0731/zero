"""Stand-ins at tool entry; values replaced before output leaves the harness."""

import base64
import html
import json
import re
from urllib.parse import quote, quote_plus

from core.tools import Result

REFERENCE = re.compile(r"⟦vault:([a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)?)⟧")


def walk(value, transform, numbers=False, keys=False):
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, dict):
        return {(transform(str(key)) if keys else key):
                walk(item, transform, numbers, keys)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [walk(item, transform, numbers, keys) for item in value]
    if numbers and type(value) in (int, float):
        text = str(value)
        safe = transform(text)
        return value if safe == text else safe
    return value


class Boundary:
    def __init__(self, vault):
        self.vault = vault

    def replacements(self):
        found = {}
        values = self.vault.values()
        if self.vault.key_value:
            values[self.vault.key_value] = "[vault encryption key]"
        for value, stand_in in values.items():
            variants = {value, quote(value, safe=""), quote_plus(value),
                        html.escape(value), json.dumps(value)[1:-1],
                        json.dumps(value, ensure_ascii=False)[1:-1],
                        base64.b64encode(value.encode()).decode()}
            for variant in variants:
                found[variant] = stand_in
        return found

    def redact(self, value, partial=False, include_numbers=False,
               include_keys=False):
        replacements = self.replacements()
        if not replacements:
            return value
        pattern = re.compile("|".join(map(re.escape,
                                         sorted(replacements, key=len,
                                                reverse=True))))

        def text(raw):
            # Existing stand-ins are opaque, even when a short secret happens
            # to occur in their names.
            pieces = re.split(r"(⟦vault:[^⟧]+⟧)", raw)
            for index in range(0, len(pieces), 2):
                original = pieces[index]
                if partial and index == len(pieces) - 1:
                    keep = 0
                    for secret in replacements:
                        for length in range(1, min(len(secret), len(original)) + 1):
                            if original.endswith(secret[:length]):
                                keep = max(keep, length)
                    if keep:
                        original = original[:-keep]
                pieces[index] = pattern.sub(
                    lambda match: replacements[match[0]], original)
            return "".join(pieces)

        return walk(value, text, numbers=include_numbers, keys=include_keys)

    def resolve(self, arguments):
        references = set()

        def find(text):
            references.update(REFERENCE.findall(text))
            return text

        walk(arguments, find, keys=True)
        if not references:
            return arguments
        values = self.vault.resolve(references)
        return walk(arguments, lambda text: REFERENCE.sub(
            lambda match: values[match[1]], text), keys=True)

    def result(self, answer):
        return Result(self.redact(answer.output), head=self.redact(answer.head),
                      fields=self.redact(answer.fields, include_numbers=True,
                                         include_keys=True), failed=answer.failed,
                      retry_arguments=self.redact(answer.retry_arguments))

    def stream(self):
        return RedactedStream(self)


class RedactedStream:
    """Keep a possible secret prefix across chunks, before clipping or saving."""

    def __init__(self, boundary):
        self.boundary = boundary
        self.pending = b""

    def feed(self, data, final=False):
        pending = self.pending + data
        replacements = {key.encode(): value.encode()
                        for key, value in self.boundary.replacements().items()}
        if not replacements:
            self.pending = b""
            return pending
        pattern = re.compile(b"|".join(map(re.escape,
                                          sorted(replacements, key=len,
                                                 reverse=True))))
        # Substitute complete matches first so a full match is never divided
        # by the retained suffix.
        output = bytearray()
        position = 0
        for match in pattern.finditer(pending):
            output.extend(pending[position:match.start()])
            output.extend(replacements[match[0]])
            position = match.end()
        tail = pending[position:]
        keep = 0
        if not final:
            for secret in replacements:
                for length in range(1, min(len(secret) - 1, len(tail)) + 1):
                    if tail.endswith(secret[:length]):
                        keep = max(keep, length)
        output.extend(tail[:-keep] if keep else tail)
        self.pending = tail[-keep:] if keep else b""
        return bytes(output)

"""Command output as the model reads it: the start and the end kept, the middle
cut, under a header saying how much there was (a job's look clips with
jobs/process.py's `clip`). The limit is the session's bash_output_chars, passed in.
"""

import codecs
import io

from jobs.process import clip


class OutputBuffer:
    """Keep a bounded head and tail, and count the full decoded output."""

    def __init__(self, limit):
        self.limit = limit
        self.head = ""
        self.tail = ""
        self.characters = 0
        self.newlines = 0
        self.ends_with_newline = True
        self.decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True
        )

    def add(self, data, final=False):
        text = self.decoder.decode(data, final=final)
        self.characters += len(text)
        self.newlines += text.count("\n")
        if text:
            self.ends_with_newline = text.endswith("\n")
        half = self.limit // 2
        take = half - len(self.head)
        self.head += text[:take]
        self.tail = (self.tail + text[take:])[-half:]

    def clipped(self):
        if self.characters <= self.limit:
            return self.head + self.tail
        cut = self.characters - self.limit
        return f"{self.head}\n[... {cut} characters cut ...]\n{self.tail}"


def format_output(status, seconds, output, limit=None):
    """Codex-style result: status, wall time, size if cut, then the output.
    `limit`: for a string; an OutputBuffer has its own."""
    if isinstance(output, OutputBuffer):
        size = output.characters
        count = output.newlines + (not output.ends_with_newline)
        text = output.clipped()
        limit = output.limit
    else:
        size = len(output)
        count = output.count("\n") + (not output.endswith("\n"))
        text = clip(output, limit)
    lines = [status, f"Wall time: {seconds:.1f} s"]
    if size > limit:
        lines.append(
            f"Total output: {count} lines, {size} characters "
            "(the middle is cut below)"
        )
    lines.append("Output:")
    lines.append(text or "(no output)")
    return "\n".join(lines)

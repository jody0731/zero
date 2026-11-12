"""Connecting a model: a server's models asked for, a test call of a few
tokens, and the server, its key and the user's default saved (model/service.py's
messages).

A server is asked for its models at the OpenAI APIs' standard door,
/v1/models (with each one's context window where the server says: vLLM's
max_model_len, LM Studio's and OpenRouter's context_length), then for as
few tokens as its API allows from the chosen model, in the API its entry
names (apis/; a server of the user's own speaks the Chat Completions API).
A server that lists nothing (a 404 there) is proven by the call alone.
Nothing is saved until the call answers.

What is saved: the key in keys.toml under the server's name (mode 600); in
models.toml only the lines the catalog doesn't already give (a server of
the user's own: its title, url and context window; a shipped one: what differs), its
table rewritten whole and the rest of the file kept as it is, comments too.
A key is never put in a reply: any error that would echo it is scrubbed.
"""

import json
import re
from pathlib import Path

import openai
from openai import OpenAI

import paths
from core.llm import complete
from model import servers
from model.apis import APIS, DEFAULT
from settings import sections

# What a server's name may be: what a model's name starts with
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
# Seconds a server has to list its models
LIST_SECONDS = 3
# Seconds the test call may stay silent: a local server may first load the
# model (a backstop, as model_silence_seconds)
TEST_SECONDS = 300
# Where servers report a model's context window in /v1/models
CONTEXT_WINDOW_FIELDS = ("max_model_len", "context_length", "max_context_length")


class ConnectError(Exception):
    """Why a server can't be listed, tested or saved; said to the face."""


def scrub(text, key):
    """The text with the key cut out, should a server echo it."""
    if key and key != servers.NO_KEY:
        text = text.replace(key, "…")
    return text


def error_text(error, key):
    return scrub(f"{type(error).__name__}: {error}", key)[:300]


def listing(url, key):
    """The server's models: [{"id", "context_window"}], context_window None where the
    server doesn't say. ConnectError when it can't be asked; NotFound
    (openai's) passes, for the caller to judge."""
    client = OpenAI(base_url=url, api_key=key, timeout=LIST_SECONDS,
                    max_retries=0)
    try:
        items = client.models.list().data
    except openai.NotFoundError:
        raise
    except Exception as e:
        raise ConnectError(f"{url} didn't list its models: {error_text(e, key)}") from e
    found = []
    for item in items:
        data = item.model_dump()
        window = next((data[f] for f in CONTEXT_WINDOW_FIELDS
                       if isinstance(data.get(f), int)), None)
        found.append({"id": item.id, "context_window": window})
    return found


def probe(url, key):
    """(the url that answered, its models). A URL without /v1 that finds
    nothing is tried again with it, as local servers serve there."""
    try:
        return url, listing(url, key)
    except openai.NotFoundError as e:
        if not url.rstrip("/").endswith("/v1"):
            other = url.rstrip("/") + "/v1"
            try:
                return other, listing(other, key)
            except openai.NotFoundError:
                pass
        raise ConnectError(
            f"{url} has no /models: OpenAI-style servers usually serve "
            f"under /v1 ({error_text(e, key)})"
        ) from e


def test_call(url, key, model, api):
    """As few output tokens as the server's API allows, from the model, in
    that API (apis/); ConnectError if they don't come."""
    client = OpenAI(base_url=url, api_key=key, timeout=TEST_SECONDS,
                    max_retries=0)
    try:
        complete(api, client, model, [{"role": "user", "content": "Say OK."}],
                 silence=TEST_SECONDS, max_output_tokens=api.MIN_OUTPUT_TOKENS)
    except Exception as e:
        raise ConnectError(
            f"the test call to {model} at {url} failed: {error_text(e, key)}"
        ) from e


# ------------------------------------------------------------- the files


def value(v):
    """A TOML value: strings as JSON's, numbers, booleans, lists."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(value(x) for x in v) + "]"
    return str(v)


def table_text(name, lines):
    """A server's table: its lines, then its models' own, one a line."""
    text = [f"[{name}]\n"]
    text += [f"{k} = {value(v)}\n" for k, v in lines.items() if k != "models"]
    models = lines.get("models") or {}
    if models:
        text.append(f"\n[{name}.models]\n")
        for model, line in models.items():
            inner = ", ".join(f"{k} = {value(v)}" for k, v in line.items())
            text.append(f"{json.dumps(model)} = {{ {inner} }}\n" if inner
                        else f"{json.dumps(model)} = {{}}\n")
    return "".join(text)


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# A table's header line: its name, bare or quoted, and a comment after it
HEADER = re.compile(r'^\[\s*("(?:[^"\\]|\\.)*"|[^\]#]*?)\s*\]\s*(?:#.*)?$')


def table_name(header):
    """The table a header line opens (`[deepseek] # mine`, `["deepseek"]`,
    `[deepseek.models]`), or None for no header."""
    match = HEADER.match((header or "").strip())
    if not match:
        return None
    name = match.group(1)
    return json.loads(name) if name.startswith('"') else name


def replaced(text, name, new):
    """The file's text with [name] and its subtables replaced by `new`
    (gone for None) where the first of them stood; the rest as it was."""
    pieces, at = [], None
    for header, piece in sections(text):
        inner = table_name(header) or ""
        if header and (inner == name or inner.startswith(name + ".")):
            if at is None:
                at = len(pieces)
            continue
        pieces.append(piece)
    if new is not None:
        block = new + "\n"
        if at is None:
            pieces.append(block)
        else:
            pieces.insert(at, block)
    # Tables apart by one blank line, the file ending in one newline
    joined = "".join(p if p.endswith("\n") or not p else p + "\n"
                     for p in pieces)
    joined = re.sub(r"\n{3,}", "\n\n", joined).lstrip("\n")
    return joined.rstrip("\n") + "\n" if joined.strip() else ""


def with_format(text, number):
    """The file's text with its format line once, under the comments that
    open it."""
    if re.search(rf"(?m)^\s*{paths.FILE_FORMAT_KEY}\s*=", text):
        return text
    lines = text.splitlines(keepends=True)
    top = 0
    while top < len(lines) and lines[top].lstrip().startswith("#"):
        top += 1
    line = f"{paths.FILE_FORMAT_KEY} = {number}\n"
    rest = "".join(lines[top:])
    return "".join(lines[:top]) + line + ("\n" + rest if rest.strip() else "")


def save_key(name, key, path=None):
    """The server's key in keys.toml (owner-only from the start); None takes
    it out."""
    path = Path(path or paths.keys_file())
    with paths.changing(path):
        text = read(path)
        if key is None and name not in {table_name(h) for h, _ in
                                        sections(text)}:
            return
        new = None if key is None else table_text(name, {"api_key": key})
        text = with_format(replaced(text, name, new), servers.KEYS_FILE_FORMAT)
        paths.write_whole(path, text, mode=0o600)


def save_server(name, lines, path=None):
    """The server's table in models.toml; None takes it out."""
    path = Path(path or paths.models_file())
    with paths.changing(path):
        text = read(path)
        new = None if lines is None else table_text(name, lines)
        text = with_format(replaced(text, name, new),
                           servers.MODELS_FILE_FORMAT)
        paths.write_whole(path, text, mode=0o644)


def own_lines(name, path=None):
    """What the user's models.toml says of the server: its table, or {}."""
    path = path or paths.models_file()
    return dict(servers.load(path, path).get(name, {}))


def catalog():
    return servers.load(servers.CATALOG, servers.CATALOG)


# ---------------------------------------------------------- the messages


def full_name(name, model):
    """The name a session takes: bare for Ollama's models."""
    return model if name == servers.UNPREFIXED_SERVER else f"{name}/{model}"


def reach(request):
    """(name, server or None, url, key) for a request: a stored key goes
    only to its own server's url; a typed url takes a typed key or none."""
    name = request.get("name")
    if name is not None and (not isinstance(name, str)
                             or not NAME.fullmatch(name)):
        raise ConnectError(
            "a server's name is lowercase letters, digits, - and _ (what its "
            f"models' names start with), not {name!r}"
        )
    known = servers.find(name) if name else None
    url = request.get("url")
    if url is not None and (not isinstance(url, str)
                            or not url.startswith(("http://", "https://"))):
        raise ConnectError(f"a server's url starts with http:// or https://, "
                           f"not {url!r}")
    url = url or (known.url if known else None)
    if not url:
        raise ConnectError("a server of one's own needs its url")
    typed = request.get("key")
    if typed is not None and not isinstance(typed, str):
        raise ConnectError("a key is a string; empty for none")
    if typed is not None:
        typed = typed.strip()
        if not typed and known and known.key_env:
            raise ConnectError(f"{known.title} needs its API key")
        key = typed or servers.NO_KEY
    elif known and url == known.url:
        try:
            key = servers.api_key(known)
        except servers.ModelServerError as e:
            raise ConnectError(str(e)) from e
    else:
        key = servers.NO_KEY
    return name, known, url, key


def probe_request(request):
    _, _, url, key = reach(request)
    url, models = probe(url, key)
    return {"url": url, "models": models}


def context_window_of(entry, model):
    """The context window the table already gives the model, or None."""
    if not entry:
        return None
    line = entry.get("models", {}).get(model, {})
    return line.get("context_window") or entry.get("context_window")


def connect(request):
    """Test the server's model, then save it: returns (model's full name,
    the lines written). ConnectError says why not, nothing saved."""
    if not request.get("name"):
        raise ConnectError("connect needs the server's name")
    name, known, url, key = reach(request)
    model = request.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConnectError("connect needs the model's name")
    model = model.strip().removeprefix(f"{name}/")
    context_window = request.get("context_window")
    if context_window is not None and (
        type(context_window) is not int or context_window <= 0
    ):
        raise ConnectError("a context window is a whole number of tokens")
    title = request.get("title")
    try:
        listed = listing(url, key)
    except openai.NotFoundError:
        # Lists nothing: the call alone proves it
        listed = []
    table = servers.table()
    entry = table.get(name)
    reported = next((m["context_window"] for m in listed if m["id"] == model),
                    None)
    current = context_window_of(entry, model)
    context_window = context_window or reported or current
    if context_window is None:
        raise ConnectError(
            f"no context window for {model}: the server doesn't say, so give "
            "it (tokens)"
        )
    api = APIS[(entry or {}).get("api", DEFAULT)]
    test_call(url, key, model, api)
    lines = own_lines(name)
    before = dict(lines)
    shipped = catalog().get(name)
    if shipped is None:
        # Its name for the screens first, then where it is
        rest = {k: v for k, v in lines.items() if k not in ("title", "url")}
        lines = {"title": title or lines.get("title") or name, "url": url,
                 **rest}
    elif url != shipped["url"] or "url" in lines:
        lines["url"] = url
    if context_window != current:
        if not lines.get("context_window") and not (
            (shipped or {}).get("context_window")
        ):
            lines["context_window"] = context_window
        else:
            models = {**lines.get("models", {})}
            models[model] = {**models.get(model, {}), "context_window": context_window}
            lines["models"] = models
    if request.get("key") is not None:
        typed = request["key"].strip()
        save_key(name, typed or None)
    if lines != before:
        save_server(name, lines)
    return full_name(name, model)


def disconnect(name):
    """The server's key and table out of the user's files."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConnectError(f"no server {name!r}")
    save_key(name, None)
    if name in servers.load(paths.models_file(), paths.models_file()):
        save_server(name, None)


def server_of(model):
    """The server a model name goes to, or None if it can't be told."""
    if not model:
        return None
    try:
        return servers.resolve(model)[0].name
    except (servers.ModelServerError, KeyError):
        prefix, _, rest = model.partition("/")
        return prefix if rest else servers.UNPREFIXED_SERVER


def not_ready_reason(model):
    """Why a session on the model couldn't start, or None: no call made,
    so a wrong key or a server that is down shows at the first turn."""
    if not model:
        return "no model is set yet"
    try:
        server, _ = servers.resolve(model)
        servers.api_key(server)
    except servers.ModelServerError as e:
        return str(e)
    return None


def rows():
    """Every server ZERO knows, for a face: whether its key is there (never
    the key), and whether the user's models.toml names it."""
    table = servers.table()
    shipped = catalog()
    mine = servers.load(paths.models_file(), paths.models_file())
    listed = []
    for name, entry in table.items():
        server = servers.ModelServer(name, entry)
        try:
            got = servers.api_key(server)
            key = "none needed" if got == servers.NO_KEY else "there"
        except servers.ModelServerError:
            key = "missing"
        listed.append({
            "name": name, "title": server.title, "url": server.url,
            "key_state": key, "needs_key": server.key_env is not None,
            "user_added": name in mine, "in_catalog": name in shipped,
            "context_window": server.context_window,
            # Each model the table names, with the window it would take
            "models": {m: context_window_of(entry, m)
                       for m in entry.get("models", {})},
        })
    return listed


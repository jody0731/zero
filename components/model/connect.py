"""Connecting a model: a server's models asked for, a test call of a few
tokens, and the server, its key and the user's default saved (model/service.py's
messages).

A server is asked for its models at /v1/models, including reported context
windows. The connection then asks for as few tokens as its API allows from
the chosen model, through the same reply API ordinary sessions use.
A server that lists nothing (a 404 there) is proven by the call alone.
An API that reports a window after loading is listed again after the test.
Nothing is saved until the call answers.

The key and its URL are saved in keys.toml. The server's connection and any window
the user gives for a particular model and URL are saved in models.toml.
A key is never put in a reply: any error that would echo it is scrubbed.
"""

import json
import re
from pathlib import Path

import openai

import paths
from core import entries
from core.llm import (
    MODEL_ERRORS, ModelHTTPError, complete, failure_text, request_error_details,
    request_failure, safe_error_text,
)
from components.model import directory, servers
from components.model.apis import APIS, DEFAULT
from components.model.limits import endpoint, model_id, token_count
from settings import sections

# What a server's name may be: what a model's name starts with
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
# Seconds the test call may stay silent: a local server may first load the
# model (a backstop, as model_silence_seconds)
TEST_SECONDS = 300
class ConnectError(Exception):
    """Why a server can't be listed, tested or saved; said to the face."""

    def __init__(self, message, *, model_failure=None):
        super().__init__(message)
        self.model_failure = model_failure


def error_text(error, key):
    key = None if key == servers.NO_KEY else key
    if isinstance(error, MODEL_ERRORS):
        return failure_text(request_error_details(error, key=key))
    return safe_error_text(f"{type(error).__name__}: {error}", key)


def is_not_found(error):
    return isinstance(error, openai.NotFoundError) or (
        isinstance(error, ModelHTTPError) and error.status_code == 404
    )


def listing(url, key, *, api=None, cached=False):
    """The server's models: [{"id", "context_window"}], context_window None where the
    server doesn't say. ConnectError when it can't be asked. NotFound
    passes, for the caller to judge."""
    try:
        return directory.listing(url, key, api=api, cached=cached)
    except Exception as e:
        if is_not_found(e):
            raise
        detail = error_text(e, key)
        raise ConnectError(f"Model list request failed · {detail}") from None


def probe(url, key, *, api=None):
    """(the url that answered, its models). A URL without /v1 that finds
    nothing is tried again with it when the API uses a /v1 prefix."""
    chosen = api or APIS[DEFAULT]
    try:
        return url, listing(url, key, api=chosen)
    except Exception as e:
        if not is_not_found(e):
            raise
        if chosen.RETRY_V1_PREFIX and not url.rstrip("/").endswith("/v1"):
            other = url.rstrip("/") + "/v1"
            try:
                return other, listing(other, key, api=chosen)
            except Exception as retry_error:
                if not is_not_found(retry_error):
                    raise
        prefix = " The usual API prefix is /v1" if chosen.RETRY_V1_PREFIX else ""
        raise ConnectError(
            f"{url} has no /models.{prefix} ({error_text(e, key)})."
        ) from e


def test_call(url, key, model, api, *, server=None, title=None):
    """As few output tokens as the server's API allows, from the model, in
    that API (apis/); ConnectError if they don't come."""
    redact_key = None if key == servers.NO_KEY else key
    try:
        with api.make_client(url, key, TEST_SECONDS) as client:
            conversation = (entries.root("text", "user", text="Say OK."),)
            complete(api, client, model, conversation,
                     silence=TEST_SECONDS, max_output_tokens=api.MIN_OUTPUT_TOKENS)
    except MODEL_ERRORS as error:
        report = request_failure(error, "test_model", server or "model server",
                                 title=title, key=redact_key)
        raise ConnectError(
            f"the test call failed: {failure_text(report)}",
            model_failure={**report, "next_action": "stop"},
        ) from None
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
    text += [f"{k} = {value(v)}\n" for k, v in lines.items()
             if k not in ("models", "context_windows")]
    models = lines.get("models") or {}
    if models:
        text.append(f"\n[{name}.models]\n")
        for model, line in models.items():
            inner = ", ".join(f"{k} = {value(v)}" for k, v in line.items())
            text.append(f"{json.dumps(model)} = {{ {inner} }}\n" if inner
                        else f"{json.dumps(model)} = {{}}\n")
    if windows := lines.get("context_windows"):
        text.append(f"\n[{name}.context_windows]\n")
        for url, entries in windows.items():
            inner = ", ".join(f"{value(model)} = {window}"
                              for model, window in entries.items())
            text.append(f"{value(url)} = {{ {inner} }}\n")
    return "".join(text)


def read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# A table's header line: its name, bare or quoted, and a comment after it
HEADER = re.compile(r'^\[\s*("(?:[^"\\]|\\.)*"|[^\]#]*?)\s*\]\s*(?:#.*)?$')


def table_name(header):
    """The table a header line opens (`[server] # mine`, `["server"]`,
    `[server.models]`), or None for no header."""
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


def write_key(path, name, key, url=None):
    """Write a server's key for its URL, or remove it when key is None."""
    text = read(path)
    if key is None and name not in {table_name(h) for h, _ in sections(text)}:
        return
    new = None if key is None else table_text(
        name, {"api_key": key, "url": endpoint(url)},
    )
    text = with_format(replaced(text, name, new), servers.KEYS_FILE_FORMAT)
    paths.write_whole(path, text, mode=0o600)


def set_key(name, key):
    """Change just the saved credential for a configured server."""
    if key is not None:
        if not isinstance(key, str) or not key.strip():
            raise ConnectError("Enter an API key to save.")
        key = key.strip()
        if any(character.isspace() or not character.isprintable() for character in key):
            raise ConnectError("An API key is one line without spaces.")
    keys = paths.keys_file()
    with paths.changing(paths.models_file(), keys):
        server = (servers.find(name) if isinstance(name, str) and NAME.fullmatch(name)
                  else None)
        if server is None:
            raise ConnectError("Choose a configured model server.")
        write_key(keys, name, key, server.url)
        return server


def save_server(name, lines, path=None):
    """The server's table in models.toml; None takes it out."""
    path = Path(path or paths.models_file())
    with paths.changing(path):
        write_server(path, name, lines)


def write_server(path, name, lines):
    new = None if lines is None else table_text(name, lines)
    text = with_format(replaced(read(path), name, new), servers.MODELS_FILE_FORMAT)
    paths.write_whole(path, text, mode=0o644)


def own_lines(name, path=None):
    """What the user's models.toml says of the server: its table, or {}."""
    path = path or paths.models_file()
    return dict(servers.load(path, path).get(name, {}))


def catalog():
    return servers.catalog()


# ---------------------------------------------------------- the messages


def full_name(name, model):
    """The name a session takes."""
    return model if name == servers.bare_server_name() else f"{name}/{model}"


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
        if not typed and known and known.key_env and endpoint(url) == endpoint(
            known.url,
        ):
            raise ConnectError(f"{known.title} needs its API key")
        key = typed or servers.NO_KEY
    elif known and endpoint(url) == endpoint(known.url):
        try:
            key = servers.api_key(known)
        except servers.ModelServerError as e:
            raise ConnectError(str(e)) from e
    else:
        key = servers.NO_KEY
    return name, known, url, key


def probe_request(request):
    _, known, url, key = reach(request)
    api = known.api if known and endpoint(url) == endpoint(known.url) else None
    url, models = probe(url, key, api=api)
    return {"url": url, "models": models}


def test_request(request):
    """Test one model with the saved connection and leave files alone."""
    name, server, _, key = reach({"name": request.get("name")})
    model = request.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConnectError("Enter the model's name.")
    model = model.strip().removeprefix(f"{name}/")
    if not model:
        raise ConnectError("Enter the model's name.")
    _, entry, _ = model_route({"name": name, "model": model})
    reply = servers.ModelServer(name, entry, model, refresh=False)
    test_call(reply.reply_url, key, model, reply.api, server=name, title=server.title)
    return full_name(name, model)


def model_route(request):
    """A model's server, current or proposed URL, and exact served ID."""
    model = request.get("model")
    if not model_id(model):
        raise ConnectError("Enter the model's name.")
    name = request.get("name")
    url = request.get("url")
    entries = servers.table()
    if name is None:
        if url is not None:
            raise ConnectError("Enter the server's name.")
        name, model = servers.model_address(model, entries)
    else:
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ConnectError("Enter a short name for the model server.")
        model = model.removeprefix(f"{name}/")
        if not model_id(model):
            raise ConnectError("Enter the model's name.")
    entry = entries.get(name)
    if url is None:
        if entry is None:
            raise ConnectError("Choose a configured model server.")
        return name, entry, model
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ConnectError("Enter the API URL starting with http:// or https://.")
    if entry is None or endpoint(entry["url"]) != endpoint(url):
        own = {**servers.without_endpoint_settings(own_lines(name)), "url": url}
        entry = servers.merge(catalog(), {name: own}, paths.models_file())[name]
    return name, entry, model


def save_window(lines, url, model, window):
    windows = lines.get("context_windows", {})
    url = endpoint(url)
    return {**lines, "context_windows": {
        **windows, url: {**windows.get(url, {}), model: window},
    }}


def remove_window(lines, url, model):
    windows = lines.get("context_windows")
    if not isinstance(windows, dict):
        return lines
    key = endpoint(url)
    per_url = windows.get(key)
    if not isinstance(per_url, dict) or model not in per_url:
        return lines
    next_per_url = {k: v for k, v in per_url.items() if k != model}
    next_windows = {k: v for k, v in windows.items() if k != key}
    if next_per_url:
        next_windows[key] = next_per_url
    updated = {k: v for k, v in lines.items() if k != "context_windows"}
    if next_windows:
        updated["context_windows"] = next_windows
    return updated


def window_request(request):
    name, entry, model = model_route(request)
    verified, limits, source = servers.window_details(name, entry, model)
    return {
        "url": entry["url"],
        "context_window": limits.context_window,
        "source": source,
        "automatic_window": verified.context_window,
        **limits.as_dict(),
    }


def set_window_request(request):
    if "context_window" not in request:
        raise ConnectError("A context window is a whole number of tokens.")
    raw_window = request["context_window"]
    if raw_window is not None:
        window = token_count(raw_window)
        if window is None:
            raise ConnectError("A context window is a whole number of tokens.")
    else:
        window = None
    url = request.get("url")
    if not isinstance(url, str):
        raise ConnectError("The model's API URL is required.")
    path = paths.models_file()
    with paths.changing(path):
        name, entry, model = model_route({"model": request.get("model")})
        if endpoint(entry["url"]) != endpoint(url):
            raise ConnectError(
                "The model server's URL changed. Choose the model again."
            )
        lines = own_lines(name, path)
        if window is None:
            updated = remove_window(lines, entry["url"], model)
            if updated == lines:
                return
            write_server(
                path, name, None if not updated and name in catalog() else updated,
            )
            return
        verified, _ = servers.model_limits(name, entry, model, refresh=False)
        if (verified.context_window is not None
                and window > verified.context_window):
            raise ConnectError(
                f"Context window cannot exceed {verified.context_window:,} "
                f"tokens for {model}."
            )
        write_server(path, name, save_window(lines, entry["url"], model, window))


def connect(request):
    """Test the server's model, then save it: returns (model's full name,
    the lines written). ConnectError says why not, nothing saved."""
    if not request.get("name"):
        raise ConnectError("connect needs the server's name")
    name, known, url, key = reach(request)
    model = request.get("model")
    if not isinstance(model, str) or not model_id(model.strip()):
        raise ConnectError("connect needs the model's name")
    model = model.strip().removeprefix(f"{name}/")
    context_window = request.get("context_window")
    if context_window is not None and token_count(context_window) is None:
        raise ConnectError("a context window is a whole number of tokens")
    title = request.get("title")
    _, entry, _ = model_route({"name": name, "url": url, "model": model})
    reply = servers.ModelServer(name, entry, model, refresh=False)
    try:
        listed = listing(url, key, api=reply.api)
    except Exception as error:
        if not is_not_found(error):
            raise
        # Lists nothing: the call alone proves it
        listed = []
    reported = next((row for row in listed if row["id"] == model), {})
    _, limits = servers.model_limits(name, entry, model)
    known_window = context_window or limits.context_window
    loads_window = (reported.get("context_reported_on_load")
                    and reported["context_window"] is None)
    missing_window = (f"no context window for {model}. Give the context window "
                      "in tokens.")
    if known_window is None and not loads_window:
        raise ConnectError(missing_window)
    test_call(reply.reply_url, key, model, reply.api, server=name,
              title=title or (known.title if known else None))
    if loads_window:
        try:
            listing(url, key, api=reply.api)
        except Exception as error:
            if not isinstance(error, ConnectError) and not is_not_found(error):
                raise
        _, limits = servers.model_limits(name, entry, model, refresh=False)
        known_window = context_window or limits.context_window
    if known_window is None:
        raise ConnectError(missing_window)
    path, keys = paths.models_file(), paths.keys_file()
    with paths.changing(path, keys):
        before = own_lines(name, path)
        shipped = catalog().get(name)
        previous_url = before.get("url") or (shipped["url"] if shipped else url)
        lines = (servers.without_endpoint_settings(before)
                 if endpoint(previous_url) != endpoint(url) else dict(before))
        if shipped is None:
            lines.update(title=title or lines.get("title") or name, url=url)
        elif url != shipped["url"] or "url" in lines:
            lines["url"] = url
        if context_window is not None:
            lines = save_window(lines, url, model, context_window)
        if request.get("key") is not None:
            typed = request["key"].strip()
            write_key(keys, name, typed or None, url)
        if lines != before:
            write_server(path, name, lines)
    return full_name(name, model)


def disconnect(name):
    """The server's key and table out of the user's files."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ConnectError(f"no server {name!r}")
    path, keys = paths.models_file(), paths.keys_file()
    with paths.changing(path, keys):
        write_key(keys, name, None)
        if name in servers.load(path, path):
            write_server(path, name, None)


def server_of(model):
    """The server a model name goes to, or None if it can't be told."""
    if not model:
        return None
    try:
        return servers.model_address(model, servers.table())[0]
    except (servers.ModelServerError, KeyError):
        prefix, _, rest = model.partition("/")
        return prefix if rest else servers.bare_server_name()


def not_ready_reason(model):
    """Why a session on the model couldn't start, or None: no call made,
    so a wrong key or a server that is down shows at the first turn."""
    if not model:
        return "no model is set yet"
    try:
        server, _ = servers.resolve(model, refresh=False)
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
    try:
        keys = servers.read_keys()
    except servers.ModelServerError:
        keys = {}
    listed = []
    for name, entry in table.items():
        server = servers.ModelServer(name, entry)
        has_saved_key = servers.stored_key(name, keys) is not None
        saved_for_url = servers.saved_key(server, keys) is not None
        needs_key = server.key_env is not None or (has_saved_key and not saved_for_url)
        key = "missing" if needs_key else "none needed"
        if saved_for_url:
            key = "there"
        if servers.environment_key(server):
            key = "environment"
        listed.append({
            "name": name, "title": server.title, "url": server.url,
            "api": server.api.NAME, "api_title": server.api.TITLE,
            "key_state": key, "needs_key": needs_key,
            "has_saved_key": has_saved_key,
            "model_prefix": full_name(name, ""),
            "user_added": name in mine, "in_catalog": name in shipped,
            "context_window": server.context_window,
            # Each model the table names, with the window it would take
            "models": {
                model: servers.model_limits(
                    name, entry, model, refresh=False,
                )[1].context_window
                for model in (set(entry.get("suggested_models", ()))
                              | mine.get(name, {}).get("models", {}).keys()
                              | entry.get("context_windows", {}).get(
                                  endpoint(server.url), {},
                              ).keys())
            },
        })
    return listed


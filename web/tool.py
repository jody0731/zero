"""The web tools: web_search through DeepSeek's Search API, and web_fetch,
our own GET from this machine.

Both run in the host, never in the sandbox, which has no network. The search
goes to the provider's own client, which holds the key (model/); the key
never reaches a command or a result. The fetch resolves the host first: a
local or LAN address is refused unless the call says local_network, which is
asked about first (its `crosses`), as outside_sandbox is. A proxy's fake-ip
answer (198.18.0.0/15: Clash, Mihomo and Surge answer every name from it) is
the proxy's, not the network's, so it counts as public; the request then goes
through the environment's proxy, which resolves the real name.

What the model reads states facts: the fetch's status, address, type and size,
a page's own text, and where else a refused page may be reached.
"""

import hashlib
import ipaddress
import queue
import re
import shutil
import socket
import tempfile
import threading
import time
import weakref
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx2
import openai
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from core.control import Stopped
from core.tools import Grant, tool
from model.providers import ProviderError, api_key, find

# DeepSeek's Search API (https://api-docs.deepseek.com/guides/search)
SEARCH_PATH = "/v1/tools/search"
QUERY_CHARS = 128
MOST_RESULTS = 50
# Statuses worth one more try, after RETRY_PAUSE seconds
RETRIED = {408, 429, 500, 502, 503, 504}
RETRY_PAUSE = 2.0
# What a fetch reads at most, and how many redirects it follows
MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
# The start of a refused page shown in its refusal
REFUSAL_CHARS = 300
# Statuses a site answers a fetch it refuses with
REFUSING = {401, 403, 429, 503}
ELSEWHERE = "Some sites refuse plain fetches; a skill or an MCP server may reach them."
# ZERO names itself, in the form crawlers use
USER_AGENT = "Mozilla/5.0 (compatible; ZERO; an AI agent fetching for its user)"
ACCEPT = (
    "text/markdown, text/html;q=0.9, application/xhtml+xml;q=0.9, "
    "text/plain;q=0.8, */*;q=0.1"
)
# A proxy's fake-ip block, and the shared address space carriers use
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
CGNAT = ipaddress.ip_network("100.64.0.0/10")
# Parts of a page that are no text a reader sees
NOT_TEXT = ("script", "style", "noscript", "template", "svg", "iframe", "object",
            "embed", "head")
TEXT_TYPES = ("application/json", "application/xml", "application/javascript")


def resolve_host(host):
    """The addresses this machine's resolver gives for a host."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


class Web:
    """What a session's web tools reach the network with (default_web).
    `search`: the DeepSeek client (its `post`), or None when there's no key;
    `transport`: httpx2's, for tests; `resolve`: a host's addresses."""

    def __init__(self, search=None, transport=None, resolve=resolve_host,
                 pause=time.sleep):
        self.search = search
        self.transport = transport
        self.resolve = resolve
        self.pause = pause


def default_web(limits):
    """What a session's web tools reach the network with: the fetch
    always; the search where a DeepSeek key exists, whatever the session's
    model, through DeepSeek's own client (its key stays in the client)."""
    try:
        provider = find("deepseek")
        key = api_key(provider)
    except ProviderError:
        return Web()
    # The SDK's own retries are off: the search tries once more itself
    search = openai.OpenAI(base_url=provider.url, api_key=key,
                           timeout=limits["web_timeout"], max_retries=0)
    return Web(search=search)


class Refused(Exception):
    """The fetch stops here; the message is what the model reads."""


# web_search


def search_ready(context):
    net = context.get("web")
    return net is not None and net.search is not None


def search_describe(limits):
    return (
        "Search the web through DeepSeek's Search API. Each result has its title, "
        "URL, date, site and a summary of the page. The query takes operators: "
        "site:example.org (OR between sites), -site:, \"exact phrase\", -term, "
        "lang:en, after:2025 and before:2026-06-01 (a year or a date). It runs "
        "outside the sandbox: the sandbox's commands have no network, this tool "
        "has."
    )


def search_summary(args):
    return f'web_search "{args.get("query", "")}"'


@tool(
    search_describe,
    optional=("count",),
    summary=search_summary,
    # It changes nothing on the machine
    needs_permission=lambda args: False,
    ready=search_ready,
    query=("string", "What to search for, 1 to 128 characters"),
    count=("integer", "How many results, 1 to 50"),
)
def web_search(query, count=None, limits=None, web=None, request=None, notes=None):
    """`limits`, `web`, `request` (the turn's words, sent as ori_question) and
    `notes` come from the session, never the model."""
    limits = limits or {}
    if count is None:
        count = min(limits.get("web_results") or MOST_RESULTS, MOST_RESULTS)
    whole = isinstance(count, int) and not isinstance(count, bool)
    if not whole or not 1 <= count <= MOST_RESULTS:
        return f"Not searched: count is a whole number from 1 to {MOST_RESULTS}."
    if not isinstance(query, str) or not query.strip() or len(query) > QUERY_CHARS:
        return f"Not searched: the query is 1 to {QUERY_CHARS} characters, not blank."
    if web is None or web.search is None:
        return "Not searched: there is no DeepSeek API key."
    body = {"query": query, "count": count, "need_content": False}
    question = " ".join(str(request or "").split())[:QUERY_CHARS]
    if question:
        body["ori_question"] = question
    try:
        answer = post_search(web, body)
    except openai.APIStatusError as e:
        why = "the DeepSeek balance has run out" if e.status_code == 402 else e.message
        code = e.status_code
        return f"Not searched: DeepSeek's Search API answered HTTP {code} ({why})."
    except openai.APIError as e:
        return f"Not searched: DeepSeek's Search API couldn't be reached ({e})."
    results = answer.get("results") if isinstance(answer, dict) else None
    if not isinstance(results, list):
        return "Not searched: DeepSeek's Search API answered without results."
    if notes is not None:
        notes["web"] = {"search": True, "billed": bool(results)}
    if not results:
        return f'No results for "{query}" from DeepSeek\'s Search API.'
    lines = [f'{len(results)} results for "{query}" from DeepSeek\'s Search API:']
    for n, item in enumerate(results, 1):
        lines.append("")
        lines.append(f"{n}. {item.get('title') or item.get('url')}")
        lines.append(f"   {item.get('url')}")
        when = (item.get("date") or "")[:10]
        site = words(item.get("site_name"))
        where = " · ".join(part for part in (when, site) if part)
        if where:
            lines.append(f"   {where}")
        text = item.get("summary") or item.get("snippet")
        if text:
            lines.append(f"   {' '.join(str(text).split())}")
    return "\n".join(lines)


def words(name):
    """A site's name; DeepSeek sends some as hex, read back here."""
    if not isinstance(name, str) or len(name) < 8:
        return name
    if not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", name):
        return name
    try:
        text = bytes.fromhex(name).decode("utf-8")
    except ValueError:
        return name
    return text if text.isprintable() else name


def post_search(web, body):
    """One search, and one more try after a busy or failing answer."""
    try:
        return web.search.post(SEARCH_PATH, body=body, cast_to=object)
    except openai.APIStatusError as e:
        if e.status_code not in RETRIED:
            raise
    except (openai.APIConnectionError, openai.APITimeoutError):
        pass
    web.pause(RETRY_PAUSE)
    return web.search.post(SEARCH_PATH, body=body, cast_to=object)


# web_fetch


def fetch_describe(limits):
    chars = limits.get("web_chars")
    size = f"Up to {chars:,} characters" if chars else "The start of a page"
    return (
        "Fetch a web page or file over http(s) from this machine: HTML comes back "
        f"as Markdown, text, JSON and XML as they are. {size} come back; a "
        "longer page is saved whole in this session's /tmp, at the path the "
        "result gives. A local or LAN address "
        "(this machine or its network) is fetched only with local_network, which "
        "is asked about first. It runs outside the sandbox: the sandbox's "
        "commands have no network, this tool has."
    )


def fetch_summary(args):
    return f"web_fetch {args.get('url', '')}"


# What a fetch from this machine or its network crosses, in the one sentence
# the user, the consent check and the judge all read
LOCAL = "It fetches from this machine or its local network."


def fetch_crosses(args):
    """What a fetch with local_network crosses, or None."""
    if args.get("local_network") is not True:
        return None
    return {"rule": "local_network", "fact": LOCAL, "why": args.get("reason")}


def fetch_hosts(args):
    """A local fetch's host, which "don't ask again" grants."""
    if args.get("local_network") is not True:
        return None
    host = urlsplit(str(args.get("url", ""))).hostname
    return [host] if host else None


@tool(
    fetch_describe,
    optional=("local_network", "reason"),
    # A fetch from the internet changes nothing here; one from this machine or
    # its network may reach a service that does, and asks
    needs_permission=lambda args: args.get("local_network") is True,
    summary=fetch_summary,
    crosses=fetch_crosses,
    grant=Grant("hosts", fetch_hosts,
                lambda grant: "fetches from " + ", ".join(grant["hosts"])),
    url=("string", "The http or https URL"),
    local_network=("boolean", "Fetch from a local or LAN address. It is asked about "
                   "first"),
    reason=("string", "With local_network: why the task needs it"),
)
def web_fetch(url, local_network=False, reason="", sandbox=None, limits=None, web=None,
              stop=None, notes=None):
    """`sandbox` (where a long page is saved), `limits`, `web` and `stop` come
    from the session, never the model."""
    limits = limits or {}
    web = web or Web()
    seconds = limits.get("web_timeout")
    try:
        fetched = in_thread(lambda: get(web, url, local_network is True, seconds),
                            stop, seconds)
    except Refused as e:
        return f"Not fetched: {e}"
    return render(fetched, limits.get("web_chars"), sandbox)


def in_thread(work, stop, seconds):
    """Run `work` in a helper thread; a stop returns at once (Stopped), its
    late result dropped."""
    done = queue.Queue()

    def run():
        try:
            done.put((True, work()))
        except BaseException as e:
            done.put((False, e))

    threading.Thread(target=run, daemon=True).start()
    # The fetch's own timeout fires first, at each hop; this is a backstop
    deadline = None
    if seconds is not None:
        deadline = time.monotonic() + seconds * (MAX_REDIRECTS + 1) + 5
    while True:
        if stop is not None and stop.is_set():
            raise Stopped()
        try:
            ok, value = done.get(timeout=0.1)
        except queue.Empty:
            if deadline is not None and time.monotonic() > deadline:
                raise Refused("no answer in time.") from None
            continue
        if ok:
            return value
        raise value


def get(web, url, local_network, seconds):
    """GET `url`, following redirects by hand: each hop's address is checked.
    Returns a dict of what came back."""
    first = url
    parts = check_url(url)
    # local_network covers the URL's own host, never another one
    allowed = parts.hostname if local_network else None
    with httpx2.Client(transport=web.transport, timeout=seconds,
                       follow_redirects=False, trust_env=True,
                       headers={"user-agent": USER_AGENT, "accept": ACCEPT}) as client:
        for hop in range(MAX_REDIRECTS + 1):
            parts = check_url(url)
            host = parts.hostname
            local = local_address(web, host)
            if local and host != allowed:
                if hop == 0:
                    raise Refused(
                        f"{host} is a local address ({local}: this machine or its "
                        "network); a fetch with local_network is asked about first."
                    )
                raise Refused(
                    f"{first} redirected to {url}, a local address ({local}); a "
                    "fetch of that URL with local_network is asked about first."
                )
            try:
                with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        if hop == MAX_REDIRECTS:
                            target = urljoin(url, response.headers["location"])
                            raise Refused(
                                f"more than {MAX_REDIRECTS} redirects, the last to "
                                f"{target}."
                            )
                        url = urljoin(url, response.headers["location"])
                        continue
                    body, cut = read_capped(response)
                    return {
                        "url": url, "first": first, "status": response.status_code,
                        "type": response.headers.get("content-type", ""),
                        "charset": response.charset_encoding, "body": body,
                        "cut": cut, "host": host,
                    }
            except httpx2.TimeoutException:
                raise Refused(f"{host} gave no answer within {seconds:g} s.") from None
            except httpx2.HTTPError as e:
                raise Refused(f"{host} couldn't be reached ({e}).") from None
    raise AssertionError("unreachable")


def check_url(url):
    if not isinstance(url, str):
        raise Refused("the URL is a string, http:// or https://.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused(f"{url} isn't an http:// or https:// URL.")
    if parts.username or parts.password:
        raise Refused("a URL with a user or password in it isn't fetched over http.")
    return parts


def local_address(web, host):
    """The first local address `host` stands for, or None when it's public."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return str(literal) if is_local(literal) else None
    try:
        addresses = web.resolve(host)
    except OSError as e:
        raise Refused(f"{host} doesn't resolve on this machine ({e}).") from None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        ip = ip.ipv4_mapped or ip if ip.version == 6 else ip
        # A proxy's fake-ip answer stands for a name the proxy resolves
        if ip.version == 4 and ip in FAKE_IP:
            continue
        if is_local(ip):
            return str(ip)
    return None


def is_local(ip):
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
            or (ip.version == 4 and ip in CGNAT))


def read_capped(response):
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            return b"".join(chunks)[:MAX_BYTES], True
    return b"".join(chunks), False


# What the model reads


def render(fetched, chars, sandbox):
    mime = fetched["type"].split(";")[0].strip().lower()
    body = fetched["body"]
    kind = text_kind(mime, body)
    status = fetched["status"]
    if kind is None and 200 <= status < 300:
        what = mime or "a file of no stated type"
        return f"Not fetched: {what} ({len(body):,} bytes) isn't text."
    title, text = to_text(kind or "text", body, fetched["charset"], fetched["url"])
    if not 200 <= status < 300:
        start = " ".join(text.split())
        shown = start[:REFUSAL_CHARS] + ("…" if len(start) > REFUSAL_CHARS else "")
        line = (f"Not fetched: HTTP {status} from {fetched['host']} "
                f'({len(start):,} characters: "{shown}").')
        if status in REFUSING:
            line += f" {ELSEWHERE}"
        return line
    where = fetched["url"]
    if fetched["first"] != where:
        how = f"redirected from {fetched['first']}; HTTP {status}"
    else:
        how = f"HTTP {status}"
    named = f'"{title}", ' if title else ""
    markdown = " as Markdown" if kind == "html" else ""
    head = f"Fetched {where} ({how}, {mime or 'no stated type'}): {named}"
    head += f"{len(text):,} characters{markdown}."
    if kind == "html" and not text:
        # A page its scripts build: a browser runs them, a fetch doesn't
        head += f" Its HTML holds no text outside its scripts. {ELSEWHERE}"
        return head
    if fetched["cut"]:
        head += f" The file is longer: only its first {MAX_BYTES:,} bytes were read."
    if len(text) > chars:
        shown = save_page(sandbox, where, text, extension(kind, mime))
        head += f" The first {chars:,} are below; the whole page is at {shown}."
        text = text[:chars]
    head += " The text is the page's own."
    return f"{head}\n\n{text}"


def text_kind(mime, body):
    """"html", "text", or None for a type that isn't text."""
    if mime in ("text/html", "application/xhtml+xml"):
        return "html"
    if mime.startswith("text/") or mime in TEXT_TYPES:
        return "text"
    if mime.endswith(("+json", "+xml")):
        return "text"
    if not mime and b"\0" not in body[:8192]:
        return "html" if b"<html" in body[:2048].lower() else "text"
    return None


def extension(kind, mime):
    if kind == "html":
        return ".md"
    if "json" in mime:
        return ".json"
    return ".xml" if "xml" in mime else ".txt"


def to_text(kind, body, charset, url):
    """(title, text) of a body: HTML as Markdown, its links made whole from
    `url`, its images their words; the rest decoded."""
    if kind != "html":
        return "", body.decode(charset or "utf-8", errors="replace")
    soup = BeautifulSoup(body, "html.parser", from_encoding=charset)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(NOT_TEXT):
        tag.decompose()
    for link in soup.find_all("a", href=True):
        link["href"] = urljoin(url, link["href"])
    for img in soup.find_all("img"):
        words = (img.get("alt") or "").strip()
        if words:
            img.replace_with(words)
        else:
            img.decompose()
    text = MarkdownConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return title, re.sub(r"\n{3,}", "\n\n", text).strip()


def save_page(sandbox, url, text, ext):
    """Save a long page whole where the session's commands and file tools see
    it; return the path as they see it."""
    parts = urlsplit(url)
    # The path's own extension gives way to the saved text's
    path = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", parts.path)
    stem = re.sub(r"[^A-Za-z0-9.]+", "-", f"{parts.hostname}{path}").strip("-.")[:80]
    digest = hashlib.sha256(url.encode()).hexdigest()[:6]
    name = f"{stem}-{digest}{ext}"
    if sandbox is not None and getattr(sandbox, "sandboxed", False):
        folder, shown = Path(sandbox.private_tmp()) / "web", "/tmp/web"
    else:
        folder = Path(pages_folder(sandbox))
        shown = str(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")
    return f"{shown}/{name}"


# Outside a sandbox (auto+, bypassPermissions): a folder of the session's own
# in the machine's /tmp, gone with the session
_PAGES = weakref.WeakKeyDictionary()
_LOOSE = []


def pages_folder(sandbox):
    if sandbox is None:
        if not _LOOSE:
            _LOOSE.append(tempfile.mkdtemp(prefix="zero-web-"))
        return _LOOSE[0]
    if sandbox not in _PAGES:
        folder = tempfile.mkdtemp(prefix="zero-web-")
        weakref.finalize(sandbox, shutil.rmtree, folder, True)
        _PAGES[sandbox] = folder
    return _PAGES[sandbox]

"""Run web searches and fetches through the server's network connection.

Both run in ZERO's server, never in the sandbox, which has no network. The search
goes to the server's own client, which holds the key (model/); the key
never reaches a command or a result. The fetch resolves the host first: a
local or LAN address is refused unless the call says local_network, which
goes first to the session's approver (its `crossing`), as outside_sandbox
does. A proxy's fake-ip answer (198.18.0.0/15: Clash, Mihomo and Surge answer
every name from it) is the proxy's, not the network's, so it counts as public;
the request then goes through the environment's proxy, which resolves the real
name.

What the model reads states facts: the fetch's status, address, type and size,
a page's own text, and where else a refused page may be reached.
"""

import ipaddress
import queue
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

import httpx2
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from core.asking import TO_APPROVER
from core.control import Stopped
from core.tools import Result, tool
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF
from components.model.apis.search import SearchError
from components.model.servers import ModelServerError
from components.web import SETTINGS
from components.web.search import available_services, eligible_services, select

QUERY_CHARS = 128
DEFAULT_SEARCH_COUNT = 10
# The web's limits where no session's limits say (a call made without a
# session), as the settings declare them: a fetch's size and redirects, a
# search's results
DEFAULT_LIMITS = {s.name: s.default for s in SETTINGS}
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


def limit(limits, name):
    """One of the web's limits, as the session's limits stand."""
    return (limits or {}).get(name, DEFAULT_LIMITS[name])


def resolve_host(host):
    """The addresses this machine's resolver gives for a host."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


class Web:
    """The session's live model server and the web tools' network access."""

    def __init__(self, transport=None, resolve=resolve_host, *, model_server=None):
        self.model_server = model_server
        self.transport = transport
        self.resolve = resolve
        self._session = None

    def bind_session(self, session):
        """Attach the live session so model switches and settings stay current."""
        self._session = session

    def active_model_server(self, session=None):
        """Explicit server pin if given, else live `session.model.server.name`."""
        if self.model_server is not None:
            return self.model_server
        live = session or self._session
        if live is None:
            return None
        model = getattr(live, "model", None)
        server = getattr(model, "server", None)
        return getattr(server, "name", None)


class Refused(Exception):
    """The fetch stops here; the message is what the model reads."""


# web_search


def active_search_setting(limits, web=None, session=None):
    """Authoritative `search_service`: session.settings first, else limits."""
    live = session or (web._session if web is not None else None)
    if live is not None:
        session_settings = getattr(live, "settings", None)
        if isinstance(session_settings, dict):
            for key in ("search_service", "web_search_service"):
                if key in session_settings:
                    value = session_settings[key]
                    return SERVICE_CURRENT_SERVER if value is None else value
    if limits is not None:
        for key in ("search_service", "web_search_service"):
            if key in limits:
                value = limits[key]
                return SERVICE_CURRENT_SERVER if value is None else value
    return SERVICE_CURRENT_SERVER


def search_ready(context):
    return context.get("web") is not None


SEARCH_DESCRIPTION = (
    "Search the web for source URLs and any available titles or snippets. "
    "The source URLs can be read with web_fetch. "
    "The server has network access for this tool. "
    "Provider-written summaries are labeled. The selected service receives "
    "the query and result count. The session's search_service setting "
    "controls which service runs the lookup; under current_server only the "
    "active model server or keyless fallbacks may run. A failed lookup "
    "reports the service and error."
)


def describe_search_service(limits, context):
    text = "Optional service name for this lookup. Omit to use the current choice."
    web = context.get("web")
    if not web:
        return text
    session = context.get("web_session")
    saved = active_search_setting(limits, web, session)
    if saved == SERVICE_OFF:
        return "Web search is off in this session's settings."
    try:
        services = available_services()
    except ModelServerError:
        return text
    model_server = web.active_model_server(session)
    chosen, _ = select(services, model_server, saved)
    allowed = eligible_services(services, model_server, saved)
    if chosen:
        text += f" The current choice is {chosen.name}."
    if allowed:
        text += f" Available services are {', '.join(allowed)}."
    return text


def search_summary(args):
    text = f'web_search "{args.get("query", "")}"'
    service = args.get("service")
    return f"{text} via {service}" if service else text


def search_arguments_error(query, count=None, *, limits=None):
    most = limit(limits, "web_search_max_results")
    if count is not None and (type(count) is not int or not 1 <= count <= most):
        return f"Search count must be a whole number from 1 to {most}."
    if not isinstance(query, str) or not query.strip() or len(query) > QUERY_CHARS:
        return f"Search query must contain 1 to {QUERY_CHARS} characters."
    return None


def search_count(service, count, limits):
    return min(count or DEFAULT_SEARCH_COUNT, service.api.MAX_RESULTS,
               limit(limits, "web_search_max_results"))


@tool(
    SEARCH_DESCRIPTION,
    optional=("count", "service"),
    summary=search_summary,
    ready=search_ready,
    traits=("side_by_side",),
    metadata={"permissions": {"needs_permission": lambda args: False}},
    query=("string", "What to search for, 1 to 128 characters"),
    count=("integer", lambda limits, context: (
        f"How many results to request, 1 to {limit(limits, 'web_search_max_results')}. "
        f"The default is {DEFAULT_SEARCH_COUNT}. A service may cap the count."
    )),
    service=("string", describe_search_service),
    fields={
        "service": ("string", "The search service's name"),
        "source_urls": ({"type": "array", "items": {"type": "string"}},
                        "Source URLs returned by the service"),
        "citations": (
            {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "urls": {"type": "array", "items": {"type": "string"}},
                        "start_index": {"type": "integer"},
                        "end_index": {"type": "integer"},
                    },
                    "required": ["text", "urls"],
                },
            },
            "Grounded text spans and their cited source URLs",
        ),
        "web_search_queries": (
            {"type": "array", "items": {"type": "string"}},
            "Search queries executed by the provider for grounding",
        ),
        "search_suggestions_html": (
            "string",
            "Provider search suggestion HTML for sandboxed display",
        ),
    },
)
def web_search(query, count=None, service=None, web=None, notes=None, limits=None,
               stop=None, web_session=None):
    """Send a lookup to the service selected by the session's search_service."""
    error = search_arguments_error(query, count, limits=limits)
    if error:
        return Result(error, failed=True)
    if web is None:
        return Result("Search is unavailable in this session.", failed=True)
    notes = notes if notes is not None else {}
    route = notes.setdefault("web", {})
    saved = active_search_setting(limits, web, web_session)
    model_server = web.active_model_server(web_session)
    try:
        services = available_services()
    except ModelServerError:
        return search_failed(
            service or (None if saved in (SERVICE_CURRENT_SERVER, SERVICE_OFF)
                        else saved),
            "The search service configuration could not be read.",
        )
    chosen, reason = select(services, model_server, saved, service)
    if chosen is None:
        reported_service = (
            service if service is not None
            else (None if saved in (SERVICE_CURRENT_SERVER, SERVICE_OFF) else saved)
        )
        return search_failed(reported_service, reason)
    count = search_count(chosen, count, limits)

    seconds = limit(limits, "web_timeout")
    route.update(search=True, service=chosen.name, billed=None)

    def check():
        if stop is not None and stop.is_set():
            raise Stopped()

    try:
        check()
        answer = run_in_thread(
            lambda: chosen.api.search(chosen, query, count, seconds, web.transport,
                                      check),
            stop, seconds, 0,
        )
    except (SearchError, Refused) as error:
        return search_failed(chosen.name, str(error))
    route["billed"] = answer.billed
    return render_search(chosen, answer)


def search_failed(service, reason):
    message = f"Search with {service} failed. {reason}" if service else reason
    fields = {"service": service, "source_urls": []} if service else {}
    return Result(message, fields=fields, failed=True)


def render_search(service, answer):
    def redact(text):
        return text.replace(service.key, "[redacted]") if service.key else text

    sources = [source for source in answer.sources if redact(source.url) == source.url]
    fields = {"service": service.name, "source_urls": [item.url for item in sources]}
    citations = []
    for span in getattr(answer, "citations", ()) or ():
        urls = [url for url in span.urls if redact(url) == url]
        if not urls:
            continue
        row = {"text": redact(span.text), "urls": urls}
        if span.start_index is not None:
            row["start_index"] = span.start_index
        if span.end_index is not None:
            row["end_index"] = span.end_index
        citations.append(row)
    if citations:
        fields["citations"] = citations
    suggestions = getattr(answer, "suggestions", None)
    queries = []
    if suggestions is not None:
        queries = [
            redact(q) for q in suggestions.web_search_queries
            if isinstance(q, str) and q.strip()
        ]
        if queries:
            fields["web_search_queries"] = queries
        if suggestions.rendered_content:
            fields["search_suggestions_html"] = redact(suggestions.rendered_content)
    if not sources:
        if answer.summary:
            lines = [
                f"Summary written by {service.title}",
                "",
                answer.summary,
            ]
            if queries:
                lines.extend(("", f"Search queries: {', '.join(queries)}"))
            return Result(redact("\n".join(lines)), fields=fields)
        return Result(f"No results from {service.label}.", fields=fields)
    lines = [f"{len(sources)} results from {service.label}."]
    if answer.summary:
        lines.extend(("", f"Summary written by {service.title}", "", answer.summary))
    for index, source in enumerate(sources, 1):
        lines.extend(("", f"{index}. {source.title or source.url}", f"   {source.url}"))
        where = " · ".join(value for value in (source.date, source.site) if value)
        if where:
            lines.append(f"   {where}")
        if source.summary:
            lines.extend(("   Provider summary",
                          f"   {' '.join(source.summary.split())}"))
        elif source.snippet:
            lines.append(f"   {' '.join(source.snippet.split())}")
    if queries:
        lines.extend(("", f"Search queries: {', '.join(queries)}"))
    return Result(redact("\n".join(lines)), fields=fields)


# web_fetch


FETCH_DESCRIPTION = (
    "Fetch a web page or file over http(s) from this machine: HTML comes back "
    "as Markdown, text, JSON and XML as they are, whole within its share of "
    "the context's room; past it, its start and end come with where the whole "
    "is saved. A local or LAN address (this machine or its network) is fetched "
    f"only with local_network, which goes first to {TO_APPROVER}. It runs "
    "outside the sandbox: the sandbox's commands have no network, this tool has. "
    "Reading the internet with this tool needs no review."
)


def fetch_summary(args):
    return f"web_fetch {args.get('url', '')}"


# What a fetch from this machine or its network crosses, in the one sentence
# the user, the consent check and the judge all read
LOCAL = "It fetches from this machine or its local network."


def fetch_crossing(args):
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
    FETCH_DESCRIPTION,
    optional=("local_network", "reason"),
    summary=fetch_summary,
    # From the internet: beside other such calls (a local one asks, alone)
    traits=("side_by_side",),
    metadata={
        "permissions": {
            # A fetch from the internet changes nothing here; one from this
            # machine or its network may reach a service that does, and asks
            "needs_permission": lambda args: args.get("local_network") is True,
            "crossing": fetch_crossing,
            "grant": {
                "field": "hosts",
                "of": fetch_hosts,
                "words": lambda grant: "fetches from " + ", ".join(grant["hosts"]),
            },
        },
    },
    url=("string", "The http or https URL"),
    local_network=("boolean", "Fetch from a local or LAN address. It goes first "
                   f"to {TO_APPROVER}"),
    reason=("string", "With local_network: why the task needs it"),
)
def web_fetch(url, local_network=False, reason="", limits=None, web=None, stop=None,
              notes=None):
    """`limits` (its web_timeout, the most it reads and the redirects it
    follows, as they stand), `web` and `stop` come from the session, never
    the model."""
    limits = limits or {}
    web = web or Web()
    seconds = limits.get("web_timeout")
    most_bytes = limit(limits, "web_fetch_max_megabytes") << 20
    redirects = limit(limits, "web_fetch_max_redirects")
    try:
        fetched = run_in_thread(
            lambda: get(web, url, local_network is True, seconds, most_bytes,
                        redirects),
            stop, seconds, redirects)
    except Refused as e:
        return f"Not fetched: {e}"
    return render(fetched)


def run_in_thread(work, stop, seconds, redirects):
    """Run `work` in a helper thread; a stop returns at once (Stopped), its
    late result dropped. `seconds` a hop, `redirects` hops after the first,
    set its backstop."""
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
        deadline = time.monotonic() + seconds * (redirects + 1) + 5
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


def get(web, url, local_network, seconds, most_bytes, redirects):
    """GET `url`, following `redirects` redirects at most by hand, each hop's
    address checked, and reading `most_bytes` at most. Returns a dict of
    what came back."""
    first = url
    parts = check_url(url)
    # local_network covers the URL's own host, never another one
    allowed = parts.hostname if local_network else None
    with httpx2.Client(transport=web.transport, timeout=seconds,
                       follow_redirects=False, trust_env=True,
                       headers={"user-agent": USER_AGENT, "accept": ACCEPT}) as client:
        for hop in range(redirects + 1):
            parts = check_url(url)
            host = parts.hostname
            local = local_address(web, host)
            if local and host != allowed:
                if hop == 0:
                    raise Refused(
                        f"{host} is a local address ({local}: this machine or its "
                        "network); a fetch with local_network goes first to "
                        f"{TO_APPROVER}."
                    )
                raise Refused(
                    f"{first} redirected to {url}, a local address ({local}); a "
                    "fetch of that URL with local_network goes first to "
                    f"{TO_APPROVER}."
                )
            try:
                with client.stream("GET", url) as response:
                    if response.is_redirect and response.headers.get("location"):
                        if hop == redirects:
                            target = urljoin(url, response.headers["location"])
                            raise Refused(
                                f"more than {redirects} redirects, the last to "
                                f"{target}."
                            )
                        url = urljoin(url, response.headers["location"])
                        continue
                    body, cut = read_capped(response, most_bytes)
                    return {
                        "url": url, "first": first, "status": response.status_code,
                        "type": response.headers.get("content-type", ""),
                        "charset": response.charset_encoding, "body": body,
                        "cut": cut, "most_bytes": most_bytes, "host": host,
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


def read_capped(response, most_bytes):
    """The body, `most_bytes` at most, and whether it was cut there."""
    chunks, size = [], 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > most_bytes:
            return b"".join(chunks)[:most_bytes], True
    return b"".join(chunks), False


# What the model reads


def render(fetched):
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
        head += (f" The file is longer: only its first {fetched['most_bytes']:,} "
                 "bytes were read.")
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
        site_name = (img.get("alt") or "").strip()
        if site_name:
            img.replace_with(site_name)
        else:
            img.decompose()
    text = MarkdownConverter(heading_style="ATX", bullets="-").convert_soup(soup)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return title, re.sub(r"\n{3,}", "\n\n", text).strip()


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (web_search, web_fetch)

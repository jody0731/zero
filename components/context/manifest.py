"""Component manifest for context."""

from components.context import SETTINGS, room
from components.context.entries import ENTRY_SPECS
from components.context.hooks import Context
from components.context.results import folder_of
from components.context.service import ContextService
from components.context.size_note import SizeNote
from core.components import ComponentManifest, ServiceBuild, SessionBuild


def prepare_session(build: SessionBuild):
    def results():
        # A new log is named when its first turn starts.
        return folder_of(build.log.path)

    # A fork's copied context names the outputs its parents saved: their
    # folders are shown too, read-only, at their own paths.
    head = build.log.first_record()
    fork = head.entry.get("forked_from") if head is not None else None
    shown = [
        results,
        *(
            folder_of(build.log.path.parent / f"{ancestor}.jsonl")
            for ancestor in (fork or {}).get("ancestors", ())
        ),
    ]
    if (add_shown := build.state.get("add_sandbox_shown")) is not None:
        for item in shown:
            add_shown(item)
    else:
        build.state.setdefault("sandbox_shown", []).extend(shown)

    def most_chars():
        # ZERO's own files share what a result ZERO didn't size may take of a
        # fresh context, under the limits as they stand.
        at = room.line(build.limits, build.server.context_window)
        return room.share(at, build.limits) * room.BYTES_PER_TOKEN

    def line_chars():
        return (
            room.line(build.limits, build.server.context_window)
            * room.BYTES_PER_TOKEN
        )

    build.state["results"] = results
    build.state["most_chars"] = most_chars
    build.state["line_chars"] = line_chars
    build.state["bytes_per_token"] = room.BYTES_PER_TOKEN
    build.tool_context["results"] = results
    # The share of the context ZERO's own files take, as it stands.
    build.tool_context["own_files_percent"] = (
        lambda: build.limits["result_share_percent"]
    )
    # The session's line in characters: the room found tools' schemas share.
    build.tool_context["line_chars"] = line_chars


def session_hooks(build: SessionBuild):
    return [
        Context(
            build.server.context_window,
            build.state.get("results"),
            build.limits,
        ),
        SizeNote(build.limits),
    ]


def service(build: ServiceBuild):
    return ContextService()


MANIFEST = ComponentManifest(
    name="context",
    order=20,
    entries=ENTRY_SPECS,
    settings=SETTINGS,
    prepare_session=prepare_session,
    session_hooks=session_hooks,
    service=service,
)

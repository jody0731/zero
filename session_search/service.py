"""Session search's service in the server (server/services.py): the index,
each session's search, and the `session/search` message.

The index is the server's, one for its home, in a file of its own beside
the server's database (`INDEX_FILE`), so the database is never touched; the
session list it searches is the server's own. `session/search` only looks,
so ZERO's own commands may send it too (server/peers.py).
"""

from server.services import Refused, Service
from session_search.index import Index, Unreadable
from session_search.search import SearchError, find

INDEX_FILE = "session_search.sqlite"


class SessionSearch:
    """What one session's tool gets as `search`: the server's search, the
    session that asks marked in its replies."""

    def __init__(self, service, sid):
        self.service = service
        self.sid = sid

    def find(self, **request):
        return self.service.find(asking=self.sid, **request)


class SessionSearchService(Service):
    name = "session_search"
    looking_messages = ("session/search",)

    def attach(self, runtime):
        super().attach(runtime)
        self.index = Index(runtime.paths.state / INDEX_FILE)

    def find(self, query=None, session=None, around=None, count=None,
             asking=None):
        return find(self.index, self.runtime.store.sessions(),
                    self.runtime.paths.sessions, query=query, session=session,
                    around=around, count=count, asking=asking)

    def messages(self):
        return {"session/search": self.search_message}

    def search_message(self, request):
        """The user's or another agent's search: the tool's three uses."""
        fields = {name: request.get(name)
                  for name in ("query", "session", "around", "count")}
        try:
            return {"ok": True, **self.find(**fields)}
        except SearchError as error:
            raise Refused(str(error)) from None
        except Unreadable as error:
            raise Refused(f"FTS5 couldn't read the query ({error})") from None

    def session_built(self, sid, session):
        session.tool_context["search"] = SessionSearch(self, sid)

    def shutdown(self):
        self.index.close()

"""The APIs ZERO speaks to model servers, a file each. A server can pin
its API in models.toml. Otherwise ZERO prefers Responses and uses Chat
Completions where the endpoint or model requires it.

Each API adapter defines `NAME`, `TITLE`, `FEATURES` (what ZERO may ask of a
model through the API's standard fields; what a model honours of them is
measured, features.py), `MIN_OUTPUT_TOKENS` (the fewest output tokens a
request may ask for), `request_options(server, *, encrypted=False,
zero_temperature=False)`, `keeps_continuation(server)`, `open_stream(client,
model, conversation, **request)` (one streamed request from ZERO's entries
and the request's standard fields: tools, effort, json_schema, json_mode,
temperature, max_output_tokens), `fold(draft, event)` (one event into the
reply's draft, core/llm.py), and optional `alive(event)`. ZERO sends each
API's standard fields as they are and reads replies by its contract, whatever
the server: what a server doesn't honour is the server's, measured and said,
never patched.
"""

from components.model.apis import (
    chat_completions,
    deepseek_search,
    moonshot_search,
    responses,
    tavily_search,
)

APIS = {api.NAME: api for api in (chat_completions, responses)}
DEFAULT = "responses"

# The model server's search capability names the wire contract it implements.
SEARCH_APIS = {
    api.NAME: api
    for api in (deepseek_search, responses.ResponsesSearch, moonshot_search)
}
KEYLESS_SEARCH_APIS = (tavily_search,)

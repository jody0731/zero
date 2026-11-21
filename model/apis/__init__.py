"""The APIs ZERO speaks to model servers, a file each. A server can pin
its API in models.toml. Otherwise ZERO prefers Responses and uses Chat
Completions where the endpoint or model requires it.

Each API's file has `open_stream` (one streamed request, from ZERO's
messages and the request's standard fields: tools, effort, json_schema,
json_mode, temperature, max_output_tokens), `fold` (one event of that
stream into the reply's draft, core/llm.py), FEATURES (what ZERO may ask
of a model through the API's standard fields; what a model honours of them
is measured, features.py) and MIN_OUTPUT_TOKENS (the fewest output tokens a
request may ask for). ZERO sends each API's standard fields as they are and
reads replies by its contract, whatever the server: what a server doesn't
honour is the server's, measured and said, never patched.
"""

from model.apis import (
    chat_completions,
    deepseek_search,
    moonshot_search,
    responses,
    tavily_search,
)

APIS = {"chat_completions": chat_completions, "responses": responses}
DEFAULT = "responses"

# The model server's search capability names the wire contract it implements.
SEARCH_APIS = {
    api.NAME: api
    for api in (deepseek_search, responses.ResponsesSearch, moonshot_search)
}
KEYLESS_SEARCH_APIS = (tavily_search,)

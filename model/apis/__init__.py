"""The APIs ZERO speaks to model servers, a file each. A server's entry
names the one it speaks with its line `api` (catalog.toml, the user's
models.toml); the Chat Completions API when it names none.

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

from model.apis import chat_completions, responses

APIS = {"chat_completions": chat_completions, "responses": responses}
# What a server's entry speaks when it names no API
DEFAULT = "chat_completions"

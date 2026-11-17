"""One entry point for labels, private value requests and discovery."""

import json

from core.tools import tool
from vault.store import VaultError, check_labels

NAME = "vault"


def problems(args):
    action = args.get("action")
    if action not in ("find", "add", "update", "delete"):
        return "action is find, add, update or delete"
    if action == "find" and not str(args.get("query", "")).strip():
        return "find needs a query describing the site or credential"
    if action == "delete" and not str(args.get("name", "")).strip():
        return "delete needs the entry name"
    if action in ("add", "update"):
        try:
            check_labels(args.get("labels"))
        except VaultError as error:
            return str(error)


def describe(limits, context=None):
    service = (context or {}).get("vault")
    count = len(service.labels()) if service is not None else 0
    return (
        f"Encrypted vault: {count} entries. Find credentials by site or words; "
        "matching returns labels and part names, without values. Add and update "
        "take labels; the user supplies each value in a separate masked form, "
        "outside the conversation. Delete removes the named entry. "
        "In any tool argument, ⟦vault:name/part⟧ is replaced with that value "
        "immediately before execution; ⟦vault:name⟧ works for a one-part entry. "
        "Returned values appear as stand-ins in results and logs."
    )


LABELS = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "kind", "description", "sites", "username", "parts"],
    "properties": {
        "name": {"type": "string"},
        "kind": {"type": "string", "enum":
                 ["login", "key", "card", "identity", "note"]},
        "description": {"type": "string"},
        "sites": {"type": "array", "items": {"type": "string"}},
        "username": {"type": "string"},
        "parts": {"type": "array", "items": {"type": "string"}},
    },
}


@tool(
    describe, optional=("query", "name", "labels"), validate=problems,
    ready=lambda context: context.get("vault") is not None,
    needs_permission=lambda args: args.get("action") == "delete",
    crossing=lambda args: ({"rule": "vault_deletion",
                            "fact": "delete a saved vault entry", "why": ""}
                           if args.get("action") == "delete" else None),
    action=("string", "find, add, update or delete"),
    query=("string", "For find: site or words describing the credential"),
    name=("string", "For delete: the exact entry name"),
    labels=(LABELS, "For add or update: metadata and part names, without values"),
)
def vault(action, query="", name="", labels=None, vault=None, vault_find=None,
          notes=None):
    if action == "find":
        return json.dumps(vault_find(query, notes), ensure_ascii=False)
    if action == "delete":
        vault.delete(name)
        return f"Deleted vault entry {name}."
    return "The values wait for the user's masked input."


TOOLS = (vault,)

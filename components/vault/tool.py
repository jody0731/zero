"""One entry point for labels, private value requests and discovery."""

import json

from core.tools import tool
from components.vault.store import VaultError, check_labels

NAME = "vault"

# These receipts reach the next model step without any private value.
SAVED = (
    "Input supplied. The requested operation can proceed. The saved entry "
    "remains available after use. Removal is a separate user request."
)
CANCELLED = (
    "Input cancelled. The user has closed this request for the value in every "
    "channel. They already saw what was needed and chose Cancel. The only new "
    "information for them is which work could not be completed."
)


def problems(args):
    action = args.get("action")
    if action not in ("find", "add", "update", "delete"):
        return "action is find, add, update or delete"
    if action == "find" and not str(args.get("query", "")).strip():
        return "find needs a query describing the private information"
    if action == "delete" and not str(args.get("name", "")).strip():
        return "delete needs the entry name"
    if action in ("add", "update"):
        try:
            check_labels(args.get("labels"))
        except VaultError as error:
            return str(error)
        form = args.get("input_form")
        if not isinstance(form, dict):
            return "add/update needs input_form with title, instructions and fields"
        for name in ("title", "instructions"):
            if not isinstance(form.get(name), str) or not form[name].strip():
                return f"input_form needs nonempty {name}"
        fields = form.get("fields")
        if not isinstance(fields, list):
            return "input_form fields must describe each required part"
        parts = []
        for field in fields:
            if not isinstance(field, dict) or any(
                not isinstance(field.get(name), str) or not field[name].strip()
                for name in ("part", "label", "instructions")
            ):
                return "each input_form field needs part, label and instructions"
            parts.append(field["part"])
        if sorted(parts) != sorted(args["labels"]["parts"]):
            return "input_form fields must describe every part exactly once"


def describe(limits, context=None):
    service = (context or {}).get("vault")
    count = len(service.labels()) if service is not None else 0
    return (
        f"Private information store: {count} entries. Find returns labels and "
        "part names. Add asks the user to enter missing credentials in a masked "
        "form. Update opens the form for an existing entry. "
        "Save keeps the entry for later use and resumes the task with a receipt. "
        "The private input stays outside the conversation. In any tool argument, "
        "⟦vault:name/part⟧ supplies a saved value at execution. "
        "Use ⟦vault:name⟧ for a one-part entry. Returned values appear as "
        "stand-ins in results and logs."
    )


LABELS = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "kind", "description", "sites", "username", "parts"],
    "properties": {
        "name": {"type": "string", "description":
                 "1-80 letters, digits, dots, dashes or underscores; "
                 "starts with a letter or digit"},
        "kind": {"type": "string", "enum":
                 ["login", "key", "card", "identity", "note"]},
        "description": {"type": "string"},
        "sites": {"type": "array", "items": {"type": "string"},
                  "description": "Associated sites; [] when none apply"},
        "username": {"type": "string", "description":
                     "Account username, or empty when unknown or not needed"},
        "parts": {"type": "array", "items": {"type": "string"},
                  "description": "Required secret fields, each named with the "
                                 "same characters and length as an entry name"},
    },
}

INPUT_FORM = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "instructions", "fields"],
    "properties": {
        "title": {"type": "string", "description": "The user's task"},
        "instructions": {"type": "string", "description":
                         "Why the task needs the private input. The app supplies "
                         "privacy and retention text."},
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["part", "label", "instructions"],
            "properties": {
                "part": {"type": "string", "description":
                         "One of labels.parts, each included exactly once"},
                "label": {"type": "string", "description":
                          "Readable name of the value the user enters"},
                "instructions": {"type": "string", "description":
                                 "What to enter, with practical steps for finding "
                                 "the value when helpful."},
            },
        }},
    },
}


@tool(
    describe, optional=("query", "name", "labels", "input_form"),
    validate=problems,
    ready=lambda context: context.get("vault") is not None,
    metadata={
        "permissions": {
            "needs_permission": lambda args: args.get("action") == "delete",
            "crossing": lambda args: (
                {"rule": "vault_deletion",
                 "fact": "delete a saved vault entry", "why": ""}
                if args.get("action") == "delete" else None
            ),
        },
    },
    action=("string", "find, add, update or delete"),
    query=("string", "For find: words describing the private information"),
    name=("string", "For delete: the exact entry name"),
    labels=(LABELS, "For add or update: metadata and part names, without values"),
    input_form=(INPUT_FORM, "For add or update: the user's masked form"),
)
def vault(action, query="", name="", labels=None, input_form=None, vault=None,
          vault_find=None, notes=None):
    if action == "find":
        return json.dumps(vault_find(query, notes), ensure_ascii=False)
    if action == "delete":
        vault.delete(name)
        return f"Deleted vault entry {name}."
    return "The values wait for the user's masked input."


TOOLS = (vault,)

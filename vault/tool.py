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
        f"Encrypted vault: {count} entries. Passwords, API keys and session "
        "cookies can authenticate tools as the user's account without exposing "
        "their values to you. Find returns matching labels and part names; "
        "a saved match is usable without another input form. Added entries "
        "remain available for later tasks until deleted. "
        "Find locates existing credentials for the requested access; missing "
        "credentials are collected by add. An explanation that credentials "
        "are needed does not open a form or obtain them. "
        "Calling add or update with labels opens the user's masked input form "
        "and pauses this turn. You fill the labels; the user enters only the "
        "values in that form, outside the conversation. Every listed part "
        "requires a nonempty value before saving. Saving resumes your "
        "turn, so you can use the entry for the task. The form is the private "
        "handoff: the user can supply the value or cancel there, without a "
        "separate preliminary question. Cancellation leaves this attempt "
        "without the requested value; it does not authorize obtaining that "
        "value another way. "
        "For add/update, input_form supplies the task-specific title, why "
        "the information is needed, and each field's readable label and "
        "instructions for finding and copying the exact value. These appear "
        "beside masked fields and address a user who does not know your tools. "
        "The app supplies the storage, retention and privacy explanation. "
        "Those facts are application-owned; input_form describes the task "
        "and finding/copying steps, not storage guarantees. A task-specific "
        "purpose does not make a saved entry single-use or automatically "
        "delete it. "
        "The user needs the value's location and format, "
        "not knowledge of this tool or of HTTP header construction. "
        "An authorization failure alone does not identify the credential "
        "type or cookie name. Asking for a whole Cookie header still assumes "
        "cookie authentication; it does not resolve an unknown method. A "
        "missing method calls for site documentation or a focused factual "
        "question about how the user signs in, not a guessed private field. "
        "Delete removes the named entry. "
        "In any tool argument, ⟦vault:name/part⟧ is replaced with that value "
        "immediately before execution; ⟦vault:name⟧ works for a one-part entry. "
        "A shell HTTP client's cookie or authorization header can contain a "
        "stand-in. "
        "Returned values appear as stand-ins in results and logs."
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
        "sites": {"type": "array", "items": {"type": "string"}},
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
                         "Why the task needs this information; the app "
                         "separately explains storage and privacy"},
        "fields": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["part", "label", "instructions"],
            "properties": {
                "part": {"type": "string", "description":
                         "One of labels.parts, each included exactly once"},
                "label": {"type": "string", "description":
                          "Readable name of the value the user enters"},
                "instructions": {"type": "string", "description":
                                 "Where to find this value and exactly what "
                                 "to copy, suited to the user's application. "
                                 "The complete value includes any punctuation; "
                                 "entry-name restrictions do not apply to it."},
            },
        }},
    },
}


@tool(
    describe, optional=("query", "name", "labels", "input_form"),
    validate=problems,
    ready=lambda context: context.get("vault") is not None,
    needs_permission=lambda args: args.get("action") == "delete",
    crossing=lambda args: ({"rule": "vault_deletion",
                            "fact": "delete a saved vault entry", "why": ""}
                           if args.get("action") == "delete" else None),
    action=("string", "find, add, update or delete"),
    query=("string", "For find: site or words describing the credential"),
    name=("string", "For delete: the exact entry name"),
    labels=(LABELS, "For add or update: metadata and part names, without values"),
    input_form=(INPUT_FORM, "Required for add/update: the user's input form"),
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

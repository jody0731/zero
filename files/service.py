"""Workspace folders for a person's picker, without reading file contents.

Not a looking message: ZERO's own commands cannot use this user-facing
operation to list folders outside their sandbox.
"""

import heapq
import os
from pathlib import Path

from files.seen import WRITTEN_THERE, written
from server.services import Refused, Service

PAGE_FOLDERS = 200


def name_order(name):
    return name.casefold(), name


class FilesService(Service):
    name = "files"

    def fork_effects(self, sid, left):
        paths = written(left)
        return WRITTEN_THERE.format(paths=", ".join(paths)) if paths else ""

    def messages(self):
        return {"workspace/list": self.list_message}

    def list_message(self, request):
        given = request.get("path")
        after = request.get("after", "")
        if given is not None and not isinstance(given, str):
            raise Refused("path must be an absolute folder path")
        if not isinstance(after, str):
            raise Refused("after must be a folder name")
        path = Path(given) if given is not None else Path.home()
        if not path.is_absolute():
            raise Refused("path must be an absolute folder path")
        try:
            path = path.resolve(strict=True)
            with os.scandir(path) as entries:
                names = heapq.nsmallest(PAGE_FOLDERS + 1,
                    (entry.name for entry in entries if entry.is_dir()
                     and name_order(entry.name) > name_order(after)), key=name_order)
        except (OSError, RuntimeError, ValueError) as error:
            raise Refused(f"This folder cannot be opened: {error}") from error
        more = len(names) > PAGE_FOLDERS
        names = names[:PAGE_FOLDERS]
        return {"ok": True, "path": str(path),
                "parent": str(path.parent) if path.parent != path else None,
                "folders": [{"name": name, "path": str(path / name)}
                            for name in names],
                "next_after": names[-1] if more else None}

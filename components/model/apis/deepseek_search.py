"""DeepSeek's direct Search API."""

import re

from components.model.apis.search import (
    SearchResponse,
    Source,
    bearer,
    post_json,
    result_rows,
    source_value,
)

NAME = "deepseek_search"
MAX_RESULTS = 50


def endpoint(base_url):
    return base_url.rstrip("/").removesuffix("/v1") + "/v1/tools/search"


def site_name(name):
    """The API sometimes encodes site names as hex."""
    if len(name) < 8 or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", name):
        return name
    try:
        text = bytes.fromhex(name).decode("utf-8")
    except ValueError:
        return name
    return text if text.isprintable() else name


def search(service, query, count, seconds, transport, check):
    body = {"query": query, "count": count}
    answer = post_json(
        service.url, body, bearer(service.key), seconds, transport, check,
        key=service.key,
    )
    sources = [
        Source(
            row["url"], source_value(row, "title"), source_value(row, "snippet"),
            source_value(row, "date")[:10], site_name(source_value(row, "site_name")),
            source_value(row, "summary"),
        )
        for row in result_rows(answer, "results")[:count]
    ]
    return SearchResponse(sources, billed=bool(sources))

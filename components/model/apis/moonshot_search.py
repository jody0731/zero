"""Moonshot's Search Basic API."""

from components.model.apis.search import (
    SearchResponse,
    Source,
    bearer,
    post_json,
    result_rows,
    source_value,
)

NAME = "moonshot_search"
MAX_RESULTS = 20


def endpoint(base_url):
    return base_url.rstrip("/").removesuffix("/v1") + "/v1/tools/search"


def search(service, query, count, seconds, transport, check):
    body = {"text_query": query, "limit": count}
    answer = post_json(
        service.url, body, bearer(service.key), seconds, transport, check,
    )
    sources = [
        Source(
            row["url"], source_value(row, "title"), source_value(row, "snippet"),
            source_value(row, "date")[:10], source_value(row, "site_name"),
        )
        for row in result_rows(answer, "search_results")[:count]
    ]
    return SearchResponse(sources)

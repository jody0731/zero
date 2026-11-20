"""Tavily's directly available keyless Search API."""

from model.apis.search import (
    SearchResponse,
    Source,
    post_json,
    result_rows,
    source_value,
)

NAME = "tavily"
TITLE = "Tavily"
URL = "https://api.tavily.com/search"
MAX_RESULTS = 20


def search(service, query, count, seconds, transport, check):
    headers = {"X-Tavily-Access-Mode": "keyless", "X-Client-Name": "ZERO"}
    answer = post_json(
        service.url, {"query": query, "max_results": count}, headers,
        seconds, transport, check,
    )
    sources = [
        Source(
            row["url"], source_value(row, "title"), source_value(row, "content"),
            source_value(row, "published_date"),
        )
        for row in result_rows(answer, "results")[:count]
    ]
    return SearchResponse(sources, source_value(answer, "answer"), billed=False)

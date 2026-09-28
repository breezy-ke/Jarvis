"""Research's plain-code parts: citations that must be real, and search results that must be sane."""

from __future__ import annotations

import json

import httpx
import pytest

from jarvis.research.researcher import UNCITED, Answer, Source, keep_real_citations
from jarvis.research.searxng import SearchError, SearXNG


def test_only_citations_to_real_sources_survive() -> None:
    text, cited = keep_real_citations("A [1], B [2, 3], C [7] and D [3;1].", {1, 2, 3})
    assert text == "A [1], B [2][3], C and D [3][1]."
    assert cited == [1, 2, 3]
    assert keep_real_citations("No sources at all [0] [99].", {1}) == ("No sources at all .", [])
    lines, _ = keep_real_citations("- one [1]\n- two [5]\n\nEnd [2]", {1, 2})
    assert lines == "- one [1]\n- two\n\nEnd [2]"


def test_an_answer_lists_its_sources_with_their_links() -> None:
    read = Source(1, "A guide", "https://a.test/guide", "text", read=True)
    snippet = Source(2, "A snippet", "https://b.test/x", "snip", read=False)
    answer = Answer("q", "It works [1][2].", sources=(read, snippet), consulted=(read, snippet))
    assert answer.render() == (
        "It works [1][2].\n\nSources:\n[1] A guide — https://a.test/guide\n"
        "[2] A snippet — https://b.test/x (search result only)"
    )
    uncited = Answer("q", f"Maybe.\n\n{UNCITED}", sources=(read,), consulted=(read,))
    assert "Pages Jarvis read:" in uncited.render()
    assert Answer("q", "Nothing found.").render() == "Nothing found."
    assert read.host == "a.test"


async def search_with(body: object, status: int = 200) -> list[str]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json"
        return httpx.Response(status, content=json.dumps(body).encode())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    results = await SearXNG("http://searxng:8080", http=http).search("q")
    return [r.url for r in results]


async def test_strange_search_answers_are_handled() -> None:
    assert await search_with({"results": "not a list"}) == []
    assert await search_with(["not", "an", "object"]) == []
    assert await search_with(
        {
            "results": [
                "junk",
                {"url": "https://ok.test/a", "title": "<b>Fine</b>"},
                {"url": "https://ok.test/b", "title": ""},  # no title: skipped
                {"url": "ftp://files.test/x", "title": "FTP"},
                {"url": "https://OK.test/a#top", "title": "The same, again"},
            ]
        }
    ) == ["https://ok.test/a"]
    with pytest.raises(SearchError, match="answered 403"):
        await search_with({}, status=403)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    down = SearXNG(
        "http://searxng:8080", http=httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(SearchError, match="didn't answer"):
        await down.search("q")
    assert await down.reachable() == "the search engine on your PC didn't answer (ConnectError)"

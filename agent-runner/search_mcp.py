"""Web search as an MCP server (Tavily behind one tool).

Runs as its own container in the pod, from the agent image, and listens on the
pod's loopback only. It is the only process that holds the Tavily key and the
only one that talks to the search service; the agent runner reaches it over
MCP (streamable HTTP) and gets one tool:

    web_search(query, max_results=5) -> {"results": [{"title", "url", "content"}]}

Nothing but the query leaves the installation. Queries are not logged.
"""

from __future__ import annotations

import os

import httpx
from fastmcp import FastMCP

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
TAVILY_URL = os.environ.get("TAVILY_URL", "https://api.tavily.com/search")
HOST = os.environ.get("SEARCH_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("SEARCH_MCP_PORT", "8012"))
MAX_RESULTS = 8
MAX_QUERY = 400
MAX_CONTENT = 1500

mcp = FastMCP(
    "web-search",
    instructions="Search the public web. Use short, specific queries.",
)


def _results(body: object) -> list[dict[str, str]]:
    """Title, address and text of each hit; anything else is dropped."""
    items = body.get("results") if isinstance(body, dict) else None
    results: list[dict[str, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": " ".join(str(item.get("title") or "").split())[:200] or url,
                "url": url,
                "content": str(item.get("content") or "").strip()[:MAX_CONTENT],
            }
        )
    return results


@mcp.tool
async def web_search(query: str, max_results: int = 5) -> dict:
    """Search the public web and return the best pages.

    Args:
        query: What to search for, as a short and specific query.
        max_results: How many pages to return (1 to 8).
    """
    query = " ".join(query.split())[:MAX_QUERY]
    if not query:
        return {"results": [], "error": "The query is empty."}
    if not TAVILY_API_KEY:
        return {"results": [], "error": "Web search is not configured."}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
                json={
                    "query": query,
                    "max_results": max(1, min(MAX_RESULTS, max_results)),
                    "search_depth": "basic",
                },
            )
    except httpx.HTTPError as exc:
        return {"results": [], "error": f"The search service could not be reached ({type(exc).__name__})."}
    if response.status_code != 200:
        return {"results": [], "error": f"The search service answered HTTP {response.status_code}."}
    try:
        body = response.json()
    except ValueError:
        return {"results": [], "error": "The search service gave an unreadable answer."}
    return {"results": _results(body)}


if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT, show_banner=False)

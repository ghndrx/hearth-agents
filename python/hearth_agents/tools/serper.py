"""Web search via Serper (Google search API).

Used when wikidelve has no coverage and the agent needs current external docs.
"""

import httpx
from langchain_core.tools import tool

from ..config import settings
from ..logger import log


@tool
def web_search(query: str) -> str:
    """Search Google via Serper for current documentation, examples, or news.

    Prefer ``wikidelve_search`` first — this costs real money per call.

    Args:
        query: Search query.
    """
    if not settings.serper_api_key:
        return "Serper not configured."
    try:
        with httpx.Client(timeout=15.0) as c:
            r = c.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 5},
            )
            r.raise_for_status()
            data = r.json()
        results = data.get("organic", [])[:5]
        if not results:
            return "No results."
        return "\n\n".join(
            f"{x.get('title', '?')}\n{x.get('link', '')}\n{x.get('snippet', '')}"
            for x in results
        )
    except Exception as e:
        log.warning("web_search_failed", error=str(e))
        return f"Search failed: {e}"

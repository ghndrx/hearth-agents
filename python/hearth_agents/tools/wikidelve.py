"""Wikidelve research tool — agents call this BEFORE implementing.

Wikidelve is our deep-research service. It already holds thousands of articles
on every topic we've researched this session, so searching it first saves
real MiniMax tokens and often beats fresh research on quality.
"""

import httpx
from langchain_core.tools import tool

from ..config import settings
from ..logger import log

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)


def _client() -> httpx.Client:
    if not settings.wikidelve_url:
        raise RuntimeError("WIKIDELVE_URL not configured")
    return httpx.Client(base_url=settings.wikidelve_url, timeout=_HTTP_TIMEOUT)


@tool
def wikidelve_search(query: str) -> str:
    """Search the Hearth knowledge base for prior research, architecture decisions,
    and implementation patterns.

    Always call this FIRST before calling ``wikidelve_research``. If existing
    articles cover the topic, skip the fresh research.

    Args:
        query: Keywords describing what you need to know.

    Returns:
        Ranked list of ``[kb:slug] title — snippet`` entries. Pass the slug to
        ``wikidelve_read`` for full content.
    """
    try:
        with _client() as c:
            r = c.get("/api/search/hybrid", params={"q": query, "limit": 5})
            r.raise_for_status()
            results = r.json()
        if not results:
            return "No results."
        lines = [f"[{x['kb']}:{x['slug']}] {x.get('title', '?')} — {x.get('snippet', '')[:150]}"
                 for x in results]
        return "\n".join(lines)
    except Exception as e:
        log.warning("wikidelve_search_failed", error=str(e))
        return f"Wikidelve unavailable: {e}"


@tool
def wikidelve_read(kb: str, slug: str) -> str:
    """Fetch the full markdown body of a specific wikidelve article.

    Args:
        kb: Knowledge base name (usually ``personal``).
        slug: Article slug from ``wikidelve_search`` results.

    Returns:
        Markdown content, truncated at 30K chars to stay within context limits.
    """
    try:
        with _client() as c:
            r = c.get(f"/api/articles/{kb}/{slug}")
            r.raise_for_status()
            md = r.json().get("raw_markdown", "")
        return md[:30_000] + ("\n... (truncated)" if len(md) > 30_000 else "")
    except Exception as e:
        log.warning("wikidelve_read_failed", slug=slug, error=str(e))
        return f"Article not found: {e}"


@tool
def wikidelve_research(topic: str) -> str:
    """Queue a new deep-research job. Returns a job ID.

    Use only when ``wikidelve_search`` returns nothing useful. Jobs run async —
    the result lands in the KB a few minutes later, ready for future tasks.

    Args:
        topic: Specific research topic, minimum 10 characters.
    """
    if len(topic) < 10:
        return "Topic must be at least 10 characters."
    try:
        with _client() as c:
            r = c.post("/api/research", json={"topic": topic})
            r.raise_for_status()
            job = r.json()
        return f"Queued job {job.get('job_id', '?')} — {job.get('topic', topic)}"
    except Exception as e:
        log.warning("wikidelve_research_failed", error=str(e))
        return f"Could not queue research: {e}"

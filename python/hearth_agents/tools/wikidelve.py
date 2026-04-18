"""Wikidelve research tool — agents call this BEFORE implementing.

Wikidelve is our deep-research service. It already holds thousands of articles
on every topic we've researched this session, so searching it first saves
real MiniMax tokens and often beats fresh research on quality.
"""

import httpx
from langchain_core.tools import tool

from ..config import settings
from ..logger import log
from ..research_tracker import list_pending, list_recent, record_job

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
        # Sanitize externally-sourced content before returning — a
        # research article could carry "ignore previous instructions"
        # that would land directly in agent context. Rejected content
        # returns a short operator-visible note; the agent doesn't see
        # the payload (research #3817).
        from ..sanitize import sanitize as _sanitize
        truncated = md[:30_000] + ("\n... (truncated)" if len(md) > 30_000 else "")
        sres = _sanitize(truncated, provenance=f"wikidelve:{kb}:{slug}", max_len=30_000)
        if sres.rejected:
            return f"(wikidelve article rejected by sanitizer: {sres.reject_reason})"
        return sres.safe_text
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
        job_id = str(job.get("job_id", "?"))
        record_job(job_id, topic)
        return f"Queued job {job_id} — {job.get('topic', topic)}"
    except Exception as e:
        log.warning("wikidelve_research_failed", error=str(e))
        return f"Could not queue research: {e}"


@tool
def wikidelve_pending_jobs() -> str:
    """List wikidelve research jobs this agent system has queued but not yet
    confirmed complete. Use this BEFORE calling ``wikidelve_research`` to avoid
    double-queueing the same topic.

    Returns:
        Newline-delimited ``job_id | topic | queued_at`` entries, or ``(none)``.
    """
    pending = list_pending(limit=20)
    if not pending:
        return "(none)"
    return "\n".join(f"{j['job_id']} | {j.get('topic','?')} | {j.get('ts','?')}" for j in pending)


@tool
def wikidelve_recent_completions(limit: int = 10) -> str:
    """Show the most-recently tracked wikidelve research jobs with their status.
    Useful for seeing which of your prior research requests have landed in the KB.

    Args:
        limit: Max entries to return (default 10).
    """
    recent = list_recent(limit=limit)
    if not recent:
        return "(none)"
    return "\n".join(
        f"[{j.get('status','?')}] {j['job_id']} | {j.get('topic','?')}" for j in recent
    )

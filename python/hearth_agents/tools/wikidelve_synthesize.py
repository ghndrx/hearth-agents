"""Research-article → structured-recommendations synthesizer.

Reads a wikidelve article via its slug, runs a lightweight LLM pass
(MiniMax cheap model) with a synthesis prompt that matches how the
session has hand-synthesized ~30 articles so far, returns a JSON blob
with {summary, recommendations[]}.

The recommendations are shaped for direct consumption by the
self-improvement seeder: each one has a ``change_sketch`` field the
agent can paste into a Feature.description.

Closes the research → implementation loop without an operator.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from ..models import build_minimax

_SYSTEM_PROMPT = """You are synthesizing a research article into concrete implementation items for hearth-agents, an autonomous SDLC harness (LangChain DeepAgents + Kimi + MiniMax + FastAPI + kanban UI).

Read the article content provided and return a JSON object with exactly this shape (no markdown fence, raw JSON):

{
  "summary": "<60-word core recommendation + prescribed pattern>",
  "verdict": "validates | extends | contradicts",
  "recommendations": [
    {
      "title": "<short imperative>",
      "change_sketch": "<1-2 sentence description of the code/prompt change>",
      "touches": ["python/hearth_agents/..."],
      "leverage": "high | medium | low"
    }
  ],
  "skip_reasons": ["<any recommendation you declined as infra-heavy>"]
}

Rules:
- Max 5 recommendations; only what the article concretely prescribes.
- Skip items needing new external infra (SaaS platforms, dedicated clusters, billing APIs).
- Prefer items that slot into existing tool / prompt / loop / webhook surfaces.
- Your ``touches`` paths should be realistic given hearth-agents' Python package layout."""


@tool
async def wikidelve_synthesize(kb: str, slug: str) -> str:
    """Read a wikidelve article and return structured implementation
    recommendations as JSON. Use this when the operator says "check
    out research R{N} and tell me what we should ship".

    Args:
        kb: knowledge base (usually 'personal').
        slug: article slug from wikidelve_search or wikidelve_recent_completions.
    """
    from ..tools.wikidelve import wikidelve_read
    # wikidelve_read already sanitizes + wraps in <untrusted>; we leave
    # the sanitizer tags intact so the synthesizer honors the boundary.
    article = wikidelve_read.invoke({"kb": kb, "slug": slug})
    if article.startswith("Article not found") or article.startswith("(wikidelve article rejected"):
        return json.dumps({"error": article[:300]})
    try:
        model = build_minimax()
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"synthesize model unavailable: {e}"})
    try:
        resp = await model.ainvoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": article[:20000]},
        ])
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"synthesize invoke failed: {e}"})
    text = getattr(resp, "content", "") or ""
    # Strip common markdown-fence wrapping.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("\n", 1)[0]
    # Validate it parses as JSON; if not, fall through with raw text.
    try:
        json.loads(cleaned)
        return cleaned
    except json.JSONDecodeError:
        return json.dumps({"summary": text[:800], "recommendations": [], "raw": True})

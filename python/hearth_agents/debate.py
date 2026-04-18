"""Multi-agent debate for stuck features.

Research #3816 (multi-agent debate for code generation): run the same
task on two diverse models in parallel, diff outputs, pick the one
that passes verification — empirically beats sequential cross-model
retry on hardest tasks.

Scoped implementation: operator-triggered via
``POST /features/{id}/debate`` rather than automatic. Automatic
triggering would need careful budget gating; for now the operator
decides when a feature is worth doubling spend on.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .backlog import Backlog, Feature
from .logger import log


async def run_debate(
    feature: Feature,
    backlog: Backlog,
    primary_agent: Any,
    fallback_agent: Any,
) -> dict[str, Any]:
    """Run both agents on the same feature prompt in parallel. Each
    gets its own ainvoke call; we don't touch worktrees here — the
    agents do via their own git_commit tool usage. When both finish,
    we report both outputs so the operator can choose.

    Budget-conscious: assumes fallback_agent is non-None (debate
    requires both models). Returns quickly if either isn't configured.
    """
    from .loop import _feature_prompt, _extract_token_usage, _add_feature_tokens
    if primary_agent is None or fallback_agent is None:
        return {"error": "both primary and fallback agents required for debate"}

    prompt = _feature_prompt(feature)

    async def _invoke(tag: str, agent: Any) -> dict[str, Any]:
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"metadata": {"feature_id": feature.id, "debate": tag}},
            )
        except Exception as e:  # noqa: BLE001
            log.warning("debate_invoke_failed", tag=tag, err=str(e)[:200])
            return {"tag": tag, "error": str(e)[:200]}
        in_tok, out_tok = _extract_token_usage(result)
        _add_feature_tokens(feature.id, in_tok, out_tok)
        tools: list[dict[str, Any]] = []
        for m in (result or {}).get("messages", []) or []:
            for tc in (getattr(m, "tool_calls", None) or []):
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                tools.append({"name": name})
        last = (result.get("messages") or [None])[-1]
        summary = getattr(last, "content", "") if last else ""
        return {
            "tag": tag,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "tool_count": len(tools),
            "tool_sequence": [t["name"] for t in tools][:40],
            "final_message": (summary or "")[:600],
        }

    results = await asyncio.gather(
        _invoke("primary", primary_agent),
        _invoke("fallback", fallback_agent),
    )
    return {
        "feature_id": feature.id,
        "results": results,
        "note": (
            "Both agents ran in parallel; pick the branch whose verify_staged "
            "comes back clean. If both pass, take the smaller-diff one. Only "
            "one can be merged — the operator decides."
        ),
    }

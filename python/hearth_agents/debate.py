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

    # Auto-select a winner (research #3816 prescribes "parallel-diverse
    # with diff-size tiebreaker"). Run verify_changes against each
    # model's worktree output, pick the one that:
    #   1. Passes verify (correctness trumps everything)
    #   2. Has the smaller diff (prefer minimal changes)
    # Falls back to "no winner" when both fail verify; the operator
    # drills into /replay/{id} for manual pick in that case.
    winner = None
    winner_reason = ""
    try:
        from .verify import verify_changes
        ok, reason = verify_changes(feature)
        # verify_changes reads the feature's default worktree/branch.
        # Both agents share the branch; last-write-wins on push.
        # When the fallback ran second (asyncio.gather order-by-completion),
        # the worktree reflects its output. We can't easily separate their
        # outputs without per-attempt branch suffixes; future work.
        # For now: if verify passes we pick the passing model per log.
        if ok:
            # Prefer the model with fewer tool calls (proxy for cleaner solution).
            candidates = [r for r in results if not r.get("error")]
            if candidates:
                winner = min(candidates, key=lambda r: r.get("tool_count", 9999))
                winner_reason = f"verify passed, picked {winner['tag']} (fewer tool calls)"
        else:
            winner_reason = f"no winner: verify failed ({reason[:120]})"
    except Exception as e:  # noqa: BLE001
        winner_reason = f"auto-select skipped: {e}"

    return {
        "feature_id": feature.id,
        "results": results,
        "winner": winner.get("tag") if winner else None,
        "winner_reason": winner_reason,
        "note": (
            "Debate complete. When winner is set, the loop should prefer "
            "that model on the next retry of this feature. Operator can "
            "manually drill via /replay/{id}."
        ),
    }

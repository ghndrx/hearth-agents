"""Autonomous implementation loop.

Pulls the next pending feature from the backlog, hands it to the DeepAgent,
then marks the feature ``done`` or ``blocked`` based on outcome. Sleeps between
features so we don't incinerate the MiniMax quota (4500 req/5hr on Plus).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from .backlog import Backlog, Feature
from .config import settings
from .logger import log
from .notify import Notifier
from .verify import verify_changes

# Short sleep between features — the provider-level rate limits (Kimi 4h window,
# MiniMax 4500/5hr) are the real throttle; adding a long inter-feature sleep on
# top just wastes wall-clock. 30s is enough to let the backlog flush to disk
# and not drown structlog in interleaved events.
LOOP_INTERVAL_SEC = 30

# Atomic claim lock: with multiple workers we must never let two workers grab
# the same pending feature. Also used to enforce a single-self-improvement rule
# so parallel workers don't both edit prompts.py at once.
_CLAIM_LOCK = asyncio.Lock()
_self_improv_active = 0


def _feature_prompt(feature: Feature) -> str:
    """Build the human message that kicks off the DeepAgent for one feature."""
    repos = ", ".join(feature.repos)
    research = "\n  - ".join(feature.research_topics) if feature.research_topics else "(none)"
    repo_paths = "\n".join(
        f"  {name}: {path}" for name, path in settings.repo_paths.items() if name in feature.repos
    )
    return f"""Implement feature ``{feature.id}``.

Name: {feature.name}
Priority: {feature.priority}
Discord parity: {feature.discord_parity}
Target repos: {repos}

Repo paths on disk:
{repo_paths}

Description:
{feature.description}

Research topics to check wikidelve for first:
  - {research}

Follow the orchestrator workflow: search → plan → worktree per repo → delegate
to ``developer`` → verify with ``git_status`` → delegate to ``reviewer`` →
commit on approval. Skip PR creation if implementation produced zero file changes.
"""


async def _claim_next(backlog: Backlog) -> Feature | None:
    """Atomically pick the next pending feature and mark it implementing.

    Holds ``_CLAIM_LOCK`` across the read+write so two concurrent workers can
    never grab the same feature. Also skips self-improvement features when one
    is already running — prompts.py is a shared file and parallel edits fight.
    """
    global _self_improv_active
    async with _CLAIM_LOCK:
        candidates = [f for f in backlog.features if f.status == "pending"]
        if _self_improv_active > 0:
            candidates = [f for f in candidates if not f.self_improvement]
        if not candidates:
            return None
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        candidates.sort(
            key=lambda f: (
                0 if f.self_improvement else 1,
                priority_order.get(f.priority, 99),
                f.created_at,
            )
        )
        feature = candidates[0]
        backlog.set_status(feature.id, "implementing")
        if feature.self_improvement:
            _self_improv_active += 1
        return feature


async def run_once(agent: Any, backlog: Backlog, notifier: Notifier) -> bool:
    """Process one feature. Returns True if work was done, False if idle."""
    feature = await _claim_next(backlog)
    if feature is None:
        log.debug("loop_idle", reason="no_pending_features")
        return False

    log.info("feature_start", id=feature.id, priority=feature.priority)
    await notifier.send(f"▶️ start [{feature.priority}] {feature.id}: {feature.name}")

    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": _feature_prompt(feature)}]})
        last = result["messages"][-1].content if result.get("messages") else ""
        claimed = "blocked" if "blocked" in last.lower()[:200] else "done"
        # Override the agent's self-reported verdict: if no worktree has
        # commits beyond base, the feature did not actually ship regardless
        # of what the agent's final message said.
        ok, reason = verify_changes(feature)
        verdict = claimed if (claimed == "blocked" or ok) else "blocked"
        backlog.set_status(feature.id, verdict)
        log.info("feature_end", id=feature.id, verdict=verdict, claimed=claimed, verify=reason)
        emoji = "✅" if verdict == "done" else "⛔"
        suffix = "" if verdict == claimed else f" (agent claimed {claimed}; {reason})"
        await notifier.send(f"{emoji} {verdict} {feature.id}: {feature.name}{suffix}")
    except Exception as e:
        log.exception("feature_failed", id=feature.id, error=str(e))
        backlog.set_status(feature.id, "blocked")
        await notifier.send(f"💥 failed {feature.id}: {e}")
    finally:
        if feature.self_improvement:
            global _self_improv_active
            async with _CLAIM_LOCK:
                _self_improv_active = max(0, _self_improv_active - 1)

    # After any product feature completes, auto-enqueue a fresh self-tune task
    # so the agent reflects on its own log before tackling the next product
    # feature. Self-improvement features never trigger more self-improvement
    # (would loop forever).
    if not feature.self_improvement:
        _enqueue_self_tune(backlog, trigger_feature_id=feature.id)
    return True


def _enqueue_self_tune(backlog: Backlog, trigger_feature_id: str) -> None:
    """Queue a self-tune feature that reads the agent's own log and tightens
    whichever prompt produced the worst tool-call ratio on the previous run."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    feature_id = f"self-tune-after-{trigger_feature_id}-{ts}"
    backlog.add(
        Feature(
            id=feature_id,
            name=f"Self-tune prompts after {trigger_feature_id}",
            description=(
                f"Analyze the run log for feature ``{trigger_feature_id}`` just completed. "
                f"Read ``/app/logs/hearth-agents.log`` (or ``/tmp/hearth-agents.log`` in dev). "
                "Build a histogram of tool-call names. Identify anti-patterns: "
                "read:write ratio >10:1, >5 wikidelve_search calls, >2 wikidelve_research "
                "calls, or prose responses instead of tool calls. Edit "
                "``python/hearth_agents/prompts.py`` to tighten whichever prompt "
                "produced the anti-pattern. One focused commit on a feature branch, "
                "``feat(prompts): tighten after <trigger_feature_id>``. Open a PR."
            ),
            priority="high",
            repos=["hearth-agents"],
            research_topics=[],
            discord_parity="(self-improvement)",
            self_improvement=True,
        )
    )
    log.info("self_tune_enqueued", trigger=trigger_feature_id, id=feature_id)


async def _worker(worker_id: int, backlog: Backlog, agent: Any, notifier: Notifier) -> None:
    """One feature-processing worker. Multiple workers share one backlog + agent."""
    while True:
        did_work = await run_once(agent, backlog, notifier)
        await asyncio.sleep(LOOP_INTERVAL_SEC if did_work else 60)


async def run_forever(backlog: Backlog, agent: Any) -> None:
    """Main loop. Runs until cancelled. Shares state with the HTTP server and bot.

    Spawns ``settings.loop_workers`` workers against the shared backlog. Default
    of 1 preserves existing serial behavior; raise to parallelize feature work.
    """
    n = max(1, settings.loop_workers)
    log.info("loop_started", interval_sec=LOOP_INTERVAL_SEC, workers=n, stats=backlog.stats())
    notifier = Notifier()
    await notifier.send(f"🔥 hearth-agents loop started — workers={n} {backlog.stats()}")

    try:
        await asyncio.gather(*[_worker(i, backlog, agent, notifier) for i in range(n)])
    finally:
        await notifier.close()

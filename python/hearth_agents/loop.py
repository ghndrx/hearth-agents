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

# Short sleep between features — the provider-level rate limits (Kimi 4h window,
# MiniMax 4500/5hr) are the real throttle; adding a long inter-feature sleep on
# top just wastes wall-clock. 30s is enough to let the backlog flush to disk
# and not drown structlog in interleaved events.
LOOP_INTERVAL_SEC = 30


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


async def run_once(agent: Any, backlog: Backlog, notifier: Notifier) -> bool:
    """Process one feature. Returns True if work was done, False if idle."""
    feature = backlog.next_pending()
    if feature is None:
        log.debug("loop_idle", reason="no_pending_features")
        return False

    log.info("feature_start", id=feature.id, priority=feature.priority)
    backlog.set_status(feature.id, "implementing")
    await notifier.send(f"▶️ start [{feature.priority}] {feature.id}: {feature.name}")

    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": _feature_prompt(feature)}]})
        last = result["messages"][-1].content if result.get("messages") else ""
        verdict = "blocked" if "blocked" in last.lower()[:200] else "done"
        backlog.set_status(feature.id, verdict)
        log.info("feature_end", id=feature.id, verdict=verdict)
        emoji = "✅" if verdict == "done" else "⛔"
        await notifier.send(f"{emoji} {verdict} {feature.id}: {feature.name}")
    except Exception as e:
        log.exception("feature_failed", id=feature.id, error=str(e))
        backlog.set_status(feature.id, "blocked")
        await notifier.send(f"💥 failed {feature.id}: {e}")

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


async def run_forever(backlog: Backlog, agent: Any) -> None:
    """Main loop. Runs until cancelled. Shares state with the HTTP server and bot."""
    log.info("loop_started", interval_sec=LOOP_INTERVAL_SEC, stats=backlog.stats())
    notifier = Notifier()
    await notifier.send(f"🔥 hearth-agents loop started — {backlog.stats()}")

    try:
        while True:
            did_work = await run_once(agent, backlog, notifier)
            # Short sleep when idle so new features get picked up quickly; long
            # sleep after real work so we don't hammer the rate limit.
            await asyncio.sleep(LOOP_INTERVAL_SEC if did_work else 60)
    finally:
        await notifier.close()

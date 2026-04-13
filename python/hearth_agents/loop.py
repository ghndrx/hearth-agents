"""Autonomous implementation loop.

Pulls the next pending feature from the backlog, hands it to the DeepAgent,
then marks the feature ``done`` or ``blocked`` based on outcome. Sleeps between
features so we don't incinerate the MiniMax quota (4500 req/5hr on Plus).
"""

from __future__ import annotations

import asyncio
from typing import Any

from .backlog import Backlog, Feature
from .config import settings
from .logger import log

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


async def run_once(agent: Any, backlog: Backlog) -> bool:
    """Process one feature. Returns True if work was done, False if idle."""
    feature = backlog.next_pending()
    if feature is None:
        log.debug("loop_idle", reason="no_pending_features")
        return False

    log.info("feature_start", id=feature.id, priority=feature.priority)
    backlog.set_status(feature.id, "implementing")

    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": _feature_prompt(feature)}]})
        last = result["messages"][-1].content if result.get("messages") else ""
        verdict = "blocked" if "blocked" in last.lower()[:200] else "done"
        backlog.set_status(feature.id, verdict)
        log.info("feature_end", id=feature.id, verdict=verdict)
    except Exception as e:
        log.exception("feature_failed", id=feature.id, error=str(e))
        backlog.set_status(feature.id, "blocked")

    # Auto-enqueued self-tune after every product feature was removed: those
    # features kept blocking on acceptance gates (new-test requirement, reviewer
    # APPROVE) because prompt-file edits are hard to unit-test. The seeded
    # ``self-prompt-tuning`` feature stays in the backlog for deliberate runs;
    # we just don't fire a fresh one after every product feature anymore.
    return True




async def run_forever(backlog: Backlog, agent: Any) -> None:
    """Main loop. Runs until cancelled. Shares state with the HTTP server and bot."""
    log.info("loop_started", interval_sec=LOOP_INTERVAL_SEC, stats=backlog.stats())

    while True:
        did_work = await run_once(agent, backlog)
        # Short sleep when idle so new features get picked up quickly; long
        # sleep after real work so we don't hammer the rate limit.
        await asyncio.sleep(LOOP_INTERVAL_SEC if did_work else 60)

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
from .memory import block_for_prompt, record_done
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


def _load_agents_md(feature: Feature) -> str:
    """Concatenate AGENTS.md from each target repo so the agent inherits repo
    conventions (stack, test command, style, do-not-touch list, security) before
    it starts implementing. Missing files are skipped silently.
    """
    from pathlib import Path as _P
    blocks: list[str] = []
    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        agents_md = _P(repo_path) / "AGENTS.md"
        if agents_md.exists():
            try:
                blocks.append(f"### {repo_name}/AGENTS.md\n\n{agents_md.read_text()[:6000]}")
            except OSError:
                continue
    return "\n\n---\n\n".join(blocks) if blocks else ""


def _feature_prompt(feature: Feature, fixup: str | None = None) -> str:
    """Build the human message that kicks off the DeepAgent for one feature.

    When ``fixup`` is provided, the prompt is shaped as a retry: it tells the
    agent its previous attempt failed verification and asks for a focused fix
    rather than re-implementing from scratch.
    """
    repos = ", ".join(feature.repos)
    research = "\n  - ".join(feature.research_topics) if feature.research_topics else "(none)"
    repo_paths = "\n".join(
        f"  {name}: {path}" for name, path in settings.repo_paths.items() if name in feature.repos
    )
    agents_md = _load_agents_md(feature)
    conventions_block = f"\n\nRepo conventions (from AGENTS.md):\n\n{agents_md}\n" if agents_md else ""
    memory_block = block_for_prompt(list(feature.repos))
    memory_prefix = f"\n\nRecent prior work in these repos (for context, don't duplicate):\n\n{memory_block}\n" if memory_block else ""

    if fixup:
        return f"""Your previous attempt at feature ``{feature.id}`` failed verification.

Reason: {fixup}

Fix ONLY what caused the failure. Do not re-implement. Do not revert unrelated
changes. Run the tests again in the worktree and push when green. If the same
failure recurs, report it as blocked rather than looping.

Target repos: {repos}
Repo paths:
{repo_paths}
{conventions_block}"""

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
{memory_prefix}{conventions_block}"""


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


async def run_once(agent: Any, backlog: Backlog, notifier: Notifier, worker_id: int = 0) -> bool:
    """Process one feature. Returns True if work was done, False if idle."""
    feature = await _claim_next(backlog)
    if feature is None:
        log.debug("loop_idle", reason="no_pending_features")
        return False

    log.info("feature_start", id=feature.id, priority=feature.priority, worker=worker_id)
    kind = "🔧 self-improve" if feature.self_improvement else "🚀 product"
    tag = f"[w{worker_id}]"
    await notifier.send(f"▶️ {tag} {kind} [{feature.priority}] {feature.id}: {feature.name}")

    # Bounded self-correction: if the verifier blocks on a fixable reason
    # (failing tests, oversized diff), give the agent up to MAX_FIXUPS chances
    # to fix it. Abort immediately on loop signature (same reason twice) —
    # research shows multi-turn reflection can hurt accuracy by ~40% if
    # unbounded, so keep this tight.
    MAX_FIXUPS = 2
    FIXABLE_PREFIXES = ("tests failed", "diff too large", "committed locally")

    try:
        attempt = 0
        fixup: str | None = None
        prior_reason: str | None = None
        verdict = "blocked"
        reason = "not run"
        claimed = "blocked"

        while attempt <= MAX_FIXUPS:
            prompt = _feature_prompt(feature, fixup=fixup)
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}),
                timeout=settings.per_feature_timeout_sec,
            )
            last = result["messages"][-1].content if result.get("messages") else ""
            claimed = "blocked" if "blocked" in last.lower()[:200] else "done"
            ok, reason = verify_changes(feature)
            verdict = claimed if (claimed == "blocked" or ok) else "blocked"
            if verdict == "done":
                break
            if not any(reason.startswith(p) for p in FIXABLE_PREFIXES):
                break  # non-fixable blocks (e.g. no worktree at all) won't improve
            if reason == prior_reason:
                log.warning("feature_deadlock", id=feature.id, reason=reason, attempt=attempt)
                break  # loop signature — same failure twice, bail
            prior_reason = reason
            fixup = reason
            attempt += 1
            log.info("feature_fixup", id=feature.id, attempt=attempt, reason=reason)
            await notifier.send(f"🔄 [w{worker_id}] retry {attempt}/{MAX_FIXUPS} {feature.id}: {reason[:120]}")

        backlog.set_status(feature.id, verdict)
        if verdict == "done":
            record_done(
                feature.id,
                feature.name,
                list(feature.repos),
                f"{feature.name} — {reason}. Priority {feature.priority}.",
            )
        log.info("feature_end", id=feature.id, verdict=verdict, claimed=claimed, verify=reason, attempts=attempt + 1)
        emoji = "✅" if verdict == "done" else "⛔"
        suffix = "" if verdict == claimed and attempt == 0 else f" ({reason}; attempts={attempt + 1})"
        await notifier.send(f"{emoji} [w{worker_id}] {verdict} {feature.id}: {feature.name}{suffix}")
    except asyncio.TimeoutError:
        log.warning("feature_timed_out", id=feature.id, timeout=settings.per_feature_timeout_sec)
        backlog.set_status(feature.id, "blocked")
        await notifier.send(
            f"⏱️ [w{worker_id}] timeout {feature.id} after {settings.per_feature_timeout_sec}s"
        )
    except Exception as e:
        log.exception("feature_failed", id=feature.id, error=str(e))
        backlog.set_status(feature.id, "blocked")
        await notifier.send(f"💥 [w{worker_id}] failed {feature.id}: {e}")
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
        did_work = await run_once(agent, backlog, notifier, worker_id=worker_id)
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

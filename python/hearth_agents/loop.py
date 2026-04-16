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


def _rescue_uncommitted_worktrees(feature: Feature) -> None:
    """If the agent edited files in a feature worktree but never committed
    them, auto-commit now so the iterate loop has something real to verify.

    Root cause being fixed: Kimi + DeepAgents reliably falls into
    read-explore-abandon spirals on some features — it makes legitimate
    edits via write_file/edit_file but exits the agent.ainvoke session
    without ever calling git_commit. The verifier then sees "no commits"
    and blocks, discarding real work. This helper claws those edits back
    into a commit so they go through the normal test/review gates.

    Best-effort: any failure (worktree missing, git errors) is logged and
    swallowed. Never fails the surrounding iterate loop.
    """
    import subprocess
    from pathlib import Path as _P
    branch = f"feat/{feature.id}"
    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        wt = _P(repo_path).parent / f"worktrees-{_P(repo_path).name}" / branch
        if not wt.exists():
            continue
        try:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(wt), capture_output=True, text=True, timeout=10, check=False,
            )
            has_uncommitted = status.returncode == 0 and bool(status.stdout.strip())
            if not has_uncommitted:
                continue
            # Stage + commit. Auto-push handled by our git_commit tool elsewhere,
            # but here we're bypassing the tool (raw subprocess) to keep this
            # path independent of DeepAgents. Push explicitly.
            subprocess.run(["git", "add", "-A"], cwd=str(wt), timeout=15, check=False)
            commit = subprocess.run(
                ["git", "commit", "-m", f"wip({feature.id}): auto-commit of uncommitted dev work"],
                cwd=str(wt), capture_output=True, text=True, timeout=15, check=False,
            )
            if commit.returncode != 0:
                log.warning("rescue_commit_failed", feature=feature.id, err=commit.stderr[:200])
                continue
            push = subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                cwd=str(wt), capture_output=True, text=True, timeout=60, check=False,
            )
            log.info(
                "rescue_committed",
                feature=feature.id,
                repo=repo_name,
                push_ok=(push.returncode == 0),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("rescue_failed", feature=feature.id, err=str(e)[:200])


def _agent_self_reports_blocked(text: str) -> bool:
    """Detect when the orchestrator self-reports that the feature couldn't
    be completed. Research job #3670 flagged the previous substring match
    (``"blocked" in text.lower()[:200]``) as fragile — it false-negatives
    when the agent's final message doesn't start with the word ``blocked``
    but DOES contain a structured BLOCK verdict from the reviewer or
    security subagents. This multi-signal check is more defensive.
    """
    import re as _re
    low = text.lower()
    # Our prompts teach agents to say ``BLOCKED: <reason>``; catch that first.
    if _re.search(r"\bblocked[:\s]", low):
        return True
    # Reviewer / security subagents return ``{"verdict": "BLOCK" | "REQUEST_CHANGES"}``;
    # the orchestrator usually parrots that verbatim.
    if _re.search(r'"verdict"\s*:\s*"(?:block|request_changes)"', low):
        return True
    if _re.search(r"\bverdict[:\s=\"]+(?:block|request_changes)\b", low):
        return True
    # Common give-up phrases we've observed in production logs.
    for phrase in (
        "unable to complete",
        "cannot proceed",
        "giving up on this",
        "abandoning this",
        "task failed",
    ):
        if phrase in low:
            return True
    return False

# Circuit breaker: if the block-rate in the last CIRCUIT_WINDOW_SEC exceeds
# CIRCUIT_BLOCK_THRESHOLD, pause the loop for CIRCUIT_COOLDOWN_SEC. Prevents
# burning API quota (and flooding Telegram) when something systemic is wrong —
# e.g. all features are failing because an upstream dependency broke.
CIRCUIT_WINDOW_SEC = 60 * 60       # evaluate block rate over the last hour
CIRCUIT_MIN_SAMPLES = 5            # don't trip on tiny samples
CIRCUIT_BLOCK_THRESHOLD = 0.70     # >70% blocked in window → trip
CIRCUIT_COOLDOWN_SEC = 30 * 60     # pause this long before resuming

# Sliding-window log of (wall_time, verdict) for circuit eval. Trimmed in place.
_verdict_log: list[tuple[float, str]] = []
_circuit_open_until: float = 0.0


def _record_verdict(verdict: str) -> None:
    import time as _t
    now = _t.time()
    _verdict_log.append((now, verdict))
    cutoff = now - CIRCUIT_WINDOW_SEC
    while _verdict_log and _verdict_log[0][0] < cutoff:
        _verdict_log.pop(0)


def _check_circuit_breaker() -> bool:
    """Return True if the breaker should be OPEN (loop paused). Also mutates
    ``_circuit_open_until`` to extend a cooldown when tripped fresh."""
    import time as _t
    now = _t.time()
    if _circuit_open_until > now:
        return True
    if len(_verdict_log) < CIRCUIT_MIN_SAMPLES:
        return False
    blocked = sum(1 for _, v in _verdict_log if v == "blocked")
    rate = blocked / len(_verdict_log)
    if rate >= CIRCUIT_BLOCK_THRESHOLD:
        globals()["_circuit_open_until"] = now + CIRCUIT_COOLDOWN_SEC
        samples_at_trip = len(_verdict_log)
        # Clear the verdict log when tripping — otherwise the stale
        # all-blocked window persists past the cooldown and re-trips the
        # breaker the instant a single new feature fails, trapping the
        # loop in a "wake-up-reblock" cycle. Fresh cooldown = fresh data.
        _verdict_log.clear()
        log.warning(
            "circuit_breaker_tripped",
            block_rate=round(rate, 2),
            samples_at_trip=samples_at_trip,
            cooldown_sec=CIRCUIT_COOLDOWN_SEC,
        )
        # Fire-and-forget Telegram alert — we're already in an async context
        # indirectly (called from _worker's while loop). The send is safe to
        # schedule via create_task and not await, so the breaker check stays
        # synchronous.
        try:
            from .notify import Notifier as _N
            _n = _N()
            asyncio.create_task(_n.send_coalesced(
                "circuit_breaker",
                f"🚨 circuit breaker OPEN — block rate {rate:.0%} over "
                f"{len(_verdict_log)} features, pausing {CIRCUIT_COOLDOWN_SEC // 60}m",
            ))
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def circuit_state() -> dict:
    """Snapshot of circuit breaker state. Used by /stats."""
    import time as _t
    now = _t.time()
    blocked = sum(1 for _, v in _verdict_log if v == "blocked")
    total = len(_verdict_log)
    return {
        "open": _circuit_open_until > now,
        "open_for_sec": max(0, int(_circuit_open_until - now)),
        "window_samples": total,
        "window_blocked": blocked,
        "block_rate": round(blocked / total, 2) if total else 0.0,
    }

# Per-provider cooldowns. Tracking primary (Kimi) and fallback (MiniMax)
# separately is what stops the ping-pong: when both hit 429 at once, workers
# only sleep if BOTH are cooled down. If only one is cooled, we route through
# the other instead of burning the cooldown idle.
# Minimum cooldown we'll ever apply after a 429. Providers often advertise
# Retry-After of 60s for 4h-window limits, but the deeper weekly/monthly cap
# is the real limiter — hammering every minute just spams alerts and burns
# tiny quota bursts. 15 min of backoff is the effective floor.
_RATE_LIMIT_MIN_BACKOFF_SEC = 15 * 60
_RATE_LIMIT_BACKOFF_SEC = 15 * 60  # default when no Retry-After header is given
_RATE_LIMIT_MAX_BACKOFF_SEC = 4 * 60 * 60  # safety cap; never sleep longer than this
_primary_cooldown_until: float = 0.0
_fallback_cooldown_until: float = 0.0


def _is_rate_limit_error(e: BaseException) -> bool:
    """Detect Kimi/MiniMax/OpenAI rate-limit errors.

    Prefers the typed ``openai.RateLimitError`` (sturdier across SDK versions)
    and falls back to substring + status-code heuristics for cases where
    LangChain has rewrapped the original exception.
    """
    try:
        from openai import RateLimitError
        if isinstance(e, RateLimitError):
            return True
    except ImportError:
        pass
    code = getattr(e, "status_code", None) or getattr(e, "code", None)
    if code == 429:
        return True
    msg = str(e).lower()
    return "rate_limit_reached" in msg or "rate limit" in msg


def _retry_after_seconds(e: BaseException) -> float:
    """Pull a backoff duration from the rate-limit error if the provider
    included one. Falls back to ``_RATE_LIMIT_BACKOFF_SEC`` when nothing
    parseable is found. Capped to avoid bad headers stranding workers for days.
    """
    # openai SDK exposes the raw response on the exception in some versions
    response = getattr(e, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        for key in ("retry-after", "Retry-After", "x-ratelimit-reset"):
            raw = headers.get(key) if hasattr(headers, "get") else None
            if raw:
                try:
                    return min(max(float(raw), _RATE_LIMIT_MIN_BACKOFF_SEC), _RATE_LIMIT_MAX_BACKOFF_SEC)
                except (TypeError, ValueError):
                    pass
    # Some providers embed the reset time in the body — best effort string parse.
    msg = str(e)
    import re
    m = re.search(r"retry[- ]after[: ]+(\d+)", msg, re.IGNORECASE)
    if m:
        return min(max(float(m.group(1)), _RATE_LIMIT_MIN_BACKOFF_SEC), _RATE_LIMIT_MAX_BACKOFF_SEC)
    return float(_RATE_LIMIT_BACKOFF_SEC)


# Per-provider rate-limit alert coalescing is now handled by
# Notifier.send_coalesced(key=f"rate_limit:{provider}"). The ad-hoc state
# that used to live here is redundant.

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

    # heal_hint comes from healer.py — a targeted instruction reflecting the
    # specific verify failure last time. Pasting it at the TOP makes it the
    # first thing the orchestrator reads, so the next attempt can't blindly
    # repeat the same failure mode (the 7/9 'no commits' cluster we saw).
    heal_block = f"\n\n{feature.heal_hint}\n" if feature.heal_hint else ""

    return f"""Implement feature ``{feature.id}``.
{heal_block}
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

    Before returning, runs the splitter: any candidate targeting multiple repos
    is replaced with per-repo children and we re-select. Prevents the "one
    attempt implements everything across 3 repos and blows through the diff
    cap" failure mode (data-export-portability = 2649 lines, message-threading
    = 443,603 lines).
    """
    from .splitter import maybe_split

    global _self_improv_active
    async with _CLAIM_LOCK:
        # Split loop: keep re-selecting until we get a candidate that doesn't
        # need splitting, or we run out of candidates. Bounded by backlog size
        # so a pathological state can't spin.
        for _ in range(len(backlog.features) + 1):
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
            if maybe_split(backlog, feature):
                # Parent was replaced by children; re-select so we pick a real
                # implementable feature (one of the new children or something
                # else that outranks them).
                continue
            backlog.set_status(feature.id, "implementing")
            if feature.self_improvement:
                _self_improv_active += 1
            return feature
        return None


async def run_once(
    agent: Any,
    backlog: Backlog,
    notifier: Notifier,
    worker_id: int = 0,
    using_fallback: bool = False,
    alt_agent: Any | None = None,
) -> bool:
    """Process one feature. Returns True if work was done, False if idle.

    ``using_fallback`` tells the rate-limit handler which provider's cooldown
    to set if a 429 fires — without it we couldn't tell whether the failure
    came from primary (Kimi) or fallback (MiniMax) and we'd ping-pong.
    """
    feature = await _claim_next(backlog)
    if feature is None:
        log.debug("loop_idle", reason="no_pending_features")
        return False

    log.info("feature_start", id=feature.id, priority=feature.priority, worker=worker_id)
    kind = "🔧 self-improve" if feature.self_improvement else "🚀 product"
    tag = f"[w{worker_id}]"
    # Noisy per-feature-start pings removed — feature_end is enough signal,
    # and the log still carries full start/stop events for debugging.

    # Bounded self-correction: if the verifier blocks on a fixable reason
    # Bounded self-correction: allow MAX_FIXUPS retries for recoverable
    # failures. Raised from 2 → 4 because the triage showed 6/12 blocks were
    # "tests failed" that ran out of attempts — Kimi has plenty of quota left,
    # and failing tests are exactly the kind of thing an extra 2 retries
    # catches. Still aborts on loop-signature deadlock (same reason twice)
    # so we don't infinitely spin.
    MAX_FIXUPS = 4
    FIXABLE_PREFIXES = ("tests failed", "diff too large", "committed locally", "complexity too high", "planner_undercount")
    # Patterns inside the verifier reason or test output that signal the
    # failure cannot be solved by another attempt — research shows ~90% of
    # retry budget gets wasted on these. Bail to next feature instead of
    # burning the full MAX_FIXUPS budget. Conservative list: only patterns
    # we've actually observed as repeatable failure modes.
    UNSOLVABLE_SIGNALS = (
        "no such file or directory: 'go'",      # missing toolchain
        "no such file or directory: 'pnpm'",    # missing toolchain
        "command not found: cargo",             # missing toolchain
        "permission denied",                    # filesystem permission
        "401 unauthorized",                     # external API auth
        "403 forbidden",                        # external API auth
        "no space left on device",              # disk full
        "could not resolve host",               # network
    )

    try:
        attempt = 0
        fixup: str | None = None
        prior_reason: str | None = None
        verdict = "blocked"
        reason = "not run"
        claimed = "blocked"

        while attempt <= MAX_FIXUPS:
            prompt = _feature_prompt(feature, fixup=fixup)
            # Cross-model retry: alternate between agent and alt_agent on each
            # attempt. Kimi and MiniMax have different failure modes — if one
            # can't close a feature after trying, the other often can. On
            # attempt 0 we use the caller's choice (set by ping-pong); on
            # each retry we flip. Doubles the effective "try again" budget
            # without doubling any one provider's quota burn.
            active = agent if (attempt % 2 == 0 or alt_agent is None) else alt_agent
            active_provider = (
                "fallback" if (active is alt_agent) ^ (not using_fallback) else "primary"
            )
            log.info(
                "iterate_attempt",
                feature=feature.id,
                attempt=attempt,
                active_provider=active_provider,
            )
            result = await asyncio.wait_for(
                active.ainvoke({"messages": [{"role": "user", "content": prompt}]}),
                timeout=settings.per_feature_timeout_sec,
            )
            last = result["messages"][-1].content if result.get("messages") else ""
            claimed = "blocked" if _agent_self_reports_blocked(last) else "done"
            # Rescue stray diffs: if the agent wrote files in a worktree but
            # never committed, auto-commit them now so the iterate loop has
            # something to verify. Turns the dominant "no commits" failure
            # mode into a recoverable "tests maybe fail" one.
            _rescue_uncommitted_worktrees(feature)
            ok, reason = verify_changes(feature)
            verdict = claimed if (claimed == "blocked" or ok) else "blocked"
            if verdict == "done":
                break
            if not any(reason.startswith(p) for p in FIXABLE_PREFIXES):
                break  # non-fixable blocks (e.g. no worktree at all) won't improve
            # Early bail on known-unsolvable error signatures (research #3784:
            # ~90% of retry budget gets wasted on errors no retry can fix).
            reason_lower = reason.lower()
            unsolvable = next((s for s in UNSOLVABLE_SIGNALS if s in reason_lower), None)
            if unsolvable:
                log.warning("feature_unsolvable", id=feature.id, signal=unsolvable, attempt=attempt)
                break  # bail; saves remaining retries for features that can succeed
            if reason == prior_reason:
                log.warning("feature_deadlock", id=feature.id, reason=reason, attempt=attempt)
                break  # loop signature — same failure twice, bail
            prior_reason = reason
            fixup = reason
            attempt += 1
            log.info("feature_fixup", id=feature.id, attempt=attempt, reason=reason)
            # In-loop retries are transient — only log them. The final
            # feature_end notification will include "attempts=N" if it matters.

        backlog.set_status(feature.id, verdict)
        _record_verdict(verdict)
        if verdict == "done":
            # Clear the heal hint so future re-runs (idea engine duplicates,
            # manual re-queues) start from a clean prompt instead of carrying
            # stale "PRIOR FAILURE" advice that no longer applies.
            feature.heal_hint = ""
            backlog.save()
            record_done(
                feature.id,
                feature.name,
                list(feature.repos),
                f"{feature.name} — {reason}. Priority {feature.priority}.",
            )
        log.info("feature_end", id=feature.id, verdict=verdict, claimed=claimed, verify=reason, attempts=attempt + 1)
        # Only ping Telegram for successes. Failures get batched by the healer
        # (🩹) and escalations (🚨). Per-feature blocks were the biggest source
        # of noise — they were firing dozens of times per hour while healer
        # loops resurrected and re-blocked the same features.
        if verdict == "done":
            suffix = "" if attempt == 0 else f" (attempts={attempt + 1})"
            await notifier.send(f"✅ [w{worker_id}] done {feature.id}: {feature.name}{suffix}")
    except asyncio.TimeoutError:
        log.warning("feature_timed_out", id=feature.id, timeout=settings.per_feature_timeout_sec)
        # Rescue BEFORE marking blocked — the timeout may have killed the
        # agent mid-write, leaving real code uncommitted in the worktree.
        # Without this call, a 10-min timeout discards N files of legit work.
        # Observed in production: 3 of 5 blocked features had 6+ uncommitted
        # files that the rescue path never touched because it sat inside the
        # normal post-ainvoke branch only.
        _rescue_uncommitted_worktrees(feature)
        # Re-verify after rescue — if the rescued commit is substantive, the
        # feature might actually pass the gates now.
        ok, reason = verify_changes(feature)
        verdict = "done" if ok else "blocked"
        backlog.set_status(feature.id, verdict)
        if verdict == "done":
            log.info("feature_timeout_rescued", id=feature.id)
        await notifier.send_coalesced(
            "timeout",
            f"⏱️ feature timeout after {settings.per_feature_timeout_sec}s — further timeouts suppressed for 1h",
        )
    except Exception as e:
        if _is_rate_limit_error(e):
            # Set the cooldown for whichever provider actually 429'd, not both.
            # That's what lets workers route through the *other* provider while
            # one is sleeping, instead of ping-ponging into both cooldowns.
            global _primary_cooldown_until, _fallback_cooldown_until
            now = asyncio.get_event_loop().time()
            backoff = _retry_after_seconds(e)
            cooldown_until = now + backoff
            provider = "fallback (MiniMax)" if using_fallback else "primary (Kimi)"
            # Only ping Telegram on the LEADING edge — when this 429 is the
            # first one to open the cooldown, not when another worker is just
            # racing into the same already-open window. With 2 workers this
            # doubled every alert; during a saturated window it fired every
            # ~30s until the quota refreshed.
            was_closed = (
                _fallback_cooldown_until if using_fallback else _primary_cooldown_until
            ) <= now
            if using_fallback:
                _fallback_cooldown_until = cooldown_until
            else:
                _primary_cooldown_until = cooldown_until
            backlog.set_status(feature.id, "pending")
            log.warning(
                "rate_limited",
                id=feature.id,
                provider=provider,
                backoff_sec=int(backoff),
                was_closed=was_closed,
                error=str(e)[:200],
            )
            # Single coalesced alert per provider per hour — kept sending every
            # 60s during sustained outages because Kimi's Retry-After is 60s
            # even when the weekly quota is out for days.
            await notifier.send_coalesced(
                f"rate_limit:{provider}",
                f"🛑 {provider} rate-limited — cooling {int(backoff) // 60}m, "
                "routing through the other provider (alerts suppressed 1h)",
            )
        else:
            log.exception("feature_failed", id=feature.id, error=str(e))
            # Rescue before marking blocked — same rationale as the timeout
            # branch: the exception may have killed the agent mid-write. Don't
            # discard real uncommitted code just because something downstream
            # of write_file threw.
            _rescue_uncommitted_worktrees(feature)
            ok, reason = verify_changes(feature)
            verdict = "done" if ok else "blocked"
            backlog.set_status(feature.id, verdict)
            if verdict == "done":
                log.info("feature_exception_rescued", id=feature.id)
            # Generic failures are logged but NOT sent to Telegram anymore —
            # they were the biggest residual source of spam. The healer's
            # batched reset alert covers the aggregate signal.
    finally:
        if feature.self_improvement:
            global _self_improv_active
            async with _CLAIM_LOCK:
                _self_improv_active = max(0, _self_improv_active - 1)

    # Auto-enqueue removed: self-tune-after-<feature> tasks kept blocking on
    # acceptance gates. Seeded self-prompt-tuning remains for deliberate runs.
    return True




_pingpong_counter: int = 0


async def _worker(
    worker_id: int,
    backlog: Backlog,
    agent: Any,
    notifier: Notifier,
    fallback_agent: Any | None = None,
) -> None:
    """One feature-processing worker.

    Provider routing per iteration (both-healthy mode now ping-pongs):
      - Both healthy + fallback configured -> alternate per feature.
        Kimi and MiniMax share load 50/50 via a shared ``_pingpong_counter``
        so across multiple workers the ratio evens out. MiniMax Max has
        15k req / 5h, plenty of headroom to carry half the work.
      - Primary cooled, fallback hot       -> use fallback
      - Fallback cooled, primary hot       -> use primary
      - No fallback configured             -> use primary
      - Both cooled                        -> sleep until the soonest expiry
    """
    while True:
        # Circuit breaker comes FIRST: if quality has collapsed we want to
        # pause before burning any more API quota, even if we have fallback
        # providers available.
        if _check_circuit_breaker():
            import time as _t
            wait = max(30, int(_circuit_open_until - _t.time()))
            log.info("circuit_breaker_open", worker=worker_id, sleep_sec=wait)
            await asyncio.sleep(min(wait, 120))  # wake periodically to re-eval
            continue

        now = asyncio.get_event_loop().time()
        primary_cool = _primary_cooldown_until > now
        fallback_cool = (
            fallback_agent is not None and _fallback_cooldown_until > now
        )

        if primary_cool and (fallback_agent is None or fallback_cool):
            # Nothing to use — sleep until whichever cooldown ends first.
            soonest = _primary_cooldown_until
            if fallback_agent is not None:
                soonest = min(soonest, _fallback_cooldown_until)
            wait = max(1.0, soonest - now)
            log.info(
                "rate_limit_sleeping",
                worker=worker_id,
                sleep_sec=int(wait),
                primary_cool=primary_cool,
                fallback_cool=fallback_cool,
            )
            await asyncio.sleep(wait)
            continue

        # Provider selection:
        #  1. If one is cooled, use the other.
        #  2. If both are healthy and fallback is configured, ping-pong on a
        #     shared counter. Spreads load 50/50 across providers instead of
        #     dogpiling whichever is nominally "primary".
        if primary_cool and fallback_agent is not None and not fallback_cool:
            use_fallback = True
            reason = "primary_cooled"
        elif fallback_cool:
            use_fallback = False
            reason = "fallback_cooled"
        elif fallback_agent is not None:
            # Weighted ping-pong using ``settings.minimax_bias`` (0.0-1.0).
            # 0.5 = even split; higher = more toward fallback (MiniMax). We use
            # a running fractional accumulator rather than random() so the
            # distribution is deterministic and exact over any window — e.g.
            # bias=0.75 produces exactly 3 fallback picks for every 1 primary.
            global _pingpong_counter
            bias = max(0.0, min(1.0, settings.minimax_bias))
            # Accumulator approach: each tick adds bias; when the integer
            # part increments, route to fallback. Equivalent to Bresenham.
            prev = _pingpong_counter
            _pingpong_counter += 1
            use_fallback = int((_pingpong_counter) * bias) > int(prev * bias)
            reason = f"pingpong(bias={bias:.2f})"
        else:
            use_fallback = False
            reason = "no_fallback_configured"

        active_agent = fallback_agent if use_fallback else agent
        log.info(
            "worker_routing",
            worker=worker_id,
            provider="fallback" if use_fallback else "primary",
            reason=reason,
        )
        # For cross-model retry: pass the OTHER agent as alt_agent. If the
        # active agent is primary, alt is fallback, and vice-versa. When
        # there's no fallback configured, alt_agent is None and run_once
        # stays on the same model throughout its retries (old behavior).
        alt = fallback_agent if not use_fallback else agent
        did_work = await run_once(
            active_agent,
            backlog,
            notifier,
            worker_id=worker_id,
            using_fallback=use_fallback,
            alt_agent=alt,
        )
        await asyncio.sleep(LOOP_INTERVAL_SEC if did_work else 60)


async def run_forever(backlog: Backlog, agent: Any, fallback_agent: Any | None = None) -> None:
    """Main loop. Runs until cancelled. Shares state with the HTTP server and bot.

    Spawns ``settings.loop_workers`` workers against the shared backlog. Default
    of 1 preserves existing serial behavior; raise to parallelize feature work.
    """
    n = max(1, settings.loop_workers)
    log.info("loop_started", interval_sec=LOOP_INTERVAL_SEC, workers=n, stats=backlog.stats())
    notifier = Notifier()
    await notifier.send(f"🔥 hearth-agents loop started — workers={n} {backlog.stats()}")

    try:
        await asyncio.gather(
            *[_worker(i, backlog, agent, notifier, fallback_agent) for i in range(n)]
        )
    finally:
        await notifier.close()

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


def _rescue_uncommitted_worktrees(feature: Feature) -> bool:
    """If the agent edited files in a feature worktree but never committed
    them, auto-commit now so the iterate loop has something real to verify.

    Returns True when rescue actually committed something, False otherwise.
    Callers pass that signal into the transition reason so operators can
    tell which features survived via auto-commit vs proper agent commit
    (research #3810 prescribes this distinction for post-hoc analysis).

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
    did_commit = False
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
            # Scrub build-artifact / dep-cache paths before commit — otherwise
            # rescue can ship 400k-line node_modules diffs that immediately
            # fail the diff-size gate. Same pattern list as git_commit tool.
            _BLOCKED_DIRS = ("node_modules/", ".pnpm-store/", "dist/", "build/", "target/",
                             ".next/", ".svelte-kit/", ".venv/", "__pycache__/",
                             ".pytest_cache/", ".turbo/", "coverage/")
            # Lock files + debris files: observed 849k-line diffs dominated
            # by pnpm-lock.yaml churn from the agent running pnpm install.
            _BLOCKED_FILES = ("pnpm-lock.yaml", "package-lock.json", "yarn.lock",
                              "Cargo.lock", "poetry.lock", "Gemfile.lock", "uv.lock",
                              "dummy-push-trigger.txt", "dummy-trigger.txt",
                              "push-trigger.txt")
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=str(wt), capture_output=True, text=True, timeout=10, check=False,
            ).stdout.splitlines()
            for p in staged:
                if any(sig in p for sig in _BLOCKED_DIRS) or any(p.endswith(f) for f in _BLOCKED_FILES):
                    subprocess.run(["git", "rm", "--cached", "-r", "--", p],
                                   cwd=str(wt), timeout=10, check=False)
            commit = subprocess.run(
                ["git", "commit", "-m", f"wip({feature.id}): auto-commit of uncommitted dev work"],
                cwd=str(wt), capture_output=True, text=True, timeout=15, check=False,
            )
            if commit.returncode != 0:
                log.warning("rescue_commit_failed", feature=feature.id, err=commit.stderr[:200])
                continue
            did_commit = True
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
    return did_commit


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

# Sliding-window logs of (wall_time, verdict) for circuit eval. Trimmed in
# place. Per-provider logs let us open the breaker on ONE provider without
# idling the whole loop — e.g. Kimi's tool-calling regresses, MiniMax is
# fine, so pause Kimi and route everything through MiniMax for 30m.
_verdict_log: list[tuple[float, str]] = []
_verdict_log_by_provider: dict[str, list[tuple[float, str]]] = {"primary": [], "fallback": []}
_circuit_open_until: float = 0.0
_circuit_open_until_by_provider: dict[str, float] = {"primary": 0.0, "fallback": 0.0}


def _record_verdict(verdict: str, provider: str = "primary") -> None:
    import time as _t
    now = _t.time()
    _verdict_log.append((now, verdict))
    cutoff = now - CIRCUIT_WINDOW_SEC
    while _verdict_log and _verdict_log[0][0] < cutoff:
        _verdict_log.pop(0)
    plog = _verdict_log_by_provider.setdefault(provider, [])
    plog.append((now, verdict))
    while plog and plog[0][0] < cutoff:
        plog.pop(0)


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


def _check_provider_circuit(provider: str) -> bool:
    """Per-provider circuit breaker. Returns True when the provider's
    block rate alone has tripped — caller should route around this
    provider. Trips independently of the global breaker so we can
    quarantine one model without pausing the loop."""
    import time as _t
    now = _t.time()
    if _circuit_open_until_by_provider.get(provider, 0.0) > now:
        return True
    plog = _verdict_log_by_provider.get(provider, [])
    if len(plog) < CIRCUIT_MIN_SAMPLES:
        return False
    blocked = sum(1 for _, v in plog if v == "blocked")
    rate = blocked / len(plog)
    if rate >= CIRCUIT_BLOCK_THRESHOLD:
        _circuit_open_until_by_provider[provider] = now + CIRCUIT_COOLDOWN_SEC
        samples = len(plog)
        plog.clear()
        log.warning(
            "provider_circuit_tripped",
            provider=provider,
            block_rate=round(rate, 2),
            samples=samples,
            cooldown_sec=CIRCUIT_COOLDOWN_SEC,
        )
        return True
    return False


def circuit_state() -> dict:
    """Snapshot of circuit breaker state. Used by /stats."""
    import time as _t
    now = _t.time()
    blocked = sum(1 for _, v in _verdict_log if v == "blocked")
    total = len(_verdict_log)

    def _pstate(p: str) -> dict:
        plog = _verdict_log_by_provider.get(p, [])
        pblocked = sum(1 for _, v in plog if v == "blocked")
        ptotal = len(plog)
        return {
            "open": _circuit_open_until_by_provider.get(p, 0.0) > now,
            "open_for_sec": max(0, int(_circuit_open_until_by_provider.get(p, 0.0) - now)),
            "window_samples": ptotal,
            "window_blocked": pblocked,
            "block_rate": round(pblocked / ptotal, 2) if ptotal else 0.0,
        }

    return {
        "open": _circuit_open_until > now,
        "open_for_sec": max(0, int(_circuit_open_until - now)),
        "window_samples": total,
        "window_blocked": blocked,
        "block_rate": round(blocked / total, 2) if total else 0.0,
        "per_provider": {"primary": _pstate("primary"), "fallback": _pstate("fallback")},
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

# Preemptive rate-limit prediction: we track the wall-time of each
# outgoing agent.ainvoke per-provider. If the rate in the sliding
# window is within ``_RATE_APPROACH_HEADROOM`` of the provider's
# advertised limit, we sleep briefly to let the window decay. Hitting
# 429 costs 15m of floor-cooldown; 3-10s of preemptive slowdown is
# far cheaper. Approximate — providers don't reveal the exact window
# shape — but good enough to dodge the 429 flip-flop.
_REQ_WINDOW_SEC = 5 * 60 * 60     # match Kimi 4h / MiniMax 5h window order of magnitude
_RATE_APPROACH_HEADROOM = 0.10    # sleep when within 10% of advertised rate
# Three independent dimensions per provider (research #3808 — proactive
# rate-limit management). Each entry is (ts, value) — value is 1 for RPM,
# actual token count for ITPM/OTPM. A single list per dim keeps bookkeeping
# simple and windowing uniform; we deque-trim on every note.
_request_ticks: dict[str, list[float]] = {"primary": [], "fallback": []}
_input_tokens: dict[str, list[tuple[float, int]]] = {"primary": [], "fallback": []}
_output_tokens: dict[str, list[tuple[float, int]]] = {"primary": [], "fallback": []}


def _note_request(provider: str) -> None:
    """Record one outgoing ainvoke so the predictor has data to work with.
    Kept for call-site compatibility; token counts are recorded separately
    via ``_note_usage`` after the response comes back."""
    import time as _t
    now = _t.time()
    ticks = _request_ticks.setdefault(provider, [])
    ticks.append(now)
    cutoff = now - _REQ_WINDOW_SEC
    while ticks and ticks[0] < cutoff:
        ticks.pop(0)


def _note_usage(provider: str, input_tokens: int, output_tokens: int) -> None:
    """Record post-response token counts. LangChain surfaces these on
    AIMessage.response_metadata['token_usage']; the caller extracts and
    passes them here. Zero-valued inputs are silently skipped (empty
    response, typically a cancellation)."""
    import time as _t
    now = _t.time()
    cutoff = now - _REQ_WINDOW_SEC
    if input_tokens > 0:
        ilog = _input_tokens.setdefault(provider, [])
        ilog.append((now, input_tokens))
        while ilog and ilog[0][0] < cutoff:
            ilog.pop(0)
    if output_tokens > 0:
        olog = _output_tokens.setdefault(provider, [])
        olog.append((now, output_tokens))
        while olog and olog[0][0] < cutoff:
            olog.pop(0)


def _snapshot_prompt(prompt: str) -> str:
    """Persist the full prompt to /data/prompts/<sha12>.txt and return
    the sha. Idempotent — same prompt reuses the same file. Lets
    /replay/{id} present the exact text the agent received for each
    attempt without bloating attempts.jsonl with duplicated full
    prompts. Best-effort: write failures degrade to empty sha."""
    import hashlib
    from pathlib import Path as _P
    sha = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()[:12]
    path = _P(f"/data/prompts/{sha}.txt")
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompt, encoding="utf-8")
        except OSError:
            return ""
    return sha


def _append_attempt_log(
    feature_id: str,
    attempt: int,
    provider: str,
    result: Any,
    input_tokens: int,
    output_tokens: int,
    duration_sec: float = 0.0,
    worker_id: int = 0,
    prompt_sha: str = "",
) -> None:
    """Append one line per attempt to ``/data/attempts.jsonl``. Foundation
    for the debugging-session-replay tooling (research #3807) — captures
    which tools were called with what args in what order, plus token
    usage. Tool arg values are truncated and stringified defensively so
    a non-JSON-serializable tool arg can't break the loop."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P
    tool_trace: list[dict] = []
    try:
        for m in (result or {}).get("messages", []) or []:
            tcs = getattr(m, "tool_calls", None) or []
            for tc in tcs:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
                try:
                    args_repr = _json.dumps(args, default=str)[:400]
                except (TypeError, ValueError):
                    args_repr = str(args)[:400]
                tool_trace.append({"name": name, "args": args_repr})
    except (AttributeError, TypeError):
        pass
    entry = {
        "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
        "feature_id": feature_id,
        "attempt": attempt,
        "worker": int(worker_id),
        "provider": provider,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "duration_sec": round(float(duration_sec), 2),
        "prompt_sha": prompt_sha,
        "tool_calls": tool_trace[:200],  # cap so a stuck loop can't bloat
    }
    path = _P("/data/attempts.jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry) + "\n")
    except OSError as e:
        log.warning("attempt_log_write_failed", err=str(e)[:160], feature=feature_id)


def _extract_token_usage(result: Any) -> tuple[int, int]:
    """Walk a LangChain agent result's messages for AIMessage.response_metadata
    token_usage and sum input/output. Returns (0, 0) on any extraction
    failure — the throttle gracefully degrades to RPM-only when tokens
    aren't available."""
    total_in = 0
    total_out = 0
    try:
        for m in result.get("messages", []) or []:
            meta = getattr(m, "response_metadata", None) or {}
            usage = meta.get("token_usage") or meta.get("usage") or {}
            total_in += int(usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0) or 0)
            total_out += int(usage.get("completion_tokens", 0) or usage.get("output_tokens", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0, 0
    return total_in, total_out


def _throttle_for_rate_approach(worker_id: int) -> None:
    """Sleep briefly (sync) when ANY of RPM / ITPM / OTPM approaches the
    provider's advertised limit (research #3808). Three independent
    dimensions means the worker holds off on whichever axis is stressed:
    a feature with a massive prompt will throttle on ITPM even when RPM
    is fine, and vice versa.

    Short sleep (1-10s) — the alternative is a 15m cooldown floor after
    hitting a 429, which is far worse for tail latency. Returns on first
    dimension that trips, doesn't stack sleeps."""
    import time as _t
    # Ceiling for each dimension. RPM from settings. Token ceilings are
    # provider-published: Kimi coding endpoint and MiniMax Plus both
    # advertise ~1M token/min on input and ~200K/min on output. Configurable
    # via settings once we have real observations; the defaults below are
    # conservative to stay well under any realistic plan.
    per_dim: list[tuple[str, dict[str, int], dict[str, list]]] = [
        ("RPM", {"primary": settings.minimax_rate_limit, "fallback": settings.minimax_rate_limit}, _request_ticks),
        ("ITPM", {"primary": 800_000, "fallback": 800_000}, _input_tokens),  # type: ignore[dict-item]
        ("OTPM", {"primary": 150_000, "fallback": 150_000}, _output_tokens),  # type: ignore[dict-item]
    ]
    for dim_name, limits, log_ in per_dim:
        for provider, limit in limits.items():
            if limit <= 0:
                continue
            entries = log_.get(provider, [])
            if not entries:
                continue
            # For RPM the list is floats (count=1 each); for token dims it's
            # (ts, int). Sum appropriately.
            if dim_name == "RPM":
                value = len(entries)
            else:
                value = sum(v for _, v in entries)
            approach = limit * (1.0 - _RATE_APPROACH_HEADROOM)
            if value >= approach:
                sleep_for = max(1.0, min(10.0, _REQ_WINDOW_SEC / max(1, value)))
                log.info(
                    "rate_approach_throttle",
                    worker=worker_id,
                    provider=provider,
                    dimension=dim_name,
                    value=int(value),
                    limit=limit,
                    sleep_sec=round(sleep_for, 1),
                )
                _t.sleep(sleep_for)
                return


# Stuck-worker watchdog: each worker reports its last progress moment
# into ``_worker_heartbeat`` before entering agent.ainvoke. A separate
# task periodically scans for entries older than STUCK_THRESHOLD_SEC and
# logs them; killing a coroutine from outside is messy, so we log loud
# (operators see it in /stats, Langfuse, Telegram). Future: asyncio
# cancel when we trust the signal.
_worker_heartbeat: dict[int, tuple[float, str]] = {}   # worker_id → (ts, feature_id)
_worker_last_error: dict[int, tuple[float, str, str]] = {}  # worker_id → (ts, feature_id, error)

# Read-only mode: when True, workers skip _claim_next (no new features
# get claimed). Background tasks (healer, scheduler, etc.) still run;
# the loop just stops picking up work. Useful for maintenance windows
# or when gateway-01 is under load and you want to freeze new spend.
_read_only_mode: bool = False


def set_read_only(enabled: bool) -> bool:
    global _read_only_mode
    prev = _read_only_mode
    _read_only_mode = bool(enabled)
    return prev


def is_read_only() -> bool:
    return _read_only_mode
STUCK_THRESHOLD_SEC = int(settings.per_feature_timeout_sec * 1.5)


def _record_worker_error(worker_id: int, feature_id: str, error: str) -> None:
    """Called from exception / timeout handlers so operators can see
    the last failure per worker without grepping logs. Trimmed to
    200 chars; full stack traces still go to the structured log."""
    import time as _t
    _worker_last_error[worker_id] = (_t.time(), feature_id, error[:200])

# Per-feature token accumulator. Reset per feature (not per attempt) so a
# feature that spirals across 6 fixups trips the cap. Rough $/token pricing
# to turn tokens into settings.per_feature_budget_usd comparisons. Numbers
# are conservative upper bounds; precise cost accounting lives elsewhere.
_PRICE_IN_PER_1M = 0.30   # $/1M input tokens (conservative Kimi/MiniMax upper)
_PRICE_OUT_PER_1M = 1.20  # $/1M output tokens
_per_feature_tokens: dict[str, dict[str, int]] = {}


def _add_feature_tokens(feature_id: str, input_tokens: int, output_tokens: int) -> float:
    """Accumulate tokens for a feature and return the running $ cost.
    Zero if inputs are zero (nothing to add)."""
    if input_tokens <= 0 and output_tokens <= 0:
        return 0.0
    bucket = _per_feature_tokens.setdefault(feature_id, {"in": 0, "out": 0})
    bucket["in"] += max(0, int(input_tokens))
    bucket["out"] += max(0, int(output_tokens))
    cost = (bucket["in"] / 1_000_000) * _PRICE_IN_PER_1M + (bucket["out"] / 1_000_000) * _PRICE_OUT_PER_1M
    return cost


def _reset_feature_tokens(feature_id: str) -> None:
    _per_feature_tokens.pop(feature_id, None)


def _beat(worker_id: int, feature_id: str) -> None:
    """Workers call this whenever they make meaningful progress (claim,
    ainvoke start/end, verify). A worker that's not beating for
    STUCK_THRESHOLD_SEC is considered wedged."""
    import time as _t
    _worker_heartbeat[worker_id] = (_t.time(), feature_id)


async def _watchdog() -> None:
    """Background task: scan heartbeat dict every 60s, log stuck workers."""
    from .heartbeat import beat
    import time as _t
    while True:
        beat("watchdog")
        await asyncio.sleep(60)
        now = _t.time()
        for wid, (ts, fid) in list(_worker_heartbeat.items()):
            age = now - ts
            if age > STUCK_THRESHOLD_SEC:
                log.warning(
                    "worker_stuck",
                    worker=wid,
                    feature=fid,
                    age_sec=int(age),
                    threshold=STUCK_THRESHOLD_SEC,
                )


def watchdog_state() -> dict:
    """Snapshot for /stats. Returns each worker's last-heartbeat age
    plus the most recent error (if any)."""
    import time as _t
    now = _t.time()
    out = {}
    for wid, (ts, fid) in _worker_heartbeat.items():
        entry = {"feature": fid, "age_sec": int(now - ts)}
        err = _worker_last_error.get(wid)
        if err is not None:
            err_ts, err_fid, err_msg = err
            entry["last_error"] = {
                "feature_id": err_fid,
                "error": err_msg,
                "age_sec": int(now - err_ts),
            }
        out[str(wid)] = entry
    return out


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


def _resume_context(feature: Feature) -> str:
    """If a feature's worktree already has commits or uncommitted work from
    a prior timed-out session, surface a RESUME block so the agent picks
    up where the last attempt left off instead of starting from scratch
    (research #3821 — session-resume patterns).

    Best-effort: failures are silenced; absence of a resume block is fine.
    """
    import subprocess
    from pathlib import Path as _P
    branch = f"feat/{feature.id}"
    sections: list[str] = []
    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        wt = _P(repo_path).parent / f"worktrees-{_P(repo_path).name}" / branch
        if not wt.exists():
            continue
        try:
            base = "develop" if repo_name == "hearth" else "main"
            shortstat = subprocess.run(
                ["git", "diff", "--shortstat", f"{base}...HEAD"],
                cwd=str(wt), capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
            names = subprocess.run(
                ["git", "diff", "--name-only", f"{base}...HEAD"],
                cwd=str(wt), capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip().splitlines()
            uncommitted = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(wt), capture_output=True, text=True, timeout=10, check=False,
            ).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            continue
        if not shortstat and not uncommitted:
            continue
        sections.append(
            f"- repo: {repo_name} (worktree: {wt})\n"
            f"    vs {base}: {shortstat or '(no committed diff)'}\n"
            f"    files touched: {', '.join(names[:8])}{'...' if len(names) > 8 else ''}\n"
            f"    uncommitted: {'yes' if uncommitted else 'no'}"
        )
    if not sections:
        return ""
    return (
        "\n\n⏩ RESUME CONTEXT: a prior attempt left partial work in your "
        "worktree(s). Your FIRST move should be ``git_status`` on each "
        "worktree to read the current state, then either continue the "
        "existing work or explicitly ``git reset --hard origin/BASE`` if "
        "the previous attempt was wrong. Do NOT blindly re-implement; "
        "reusing a half-finished attempt is cheaper than starting over.\n\n"
        + "\n".join(sections)
        + "\n"
    )


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
    resume_block = _resume_context(feature)

    if fixup:
        # Breaking-change prompt (research #3828 Byam template): when the
        # failure reason contains SYMBOL_UNRESOLVED AND the worktree diff
        # touched a lock/manifest file, this is almost certainly a
        # post-bump API break. Re-prompt with minimal-fix framing rather
        # than generic "try again."
        is_breaking_change = (
            "SYMBOL_UNRESOLVED" in fixup or "undefined:" in fixup
            or "Cannot find name" in fixup or "ModuleNotFoundError" in fixup
        )
        if is_breaking_change:
            return f"""Breaking-change fix for feature ``{feature.id}``.

A dependency bump introduced API calls that no longer resolve. The
verifier output below shows the specific file:line and compile error.
Fix strategy — ONLY these:

  1. Read the actual new version's docs/source to find the replacement
     API. DO NOT regenerate the same made-up name under a different
     import path.
  2. Apply the MINIMAL fix: change the import or call site, nothing else.
     Do not refactor unrelated code in the same commit.
  3. Preserve existing functionality exactly — the bump is an upgrade,
     not a redesign.
  4. Run ``verify_staged`` before committing; iterate if still red.

Verifier output:
{fixup}

Target repos: {repos}
Repo paths:
{repo_paths}
{resume_block}{conventions_block}"""

        return f"""Your previous attempt at feature ``{feature.id}`` failed verification.

Reason: {fixup}

Fix ONLY what caused the failure. Do not re-implement. Do not revert unrelated
changes. Run the tests again in the worktree and push when green using ``git_push``. If the same
failure recurs, report it as blocked rather than looping.

Target repos: {repos}
Repo paths:
{repo_paths}
{resume_block}{conventions_block}"""

    # Refactor-kind features: characterize-first (research #3832). Before
    # touching production code, pin existing behavior with characterization
    # tests. If the behavior can't be pinned in 2 attempts, escalate as
    # CHARACTERIZATION_IMPOSSIBLE (rewrite candidate, not refactor).
    if feature.kind == "refactor":
        accept = feature.acceptance_criteria or "characterization tests still pass after refactor"
        return f"""Refactor task ``{feature.id}``.
{resume_block}
Name: {feature.name}
Priority: {feature.priority}
Target repos: {repos}

Repo paths on disk:
{repo_paths}

What to refactor:
{feature.description}

Acceptance:
  {accept}

Refactor workflow (follow in order — NO SHORTCUTS):

  Phase A — Characterize current behavior:
    1. Read the code being refactored. Identify the externally-visible
       contract (public functions, API endpoints, CLI output, etc.).
    2. Use ``scaffold_test_file`` to create a characterization test per
       contract point. These tests pin CURRENT behavior, even quirky
       behavior — don't fix bugs here, just capture what exists.
    3. Run tests; they MUST pass green against the un-refactored code
       before you proceed. If you can't get them green in 2 rounds,
       stop and report ``BLOCKED: CHARACTERIZATION_IMPOSSIBLE — this
       code needs a rewrite, not a refactor``.

  Phase B — Refactor inside the seams:
    1. Change the internals only. Characterization tests stay unchanged.
    2. If a characterization test starts failing, you've broken behavior
       — revert and rethink, don't "fix" the test.
    3. Commit in small increments so each one can be bisected.

  Phase C — Self-audit + commit as normal (Phase 4.5 applies).

Research topics:
  - {research}
{memory_prefix}{conventions_block}"""

    # Bug features run a reproduce-first workflow (research #3803). The
    # developer must see repro_command fail BEFORE writing any fix code —
    # otherwise we ship "fixes" that don't actually address the bug.
    if feature.kind == "bug":
        accept = feature.acceptance_criteria or "repro_command exits 0 with a new regression test committed"
        return f"""Fix bug ``{feature.id}``.
{resume_block}
Name: {feature.name}
Priority: {feature.priority}
Target repos: {repos}

Repo paths on disk:
{repo_paths}

Symptom / description:
{feature.description}

Reproduction command (run this FIRST; it MUST fail or the bug report is wrong):
  {feature.repro_command or '(no repro_command provided — ask the orchestrator to document one before proceeding)'}

Acceptance criterion:
  {accept}

Bug workflow (follow in order):
  1. Reproduce: cd to the appropriate worktree; run the repro_command and
     CONFIRM it fails. If it passes, the bug is already fixed — report
     ``BLOCKED: cannot reproduce, bug likely stale`` and stop.
  2. Regression test: write a minimal test that fails for the same reason.
     Use ``scaffold_test_file`` if the test file doesn't exist yet.
  3. Fix: edit production code until the repro_command + the new
     regression test both pass. Keep the fix minimal — no refactoring
     alongside.
  4. Self-audit (Phase 4.5 still applies) then commit + push.

Research topics to check wikidelve for first:
  - {research}
{memory_prefix}{conventions_block}"""

    # heal_hint comes from healer.py — a targeted instruction reflecting the
    # specific verify failure last time. Pasting it at the TOP makes it the
    # first thing the orchestrator reads, so the next attempt can't blindly
    # repeat the same failure mode (the 7/9 'no commits' cluster we saw).
    heal_block = f"\n\n{feature.heal_hint}\n" if feature.heal_hint else ""

    accept_block = f"\nAcceptance criterion: {feature.acceptance_criteria}\n" if feature.acceptance_criteria else ""
    return f"""Implement feature ``{feature.id}``.
{heal_block}{resume_block}
Name: {feature.name}
Priority: {feature.priority}
Discord parity: {feature.discord_parity}
Target repos: {repos}

Repo paths on disk:
{repo_paths}

Description:
{feature.description}
{accept_block}
Research topics to check wikidelve for first:
  - {research}

Follow the orchestrator workflow: search → plan → worktree per repo → delegate
to ``developer`` → verify with ``git_status`` → delegate to ``reviewer`` →
commit on approval. Skip PR creation if implementation produced zero file changes.
{memory_prefix}{conventions_block}"""


def _worker_affinity_score(worker_id: int, kind: str) -> float:
    """Historical win-rate of a worker on features of this kind.

    Walks the transition log backwards, finds terminal verdicts on
    features whose kind matches, and correlates with the worker that
    processed them (via attempts.jsonl). Returns done/total as a
    float in [0, 1]. When sample is thin (<3) returns 0.5 (neutral).

    Cheap heuristic — runs only at claim time, bounded lookup. No ML.
    """
    from pathlib import Path as _P
    import json as _json
    # Quick: bail if log doesn't exist yet.
    transitions_path = _P("/data/transitions.jsonl")
    attempts_path = _P("/data/attempts.jsonl")
    if not (transitions_path.exists() and attempts_path.exists()):
        return 0.5
    # feature_id → last terminal verdict
    terminal: dict[str, str] = {}
    try:
        for line in transitions_path.read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                t = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if (t.get("to") or "") in ("done", "blocked"):
                terminal[t.get("feature_id", "")] = t["to"]
    except OSError:
        return 0.5
    # feature_id → worker_id (from attempts.jsonl)
    feature_worker: dict[str, int] = {}
    try:
        for line in attempts_path.read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                a = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            fid = a.get("feature_id", "")
            # attempts.jsonl doesn't record worker_id today — left as 0.
            # When that's added this heuristic becomes useful.
            feature_worker.setdefault(fid, int(a.get("worker", 0)))
    except OSError:
        return 0.5
    # Match against the current backlog to filter by kind.
    done = blocked = 0
    # Note: without access to backlog here, we approximate by inspecting
    # the feature_id prefix (bug-*, schema-*, etc) + the terminal verdict.
    # This is rough but surfaces affinity where kind is in the id.
    for fid, verdict in terminal.items():
        if feature_worker.get(fid) != worker_id:
            continue
        if kind not in fid and kind != "feature":  # weak filter
            continue
        if verdict == "done":
            done += 1
        else:
            blocked += 1
    total = done + blocked
    if total < 3:
        return 0.5
    return done / total


async def _claim_next(backlog: Backlog, worker_id: int = 0) -> Feature | None:
    """Atomically pick the next pending feature and mark it implementing.

    Worker-affinity rule: self-improvement features (which all touch
    prompts.py) are pinned to worker 0. Other workers ignore them
    entirely. Combined with the existing _self_improv_active counter
    this gives belt-and-suspenders safety against parallel edits to
    the shared prompts.py file.

    Secondary affinity: when multiple features at the same priority
    are pending, prefer the one this worker has the highest historical
    win-rate on (by kind). See _worker_affinity_score. No effect on
    an empty history.

    Holds ``_CLAIM_LOCK`` across the read+write so two concurrent workers
    can never grab the same feature.

    Before returning, runs the splitter: any candidate targeting multiple
    repos is replaced with per-repo children and we re-select. Prevents
    the "one attempt implements everything across 3 repos and blows
    through the diff cap" failure mode.
    """
    from .splitter import maybe_split

    global _self_improv_active
    async with _CLAIM_LOCK:
        for _ in range(len(backlog.features) + 1):
            candidates = [f for f in backlog.features if f.status == "pending"]
            # Worker-affinity: only worker 0 picks up self-improvement work.
            if worker_id != 0:
                candidates = [f for f in candidates if not f.self_improvement]
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
    if _read_only_mode:
        log.debug("loop_idle", reason="read_only_mode")
        return False
    feature = await _claim_next(backlog, worker_id=worker_id)
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
    # failures. Env-configurable (``MAX_FIXUPS`` → settings.max_fixups) so
    # ops can dial it up when quota is plentiful or down under pressure
    # without a code push. Still aborts on loop-signature deadlock (same
    # reason twice) so we don't infinitely spin.
    MAX_FIXUPS = settings.max_fixups

    def _detect_exploratory_spiral(messages: list) -> str | None:
        """Post-ainvoke check: did the agent waste the session on stuck
        patterns? Returns a reason string when a spiral is detected, else
        None. Research #3811 (real-time-stuck-state-detection) recommends
        three signals, all implemented here:

        1. IDENTICAL-CONSECUTIVE: same tool+args hashed 3+ times in a row
           (Kimi/MiniMax-tuned threshold; research says drop by 1-2 vs GPT-4).
        2. A-B-A-B OSCILLATION: pattern [X, Y, X, Y] with the same two
           hashes alternating 4+ times — classic re-read-then-re-read cycle.
        3. READ-ONLY SPIRAL: ≥6 reads with 0 writes (original heuristic,
           kept for backwards compat).

        Tool calls are fingerprinted as sha256(name + sorted-json args)[:12]
        so arg-identical calls match even when argument order drifts.
        """
        import hashlib
        import json as _json
        reads = 0
        writes = 0
        fingerprints: list[str] = []

        def _fp(name: str, args: dict) -> str:
            try:
                blob = name + _json.dumps(args or {}, sort_keys=True, default=str)[:500]
            except (TypeError, ValueError):
                blob = name + repr(args)[:500]
            return hashlib.sha256(blob.encode()).hexdigest()[:12]

        for m in messages or []:
            tcs = getattr(m, "tool_calls", None) or []
            for tc in tcs:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
                fingerprints.append(_fp(name, args))
                if name in ("read_file", "repo_search", "git_status"):
                    reads += 1
                elif name in ("write_file", "edit_file", "git_commit", "scaffold_test_file"):
                    writes += 1

        # 1. Identical-consecutive (threshold 3 for Kimi/MiniMax).
        for i in range(len(fingerprints) - 2):
            if fingerprints[i] == fingerprints[i + 1] == fingerprints[i + 2]:
                return f"exploratory_spiral: identical tool call 3x in a row ({fingerprints[i]})"
        # 2. A-B-A-B oscillation (threshold 4 alternations of same two hashes).
        for i in range(len(fingerprints) - 3):
            a, b, c, d = fingerprints[i], fingerprints[i + 1], fingerprints[i + 2], fingerprints[i + 3]
            if a == c and b == d and a != b:
                return f"exploratory_spiral: A-B-A-B oscillation between {a[:6]} and {b[:6]}"
        # 3. Read-only spiral (original heuristic).
        if reads >= 6 and writes == 0:
            return f"exploratory_spiral: {reads} reads, 0 writes"
        return None
    FIXABLE_PREFIXES = ("tests failed", "diff too large", "committed locally", "complexity too high", "planner_undercount", "no test file in diff", "exploratory_spiral")
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
            _beat(worker_id, feature.id)
            _note_request(active_provider)
            # Reset the per-attempt rescue flag so we know whether
            # THIS attempt needed auto-commit or the agent committed
            # on its own (research #3810's audit signal).
            attempt_rescued = False
            import time as _time_attempt
            _attempt_started = _time_attempt.time()
            result = await asyncio.wait_for(
                active.ainvoke(
                    {"messages": [{"role": "user", "content": prompt}]},
                    config={
                        "metadata": {
                            "feature_id": feature.id,
                            "feature_name": feature.name,
                            "worker": worker_id,
                            "attempt": attempt,
                            "provider": active_provider,
                        },
                        "tags": [f"feature:{feature.id}", f"worker:{worker_id}", active_provider],
                    },
                ),
                timeout=settings.per_feature_timeout_sec,
            )
            _beat(worker_id, feature.id)
            # Record token usage for the multi-dim rate-limit predictor.
            # Zero counts (e.g. when LangChain wraps a streaming response
            # without aggregating usage) are silently skipped.
            in_tok, out_tok = _extract_token_usage(result)
            _note_usage(active_provider, in_tok, out_tok)
            # Per-feature spend cap: abort the iterate loop when the
            # accumulated cost for THIS feature crosses the configured
            # budget. Caps doomed features from burning through quota
            # across 6 fixups (research note: ~90% of retries don't
            # recover; we already bail on unsolvable signals, now we
            # also bail on cost alone).
            running_cost = _add_feature_tokens(feature.id, in_tok, out_tok)
            # Per-feature override beats the global default; falls through
            # to settings.per_feature_budget_usd otherwise.
            effective_budget = (
                feature.budget_usd if getattr(feature, "budget_usd", 0.0) > 0
                else settings.per_feature_budget_usd
            )
            if effective_budget > 0 and running_cost >= effective_budget:
                log.warning(
                    "feature_budget_exhausted",
                    id=feature.id,
                    spent_usd=round(running_cost, 3),
                    budget_usd=effective_budget,
                    override=getattr(feature, "budget_usd", 0.0) > 0,
                    attempt=attempt,
                )
                reason = f"budget_exhausted: spent ${running_cost:.3f} of ${effective_budget:.2f}"
                verdict = "blocked"
                break
            # Persist this attempt's tool-call fingerprint log so future
            # debugging-session-replay tooling (research #3807) has the
            # raw trace to work against. Cheap append-only JSONL.
            _append_attempt_log(
                feature.id, attempt, active_provider, result, in_tok, out_tok,
                duration_sec=_time_attempt.time() - _attempt_started,
                worker_id=worker_id,
                prompt_sha=_snapshot_prompt(prompt),
            )
            last = result["messages"][-1].content if result.get("messages") else ""
            claimed = "blocked" if _agent_self_reports_blocked(last) else "done"
            # Rescue stray diffs: if the agent wrote files in a worktree but
            # never committed, auto-commit them now so the iterate loop has
            # something to verify. Turns the dominant "no commits" failure
            # mode into a recoverable "tests maybe fail" one.
            attempt_rescued = _rescue_uncommitted_worktrees(feature)
            ok, reason = verify_changes(feature)
            # Overlay stuck-state detection ONLY when verify fails — a
            # spiral that still produced passing diff + tests is a success.
            # This catches the "agent read 12 files, wrote nothing, still
            # claimed done" pattern that burns retries on nothing.
            if not ok:
                spiral = _detect_exploratory_spiral(result.get("messages") or [])
                if spiral:
                    reason = f"{feature.repos[0] if feature.repos else 'feature'}: {spiral}"
                    log.warning("exploratory_spiral_detected", feature=feature.id, reason=spiral)
            verdict = claimed if (claimed == "blocked" or ok) else "blocked"
            if verdict == "done":
                break
            # Substring match (not startswith) — verify_changes prefixes each
            # reason with the repo name (e.g. "hearth: tests failed: …"), so
            # startswith would never match. This has silently been preventing
            # retries on test failures for a while; substring is what the
            # heal_hint code uses and works correctly.
            if not any(p in reason for p in FIXABLE_PREFIXES):
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

        # Prepend a rescue-signal tag so transitions.jsonl can distinguish
        # features that the agent committed itself from features that only
        # landed because _rescue_uncommitted_worktrees auto-committed
        # their stray diff. Rubber-stamping ratio is a useful diagnostic.
        status_reason = (f"rescued={attempt_rescued} " if attempt_rescued else "") + (reason or "")
        backlog.set_status(feature.id, verdict, reason=status_reason[:500])
        _record_verdict(verdict, provider=active_provider)
        # Reset the per-feature token accumulator once a terminal
        # verdict is recorded — next run of the same feature ID (via
        # re-queue or healer) gets a fresh budget.
        _reset_feature_tokens(feature.id)
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
            # Auto-open a PR per repo the feature touched so the kanban's
            # "branch" link becomes a reviewable PR instead of a raw branch
            # the operator has to manually turn into one. Fire-and-forget —
            # failures (no token, already-exists) log and continue. Runs in
            # a thread so we don't block the event loop on the HTTPS call.
            try:
                from pathlib import Path as _P
                from .tools.git_ops import open_pr_if_possible
                branch = f"feat/{feature.id}"
                attempts_line = "first try" if attempt == 0 else f"{attempt + 1} attempts"
                # Human-reviewable PR body — reads like a human-authored
                # description rather than a template dump. Priority + repos
                # are easy to scan, planner estimate gives scope context,
                # verifier output proves tests passed.
                # Changelog-grouped body (research #3834): parse the
                # commits on this branch, group by conventional-commit
                # type, drop chore/ci/docs noise. Falls through cleanly
                # when commits aren't conventional.
                changelog_section = ""
                try:
                    from .commitlint import parse as _parse_cc, render_changelog
                    import subprocess as _sp
                    from pathlib import Path as _P
                    first_wt = None
                    for repo_name in feature.repos:
                        rp = settings.repo_paths.get(repo_name)
                        if not rp:
                            continue
                        wt = _P(rp).parent / f"worktrees-{_P(rp).name}" / f"feat/{feature.id}"
                        if wt.exists():
                            first_wt = str(wt)
                            break
                    if first_wt:
                        base = "develop" if any("hearth" == r for r in feature.repos) else "main"
                        log_out = _sp.run(
                            ["git", "log", f"{base}..HEAD", "--format=%B%x1e"],
                            cwd=first_wt, capture_output=True, text=True, timeout=15, check=False,
                        ).stdout
                        raws = [c for c in log_out.split("\x1e") if c.strip()]
                        parsed_list = [p for p in (_parse_cc(r) for r in raws) if p is not None]
                        rendered = render_changelog(parsed_list)
                        if rendered:
                            changelog_section = f"\n## Changelog\n\n{rendered}\n"
                except Exception as e:  # noqa: BLE001
                    log.warning("pr_changelog_skipped", err=str(e)[:120])
                risk_line = (
                    f"- ⚠️ Risk tier: `{feature.risk_tier}` — "
                    f"{'HUMAN APPROVAL REQUIRED' if feature.risk_tier == 'high' else 'draft, review before undraft' if feature.risk_tier == 'medium' else 'auto-merge eligible'}\n"
                    if getattr(feature, 'risk_tier', 'low') != 'low' else ""
                )
                kind_line = f"- Kind: `{feature.kind}`\n" if feature.kind != "feature" else ""
                pr_body = (
                    f"Autonomous implementation of **{feature.name}**.\n\n"
                    f"### Description\n{feature.description}\n\n"
                    f"### Metadata\n"
                    f"- Priority: `{feature.priority}`\n"
                    f"- Repos: {', '.join(f'`{r}`' for r in feature.repos)}\n"
                    f"- Feature ID: `{feature.id}`\n"
                    f"{kind_line}"
                    f"{risk_line}"
                    f"- Worker: `w{worker_id}` · {attempts_line}\n"
                    + (f"- Planner estimate: {feature.planner_estimate_lines} lines\n"
                       if getattr(feature, 'planner_estimate_lines', 0) else '')
                    + (f"- Parent feature: `{feature.parent_id}`\n"
                       if getattr(feature, 'parent_id', '') else '')
                    + f"\n### Verifier output\n```\n{reason[:500]}\n```\n"
                    f"{changelog_section}"
                    f"\n_Generated by hearth-agents · review before merge._"
                )
                pr_title = f"feat: {feature.name}"
                for repo_name in feature.repos:
                    repo_path = settings.repo_paths.get(repo_name)
                    if not repo_path:
                        continue
                    wt = _P(repo_path).parent / f"worktrees-{_P(repo_path).name}" / branch
                    target = str(wt) if wt.exists() else repo_path
                    await asyncio.to_thread(
                        open_pr_if_possible, target, branch, pr_title, pr_body
                    )
            except Exception as e:  # noqa: BLE001
                log.warning("auto_pr_exception", id=feature.id, err=str(e)[:200])
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
        _record_worker_error(worker_id, feature.id, f"timeout after {settings.per_feature_timeout_sec}s")
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
            _record_worker_error(worker_id, feature.id, f"{type(e).__name__}: {e}"[:200])
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
        # Per-provider circuit breakers: quality collapse on ONE provider
        # shouldn't pause the loop if the other is fine. We just remove the
        # bad provider from selection until its cooldown expires.
        primary_quarantined = _check_provider_circuit("primary")
        fallback_quarantined = _check_provider_circuit("fallback")
        primary_cool = _primary_cooldown_until > now or primary_quarantined
        fallback_cool = (
            fallback_agent is not None
            and (_fallback_cooldown_until > now or fallback_quarantined)
        )
        # Preemptive rate-limit throttle: if we're approaching the advertised
        # limit, sleep briefly to let the window slide. Avoids the 429 →
        # cooldown flip-flop that spikes tail latency. Uses recent request
        # counts tracked in _request_ticks (see _note_request below).
        _throttle_for_rate_approach(worker_id)

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

        # Operator override: settings.force_provider pins every call
        # to one model. Bypasses ping-pong + cooldowns — if force=fallback
        # and fallback is cooled, we STILL use fallback (operator's
        # explicit choice). Useful for A/B ("is kimi the problem?") or
        # quota-conservation lockdown.
        force = (settings.force_provider or "").strip().lower()
        if force in ("primary", "fallback"):
            use_fallback = force == "fallback" and fallback_agent is not None
            reason = f"force_provider={force}"
        # Provider selection:
        #  1. If one is cooled, use the other.
        #  2. If both are healthy and fallback is configured, ping-pong on a
        #     shared counter. Spreads load 50/50 across providers instead of
        #     dogpiling whichever is nominally "primary".
        elif primary_cool and fallback_agent is not None and not fallback_cool:
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


def _auto_rerun_on_new_prompts(backlog: Backlog) -> int:
    """When prompts_version differs from the version recorded against
    each escalated/blocked feature's last terminal transition, flip
    those features back to pending so the new prompts get a swing at
    them. Idempotent — only flips when transition history actually
    shows an older version. Returns the count flipped."""
    from .transitions import prompts_version, read_tail
    current = prompts_version()
    last_version_per_feature: dict[str, str] = {}
    for t in read_tail(limit=20000):
        v = t.get("prompts_version") or ""
        fid = t.get("feature_id") or ""
        if not (fid and v):
            continue
        last_version_per_feature[fid] = v  # chronological, last wins
    flipped = 0
    for f in backlog.features:
        if f.status != "blocked":
            continue
        last_v = last_version_per_feature.get(f.id, "")
        if not last_v or last_v == current:
            continue
        # Reset heal_attempts so the loop / healer can re-pick. Keep the
        # heal_hint so the new prompts inherit the prior failure context.
        f.heal_attempts = 0
        backlog.set_status(
            f.id, "pending",
            reason=f"auto_rerun_on_prompts_change {last_v}->{current}",
            actor="loop",
        )
        flipped += 1
    if flipped:
        backlog.save()
        log.info("auto_rerun_flipped", count=flipped, current_version=current)
    return flipped


async def _autoscaler(
    backlog: Backlog,
    worker_tasks: dict[int, asyncio.Task],
    spawn: Any,
) -> None:
    """Grow/shrink the worker pool based on pending backlog depth.

    Ceiling = settings.loop_workers_max or settings.loop_workers.
    Floor   = settings.loop_workers_min (default 1).

    Scale-up: pending >= high_water AND current < ceiling → spawn one.
    Scale-down: pending < low_water AND current > floor → cancel one
    (picks the newest to keep long-running workers stable).

    Runs every 60s. Idempotent; stays at current size when neither
    condition fires.
    """
    ceiling = max(1, settings.loop_workers_max or settings.loop_workers)
    floor = max(1, min(settings.loop_workers_min, ceiling))
    hi = settings.loop_autoscale_high_water
    lo = settings.loop_autoscale_low_water
    if floor == ceiling:
        return  # autoscaling disabled
    from .heartbeat import beat
    while True:
        beat("autoscaler")
        await asyncio.sleep(60)
        pending = sum(1 for f in backlog.features if f.status == "pending")
        current = len(worker_tasks)
        if pending >= hi and current < ceiling:
            wid = max(worker_tasks.keys(), default=-1) + 1
            worker_tasks[wid] = asyncio.create_task(spawn(wid))
            log.info("autoscale_up", worker=wid, current=current + 1, pending=pending)
        elif pending < lo and current > floor:
            # Drop the newest-spawned worker to preserve long-running context.
            drop = max(worker_tasks.keys())
            worker_tasks[drop].cancel()
            worker_tasks.pop(drop, None)
            log.info("autoscale_down", worker=drop, current=current - 1, pending=pending)
        # Prune tasks that ended on their own (exception, etc).
        for wid in list(worker_tasks):
            if worker_tasks[wid].done():
                worker_tasks.pop(wid)


async def run_forever(backlog: Backlog, agent: Any, fallback_agent: Any | None = None) -> None:
    """Main loop. Runs until cancelled. Shares state with the HTTP server and bot.

    Initial worker pool size is ``settings.loop_workers``. When
    ``loop_workers_max > loop_workers_min`` the pool autoscales in
    [min, max] based on pending depth.
    """
    initial = max(1, settings.loop_workers)
    log.info("loop_started", interval_sec=LOOP_INTERVAL_SEC, workers=initial, stats=backlog.stats())
    notifier = Notifier()
    await notifier.send(f"🔥 hearth-agents loop started — workers={initial} {backlog.stats()}")

    try:
        flipped = _auto_rerun_on_new_prompts(backlog)
        if flipped:
            await notifier.send(f"♻️ auto-rerun: {flipped} blocked features flipped to pending on prompts version change")
    except Exception as e:  # noqa: BLE001
        log.warning("auto_rerun_failed", err=str(e)[:200])

    def _spawn(wid: int) -> Any:
        return _worker(wid, backlog, agent, notifier, fallback_agent)

    worker_tasks: dict[int, asyncio.Task] = {
        i: asyncio.create_task(_spawn(i)) for i in range(initial)
    }
    try:
        await asyncio.gather(
            _watchdog(),
            _autoscaler(backlog, worker_tasks, _spawn),
            *worker_tasks.values(),
            return_exceptions=True,
        )
    finally:
        for t in worker_tasks.values():
            if not t.done():
                t.cancel()
        await notifier.close()

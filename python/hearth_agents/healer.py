"""Auto-heal blocked features by flipping them back to pending.

A feature can end up ``blocked`` for many reasons — transient provider outage,
flaky test, timeout, an agent's bad mood. Instead of having humans resurrect
them by hand, the healer periodically resets blocked features to ``pending``
so the loop retries them.

Each reset increments ``feature.heal_attempts``; once the cap is reached the
feature stays ``blocked`` and a Telegram message asks the human to look. The
per-session fixup loop in ``loop.py`` handles tight in-run retries; the healer
handles longer-horizon reincarnation across worker cycles.
"""

from __future__ import annotations

import asyncio

from .backlog import Backlog
from .logger import log
from .notify import Notifier
from .loop import _rescue_uncommitted_worktrees
from .verify import verify_changes

HEAL_INTERVAL_SEC = 300  # scan every 5 minutes
HEAL_MAX_ATTEMPTS = 3    # after this many resets we stop and escalate
HEAL_COOLDOWN_SEC = 600  # skip features blocked less than this long — give in-run fixup a chance


def _retry_push(feature) -> None:  # type: ignore[no-untyped-def]
    """Attempt a raw ``git push -u origin HEAD`` from each of the feature's
    worktrees. Called by the healer when a feature is blocked for 'never
    pushed' — the orchestrator's git_commit tool should have pushed, but
    auth hiccups or transient network blips sometimes leave commits local.

    Best-effort: failures are logged and swallowed. Never raises.
    """
    import subprocess
    from pathlib import Path as _P
    from .config import settings
    branch = f"feat/{feature.id}"
    for repo_name in feature.repos:
        repo_path = settings.repo_paths.get(repo_name)
        if not repo_path:
            continue
        wt = _P(repo_path).parent / f"worktrees-{_P(repo_path).name}" / branch
        if not wt.exists():
            continue
        try:
            r = subprocess.run(
                ["git", "push", "-u", "origin", "HEAD"],
                cwd=str(wt), capture_output=True, text=True, timeout=60, check=False,
            )
            log.info(
                "healer_push_retry",
                feature=feature.id,
                repo=repo_name,
                ok=(r.returncode == 0),
                err=(r.stderr[:160] if r.returncode != 0 else ""),
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("healer_push_retry_error", feature=feature.id, err=str(e)[:200])


_ADVERSARIAL_PREFIX = (
    "PRIOR FAILURE. Treat this attempt as a fresh audit, not a continuation.\n"
    "Before writing any code, run the 5-category self-audit on the CURRENT\n"
    "worktree state (MISSING / WRONG / DANGEROUS / UNTYPED / TESTGAP) per\n"
    "the developer prompt Phase 4.5. Empty categories must carry an explicit\n"
    "why_covered string; empty audit is a failing audit.\n\n"
)


def _hint_for_reason(reason: str) -> str:
    """Translate a verify_changes reason into a targeted instruction the
    next-attempt prompt can paste in. Empty string when we have no specific
    advice (the agent then runs with the normal prompt).

    Every non-empty hint is prefixed with the adversarial-audit reminder
    (research #3812) — the healer fires on features that have already
    failed at least once, and the adversarial framing is exactly what
    the soft self-review couldn't catch.
    """
    r = reason.lower()

    def _wrap(body: str) -> str:
        return _ADVERSARIAL_PREFIX + body

    if "no commits" in r:
        return _wrap(
            "PRIOR FAILURE: you opened a worktree last time and never committed "
            "anything. Do NOT enter another exploratory read-only spiral. After "
            "at most 6 reads, start writing with edit_file/write_file. End the "
            "session with either (a) a real git_commit on the feature branch or "
            "(b) a single message saying 'BLOCKED: <one concrete blocker>' and "
            "no commit. Both are acceptable; abandoning silently is not."
        )
    if "diff too large" in r:
        return _wrap(
            "PRIOR FAILURE: your diff exceeded the 600-line cap. This time, "
            "implement only the minimum viable slice that satisfies the feature "
            "name; defer secondary concerns to follow-up features. Target "
            "<300 lines of diff. If the feature genuinely cannot be sliced, "
            "report 'BLOCKED: needs decomposition' rather than over-shipping."
        )
    if "planner_undercount" in r:
        return _wrap(
            "PRIOR FAILURE: actual diff exceeded planner's estimate by >1.5x. "
            "The planner under-estimated the scope. This time, have the planner "
            "either (a) raise estimated_diff_lines to a realistic value, or "
            "(b) split the feature into per-concern sub-features BEFORE delegating. "
            "Do not re-run the same plan hoping for a smaller diff."
        )
    if "no test file in diff" in r:
        return _wrap(
            "PRIOR FAILURE: the diff contained zero test files. You shipped "
            "production code but no regression coverage. This time you MUST "
            "create or modify at least one test file per language convention "
            "(Go: *_test.go in the same package; TS/Svelte: *.test.ts co-"
            "located; Python: tests/test_*.py). Use scaffold_test_file if "
            "the test file doesn't exist yet. The test must actually exercise "
            "the new behavior — a test that only asserts True passes but "
            "catches nothing."
        )
    if "tests failed" in r:
        excerpt = reason.split("tests failed:", 1)[-1].strip()[:300] if "tests failed:" in reason else ""
        detail = f" Failing output excerpt:\n  {excerpt}\n\n" if excerpt else " "
        return _wrap(
            "PRIOR FAILURE: the test suite (or compile step) failed last attempt."
            + detail +
            "Fix exactly these errors before re-running. Do NOT re-plan the "
            "feature — the code is mostly right; just fix the specific errors "
            "above. Run verify_staged locally BEFORE the final commit."
        )
    if "never pushed" in r:
        return _wrap(
            "PRIOR FAILURE: you committed locally but never pushed. End with a "
            "git push -u origin HEAD and verify with git ls-remote --heads origin."
        )
    if "exploratory_spiral" in r:
        return _wrap(
            "PRIOR FAILURE: you spent the entire last session reading files "
            "without writing anything. Rules this attempt: (1) max 4 reads "
            "before your first write, (2) never re-read the same file twice, "
            "(3) if after 4 reads you still can't write, report "
            "'BLOCKED: <concrete reason>' and exit."
        )
    if "budget_exhausted" in r:
        return _wrap(
            "PRIOR FAILURE: the previous attempt burned through the per-feature "
            "token budget without shipping. Cut scope aggressively. Pick ONE "
            "acceptance condition and ignore everything else; follow-up features "
            "can handle the rest. If the feature fundamentally can't be done "
            "cheaply, report 'BLOCKED: needs-decomposition-or-budget-increase'."
        )
    return ""


async def run_healer(backlog: Backlog) -> None:
    """Background task: periodically resurrect blocked features."""
    from .heartbeat import beat
    notifier = Notifier()
    log.info("healer_started", interval_sec=HEAL_INTERVAL_SEC, max_attempts=HEAL_MAX_ATTEMPTS)
    try:
        while True:
            beat("healer")
            healed: list[str] = []
            escalated: list[str] = []
            for f in list(backlog.features):
                if f.status != "blocked":
                    continue
                if f.heal_attempts >= HEAL_MAX_ATTEMPTS:
                    # Only escalate once — track by marking a dummy status bump.
                    # For now we just log; human intervention is the ask.
                    continue
                # Run the rescue FIRST — if the feature has uncommitted work
                # in its worktree (timeout path abandoned it), commit + push
                # it before we even call verify. A substantive rescue commit
                # can flip the feature from 'blocked' to 'done' this cycle.
                try:
                    _rescue_uncommitted_worktrees(f)
                except Exception as e:  # noqa: BLE001
                    log.warning("healer_rescue_failed", id=f.id, err=str(e)[:200])

                # Re-verify to learn WHY this feature is blocked, so the next
                # attempt's prompt can carry a targeted hint instead of just
                # "try again". Without this the agent typically repeats the
                # exact failure mode (the 7/9 'no commits' cluster we saw).
                try:
                    ok, reason = verify_changes(f)
                except Exception as e:  # noqa: BLE001
                    ok, reason = False, f"healer could not re-verify: {e}"

                # If the rescue + push resurrected the feature into a passing
                # state, mark it done directly from the healer. This is the
                # "rescued to green" fast path — previously we'd always reset
                # to pending and force a full dev-subagent retry even when
                # the code was already good.
                if ok:
                    f.status = "done"
                    f.heal_hint = ""
                    healed.append(f"{f.id} -> done (rescued)")
                    log.info("healer_resurrected_done", id=f.id, reason=reason[:120])
                    continue

                # If the only thing stopping us is an unpushed commit, try a
                # raw git push from the healer itself. The orchestrator's
                # git_commit tool should have pushed; if it didn't (auth
                # hiccup, network blip), a bare retry often succeeds.
                if "never pushed" in reason:
                    _retry_push(f)
                    try:
                        ok, reason = verify_changes(f)
                    except Exception as e:  # noqa: BLE001
                        ok, reason = False, f"healer re-verify after push retry failed: {e}"
                    if ok:
                        f.status = "done"
                        f.heal_hint = ""
                        healed.append(f"{f.id} -> done (push-retried)")
                        log.info("healer_push_retry_success", id=f.id)
                        continue

                f.heal_hint = _hint_for_reason(reason)
                # planner_undercount needs the stale estimate cleared — the
                # next planner run has to produce a larger estimate (or split),
                # not re-use the old underestimate that keeps tripping the
                # 1.5x gate. heal_hint already tells the planner what to do;
                # zeroing the field lets an unestimated replan succeed too.
                if "planner_undercount" in reason and getattr(f, "planner_estimate_lines", 0) > 0:
                    f.planner_estimate_lines = 0
                f.heal_attempts += 1
                f.status = "pending"
                healed.append(f"{f.id} (attempt {f.heal_attempts}/{HEAL_MAX_ATTEMPTS})")
                log.info("healer_reset", id=f.id, attempt=f.heal_attempts, reason=reason[:120])

                # Auto-trigger multi-agent debate at the FIRST healer reset
                # (research #3816). With ample MiniMax Max headroom (15k req/5h)
                # the cost of running both models in parallel is cheaper than
                # waiting through more single-model heals. Earlier than the
                # original heal_attempts==2 because we have quota to burn.
                if f.heal_attempts == 1:
                    try:
                        import urllib.request
                        from .config import settings as _s
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{_s.server_port}/features/{f.id}/debate",
                            method="POST",
                        )
                        # Don't await response; debate is expensive (~30-60s).
                        # Fire in a background thread via asyncio.
                        asyncio.create_task(asyncio.to_thread(
                            lambda: urllib.request.urlopen(req, timeout=120).read()
                        ))
                        log.info("healer_auto_debate_triggered", id=f.id, heal_attempts=f.heal_attempts)
                    except Exception as e:  # noqa: BLE001
                        log.warning("healer_auto_debate_failed", id=f.id, err=str(e)[:200])

            # Identify features that just hit the ceiling for escalation.
            for f in backlog.features:
                if f.status == "blocked" and f.heal_attempts == HEAL_MAX_ATTEMPTS:
                    escalated.append(f.id)

            if healed:
                backlog.save()
                # Coalesce healer batch notifications — with 150+ blocked
                # features the loop was firing this every 5 min. An hourly
                # summary is enough signal; raw log keeps per-feature detail.
                await notifier.send_coalesced(
                    "healer_batch",
                    f"🩹 healer reset {len(healed)} blocked this cycle "
                    "(further reset batches suppressed for 1h)",
                )
            if escalated:
                # Bump to prevent re-sending the same escalation every cycle.
                for f in backlog.features:
                    if f.id in escalated:
                        f.heal_attempts += 1  # >MAX_ATTEMPTS now, silences future escalations
                backlog.save()
                # Escalations ARE user-actionable — keep the raw send but still
                # coalesce so 50 escalations don't become 50 pings.
                await notifier.send_coalesced(
                    "healer_escalation",
                    f"🚨 healer giving up on {len(escalated)} feature(s) — human review needed",
                )

            await asyncio.sleep(HEAL_INTERVAL_SEC)
    finally:
        await notifier.close()

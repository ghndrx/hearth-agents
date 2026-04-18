"""Live CI status ingestion for the agent's own PRs.

Research #3801 (live-ci-status-ingestion): our local ``verify_changes``
runs tests in a worktree on gateway-01, but GitHub Actions may enforce
checks we don't run locally (golangci-lint with project-specific rules,
codecov gates, dependency scanners). When Actions fails on a feat/
branch, we want the feature flipped back with the real CI failure in
its heal_hint so the next attempt has targeted context.

Scope: this handler only ACTS on failure conclusions; success is a no-op
(the feature is already done). Skips events that don't match one of our
repos + one of our branch prefixes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .backlog import Backlog
from .config import settings
from .logger import log


async def handle_workflow_run(backlog: Backlog, payload: dict[str, Any]) -> None:
    """Process a ``workflow_run`` webhook payload.

    GitHub fires this twice per run (``requested`` + ``completed``); we only
    care about ``completed`` with a non-success conclusion. If the failing
    run targets a ``feat/<feature_id>`` branch we own, flip that feature
    back to pending with a CI-targeted heal hint.
    """
    action = payload.get("action")
    if action != "completed":
        return
    run = payload.get("workflow_run") or {}
    conclusion = run.get("conclusion")
    if conclusion in (None, "success", "skipped", "cancelled"):
        return

    branch = (run.get("head_branch") or "").strip()
    if not branch.startswith("feat/"):
        return
    feature_id = branch.removeprefix("feat/")
    repo_full = (payload.get("repository") or {}).get("full_name") or ""
    # Only accept events from repos we actually manage. Stops an attacker
    # with a leaked webhook secret from flipping features via a fork.
    if not any(repo_full.endswith(f"/{r}") for r in settings.repo_paths):
        log.info("ci_ingest_skipped_foreign_repo", repo=repo_full, branch=branch)
        return

    feature = next((f for f in backlog.features if f.id == feature_id), None)
    if feature is None:
        log.info("ci_ingest_no_matching_feature", feature_id=feature_id, repo=repo_full)
        return

    # Fetch the failing-job summary so the heal hint carries real context.
    # Best-effort: if the API call fails, we still flip with a generic hint.
    summary = await _failing_jobs_summary(run, repo_full)
    hint = (
        "PRIOR FAILURE: GitHub Actions CI failed on this branch after the "
        "local verifier passed. Investigate the exact CI logs — the local "
        f"test run passed, but remote CI caught something different.\n"
        f"Workflow: {run.get('name', '?')}\n"
        f"Conclusion: {conclusion}\n"
        f"Run URL: {run.get('html_url', '(no url)')}\n"
        f"{summary}"
    )
    # Flip from done -> pending (or blocked -> pending) so the loop picks
    # it up again. Leave other statuses untouched — agent might already be
    # iterating on it.
    if feature.status not in ("done", "blocked"):
        log.info(
            "ci_ingest_feature_not_terminal",
            feature_id=feature_id,
            status=feature.status,
        )
        return
    feature.heal_hint = hint[:2000]
    backlog.set_status(feature_id, "pending", reason=f"ci_failed: {conclusion}", actor="webhook")
    log.info(
        "ci_ingest_feature_reset",
        feature_id=feature_id,
        prev_status=feature.status,
        conclusion=conclusion,
        run_id=run.get("id"),
    )


async def _failing_jobs_summary(run: dict[str, Any], repo_full: str) -> str:
    """Fetch the run's jobs and return a short summary of failing ones.
    Returns an empty string on auth failure or network error — the caller
    still flips the feature; the summary is a nice-to-have."""
    token = settings.github_token
    jobs_url = run.get("jobs_url")
    if not token or not jobs_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                jobs_url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
    except httpx.HTTPError as e:
        log.warning("ci_ingest_jobs_fetch_failed", err=str(e)[:200])
        return ""
    if r.status_code != 200:
        return ""
    jobs = (r.json() or {}).get("jobs") or []
    failing = [j for j in jobs if j.get("conclusion") == "failure"]
    if not failing:
        return ""
    lines = ["Failing jobs:"]
    for j in failing[:3]:
        name = j.get("name", "?")
        # Show the step that failed, if present. Don't fetch step logs —
        # GitHub requires a separate auth-gated download per job and we
        # don't want to chain API calls in a webhook handler.
        failing_step = next(
            (s.get("name") for s in (j.get("steps") or []) if s.get("conclusion") == "failure"),
            None,
        )
        lines.append(f"  - {name}" + (f" (step: {failing_step})" if failing_step else ""))
    return "\n".join(lines)


# asyncio is imported for symmetry with other modules; not directly used
# here but referenced by the test helpers.
_ = asyncio

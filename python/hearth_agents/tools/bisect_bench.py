"""Performance regression bisection.

Research #3829 (autonomous performance regression bisection): given
a benchmark command and a known-good / known-bad SHA range, walk the
range with binary search, run the bench at each midpoint, identify
the offending commit. Returns a summary the agent can use to open
a revert-first PR via the existing auto-PR flow.

Variance-aware: repeats the bench N times per SHA and compares median;
a single noisy run doesn't condemn a commit.
"""

from __future__ import annotations

import statistics
import subprocess
from typing import Any

from langchain_core.tools import tool


def _run(cmd: list[str], cwd: str, timeout: int = 300) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"bench timed out after {timeout}s"
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"


def _checkout(repo_path: str, sha: str) -> tuple[int, str]:
    return _run(["git", "checkout", "--detach", sha], repo_path, timeout=30)


def _extract_metric(output: str, metric_key: str) -> float | None:
    """Pull a numeric metric from bench output. Looks for lines like
    ``<metric_key>: 123.4`` or JSON-y ``"<metric_key>": 123``."""
    import re
    for pat in (
        rf"{re.escape(metric_key)}\s*[:=]\s*([0-9.eE+-]+)",
        rf'"{re.escape(metric_key)}"\s*:\s*([0-9.eE+-]+)',
    ):
        m = re.search(pat, output)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None


@tool
def bisect_bench(
    repo_path: str,
    good_sha: str,
    bad_sha: str,
    bench_command: list[str],
    metric_key: str,
    repeats: int = 3,
    timeout_per_run: int = 300,
) -> str:
    """Binary-search a commit range for a perf regression.

    Args:
        repo_path: repo root (must be clean; tool checks out detached HEAD).
        good_sha: commit that passes the bench target.
        bad_sha: commit that violates the target.
        bench_command: e.g. ``["go", "test", "-bench=.", "-run=^$"]``.
        metric_key: key to extract from bench output (e.g. ``"ns/op"``).
        repeats: bench runs per SHA for variance averaging (default 3).
        timeout_per_run: per-invocation timeout (default 300s).
    """
    code, out = _run(["git", "rev-list", "--reverse", f"{good_sha}..{bad_sha}"], repo_path, timeout=30)
    if code != 0 or not out:
        return f"error: could not list commits between {good_sha} and {bad_sha}: {out[:200]}"
    commits = out.splitlines()
    if not commits:
        return "error: empty commit range"

    def _metric_at(sha: str) -> tuple[float | None, str]:
        _checkout(repo_path, sha)
        values: list[float] = []
        last_out = ""
        for _ in range(repeats):
            c, o = _run(bench_command, repo_path, timeout_per_run)
            last_out = o
            if c != 0:
                continue
            v = _extract_metric(o, metric_key)
            if v is not None:
                values.append(v)
        if not values:
            return None, last_out[-400:]
        return statistics.median(values), ""

    # Anchor metrics.
    good_metric, good_log = _metric_at(good_sha)
    bad_metric, bad_log = _metric_at(bad_sha)
    if good_metric is None or bad_metric is None:
        return f"error: could not measure anchors (good={good_metric}, bad={bad_metric})"
    direction = "higher-is-worse" if bad_metric > good_metric else "lower-is-worse"

    lo, hi = 0, len(commits) - 1
    steps: list[dict[str, Any]] = []
    while lo < hi:
        mid = (lo + hi) // 2
        sha = commits[mid]
        val, _ = _metric_at(sha)
        if val is None:
            steps.append({"sha": sha[:10], "metric": None, "verdict": "skip"})
            lo = mid + 1
            continue
        is_bad = val >= bad_metric if direction == "higher-is-worse" else val <= bad_metric
        steps.append({"sha": sha[:10], "metric": val, "verdict": "bad" if is_bad else "good"})
        if is_bad:
            hi = mid
        else:
            lo = mid + 1
    culprit = commits[lo]
    # Restore to the bad_sha at the end so the worktree isn't on a detached
    # intermediate; the caller is expected to open a revert PR separately.
    _checkout(repo_path, bad_sha)
    summary_steps = "\n".join(
        f"  {s['sha']}: {s['metric']} ({s['verdict']})" for s in steps
    )
    return (
        f"bisection complete — {direction}\n"
        f"anchors: {good_sha[:10]}={good_metric}, {bad_sha[:10]}={bad_metric}\n"
        f"steps:\n{summary_steps}\n"
        f"CULPRIT: {culprit}\n"
        f"suggested action: open a revert PR for {culprit} first, fix-forward second"
    )

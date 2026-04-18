"""Offline prompt-version analytics from transitions.jsonl.

Research #3824 (DSPy-style prompt compilation) prescribes treating the
agent's own run logs as training signal: group outcomes by the
``prompts_version`` stamped into each transition, compute per-version
done/block rates, cluster failure modes, and surface which prompt
revision performs best. We're not running a full compiler yet; this is
the first step — make the attribution layer visible so prompt changes
can be evaluated with data instead of vibes.

Usage:
  from .prompt_analyzer import analyze
  report = analyze()                # in-process; reads /data/transitions.jsonl
  report["versions"]                 # list of per-version metrics
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .transitions import read_tail


def analyze(limit: int = 50000) -> dict[str, Any]:
    """Group transitions by prompts_version and compute:
      - feature_count: unique features touched under this version
      - terminal_done / terminal_blocked: terminal transitions recorded
      - done_rate: terminal_done / (terminal_done + terminal_blocked)
      - top_reasons: most common transition.reason strings (prefix-clustered)

    Only terminal-status transitions count toward the rate. Intermediate
    pending→implementing flips are noise. A version with <10 terminal
    samples is marked ``low_confidence`` to flag statistical thinness.
    """
    entries = read_tail(limit=limit)
    # per-version buckets
    per_version: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "feature_count": set(),
        "terminal_done": 0,
        "terminal_blocked": 0,
        "reasons": Counter(),
        "first_seen": None,
        "last_seen": None,
    })
    for e in entries:
        v = e.get("prompts_version") or "unknown"
        bucket = per_version[v]
        fid = e.get("feature_id", "")
        if fid:
            bucket["feature_count"].add(fid)
        ts = e.get("ts", "")
        if ts:
            if bucket["first_seen"] is None or ts < bucket["first_seen"]:
                bucket["first_seen"] = ts
            if bucket["last_seen"] is None or ts > bucket["last_seen"]:
                bucket["last_seen"] = ts
        to_status = e.get("to", "")
        if to_status == "done":
            bucket["terminal_done"] += 1
        elif to_status == "blocked":
            bucket["terminal_blocked"] += 1
            reason = (e.get("reason") or "").strip()
            # Cluster on prefix — different prompts produce different full
            # reasons for the same underlying failure mode.
            key = reason[:60].rstrip(":").rstrip(".") or "(blank)"
            bucket["reasons"][key] += 1

    versions: list[dict[str, Any]] = []
    for v, bucket in per_version.items():
        done = bucket["terminal_done"]
        blocked = bucket["terminal_blocked"]
        total = done + blocked
        rate = round(done / total, 3) if total else 0.0
        versions.append({
            "prompts_version": v,
            "feature_count": len(bucket["feature_count"]),
            "terminal_done": done,
            "terminal_blocked": blocked,
            "done_rate": rate,
            "low_confidence": total < 10,
            "first_seen": bucket["first_seen"],
            "last_seen": bucket["last_seen"],
            "top_reasons": [
                {"reason": r, "count": c}
                for r, c in bucket["reasons"].most_common(5)
            ],
        })
    # Sort newest-first by last_seen so the active version appears on top.
    versions.sort(key=lambda d: d["last_seen"] or "", reverse=True)
    # Compute best version that has enough samples to trust, so the
    # caller can tell "winning" vs "too-new-to-judge".
    trustworthy = [v for v in versions if not v["low_confidence"]]
    best = max(trustworthy, key=lambda v: v["done_rate"]) if trustworthy else None
    return {
        "total_transitions": len(entries),
        "versions": versions,
        "best_trusted_version": best["prompts_version"] if best else None,
        "best_trusted_done_rate": best["done_rate"] if best else None,
    }

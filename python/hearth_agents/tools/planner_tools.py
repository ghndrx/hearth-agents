"""Tools for recording planner output so the verifier can cross-check it later.

``record_planner_estimate`` persists the planner's estimated_diff_lines on
the Feature so verify_changes can catch undercount (>1.5x) before it blows
the 600-line diff cap (research job #3673).

Writes go through the in-process ``Backlog.update_planner_estimate`` rather
than touching the JSON file directly. The earlier disk-only path raced the
main Backlog's save(): the tool wrote 120, then a concurrent save() from
set_status/healer wrote its stale est=0 over the top, silently discarding
the value. Going through the shared in-memory instance keeps a single
source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.tools import tool

from ..backlog import get_default_backlog
from ..config import settings
from ..logger import log


@tool
def record_planner_estimate(feature_id: str, estimated_diff_lines: int) -> str:
    """Record the planner subagent's estimated diff size for a feature.

    Call this EXACTLY ONCE per feature, immediately after the planner returns
    its JSON plan and BEFORE delegating to any dev subagent. The value is
    later compared against the actual diff at verify time — if actual exceeds
    1.5x the estimate, the feature is blocked as "planner_undercount" and
    retried with a larger estimate, preventing runaway feature implementations
    that silently blow past the 600-line diff cap.

    Args:
        feature_id: The feature ID from the orchestrator's task prompt
            (e.g. "kbd-shortcut-hints").
        estimated_diff_lines: The planner's ``estimated_diff_lines`` field
            from its JSON output. Must be a positive integer.

    Returns:
        Human-readable confirmation or an error describing why the write failed.
    """
    if estimated_diff_lines <= 0:
        return f"error: estimated_diff_lines must be positive, got {estimated_diff_lines}"

    backlog = get_default_backlog()
    if backlog is not None:
        # In-process path: mutate the live Backlog so concurrent save() calls
        # from set_status/healer don't clobber our write.
        if backlog.update_planner_estimate(feature_id, int(estimated_diff_lines)):
            log.info(
                "planner_estimate_recorded",
                feature=feature_id,
                estimate_lines=estimated_diff_lines,
                via="in_memory",
            )
            return f"recorded estimate={estimated_diff_lines} for {feature_id}"
        return f"error: feature {feature_id} not found in in-memory backlog"

    # Fallback: no registered Backlog (e.g. running the tool in isolation for
    # testing). Best-effort disk write. Live production never hits this.
    path = Path(settings.backlog_path)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return f"error: backlog not found at {path}"
    except json.JSONDecodeError as e:
        return f"error: backlog unreadable: {e}"
    for f in data:
        if f.get("id") == feature_id:
            f["planner_estimate_lines"] = int(estimated_diff_lines)
            path.write_text(json.dumps(data, indent=2))
            log.info(
                "planner_estimate_recorded",
                feature=feature_id,
                estimate_lines=estimated_diff_lines,
                via="disk_fallback",
            )
            return f"recorded estimate={estimated_diff_lines} for {feature_id} (disk fallback)"
    return f"error: feature {feature_id} not found in backlog"

"""Feature splitter.

Features targeting multiple repos tend to produce enormous diffs because one
LangGraph run implements every repo in the same attempt. That's how we ended
up with ``data-export-portability`` at 2,649 lines and ``message-threading``
at **443,603 lines** — both tripping the 600-line cap and landing blocked.

Cheap, no-LLM heuristic: if a feature targets >1 repo, split into one child
feature per repo. Each child targets a single repo and inherits name/priority/
research_topics/self_improvement, with a note pointing back at the parent so
the orchestrator understands the context.

Children are added to the backlog with ``parent_id`` set. The parent's status
flips to ``done`` with a note that it was split — the loop skips it on the
next claim. The splitter only fires ONCE per feature (parent_id is a sentinel:
features with parent_id set are never re-split).
"""

from __future__ import annotations

from .backlog import Backlog, Feature
from .logger import log

# Minimum repos before we consider splitting. Single-repo features are never
# split. Changes to this threshold go here only.
_SPLIT_REPO_THRESHOLD = 2


def maybe_split(backlog: Backlog, feature: Feature) -> bool:
    """If ``feature`` is oversized by repo count, replace it with per-repo
    children and return True. Returns False if the feature is already a
    split child or targets only one repo.

    Caller must re-select the next feature after a True return; this function
    mutates the backlog and the input feature becomes ``done``/split, not
    the thing to implement.
    """
    if feature.parent_id:
        return False  # already a child, don't recurse
    if len(feature.repos) < _SPLIT_REPO_THRESHOLD:
        return False

    children: list[Feature] = []
    for repo in feature.repos:
        child = Feature(
            id=f"{feature.id}--{repo}",
            name=f"{feature.name} — {repo} slice",
            description=(
                f"{feature.description}\n\n"
                f"(Split from parent ``{feature.id}`` which targeted "
                f"{len(feature.repos)} repos. This child owns the ``{repo}`` "
                "slice only — do not touch the other repos in this attempt. "
                "Sibling work happens in separate features.)"
            ),
            priority=feature.priority,
            repos=[repo],  # type: ignore[list-item]
            research_topics=list(feature.research_topics),
            discord_parity=feature.discord_parity,
            self_improvement=feature.self_improvement,
            parent_id=feature.id,
        )
        if backlog.add(child):
            children.append(child)

    # Parent is replaced by its children — we REMOVE it from the backlog
    # entirely rather than marking it ``done``. Previously the ghost parent
    # counted as a success in /stats even when all its children were
    # blocked, inflating the done-rate and hiding real failures.
    try:
        backlog.features.remove(feature)
    except ValueError:
        # Another concurrent actor already removed it — harmless.
        pass
    backlog.save()

    if not children:
        # All child IDs already existed → prior split attempt. Parent is gone,
        # the existing children will run. No log — we've been here before.
        return True

    log.info(
        "feature_split",
        parent=feature.id,
        repos=feature.repos,
        children=[c.id for c in children],
    )
    return True

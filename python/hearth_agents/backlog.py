"""Feature backlog for the autonomous loop.

This is a Python port of the TypeScript backlog. The loop pulls the next
``pending`` feature, hands it to the DeepAgent, and the agent marks it ``done``
when its subagents finish implementation.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import json

Priority = Literal["critical", "high", "medium", "low"]
Status = Literal["pending", "researching", "implementing", "reviewing", "done", "blocked"]


def _norm_name(name: str) -> str:
    """Lowercase + alnum-only. Used for fuzzy dedup: "Auto Retention Policies"
    and "auto-retention-policies" normalize to "autoretentionpolicies"."""
    return "".join(c for c in name.lower() if c.isalnum())
Repo = Literal["hearth", "hearth-desktop", "hearth-mobile", "hearth-agents"]


@dataclass
class Feature:
    id: str
    name: str
    description: str
    priority: Priority = "medium"
    repos: list[Repo] = field(default_factory=lambda: ["hearth"])
    research_topics: list[str] = field(default_factory=list)
    discord_parity: str = ""
    status: Status = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Self-improvement features bypass normal priority ordering so the agent
    # tunes itself in between product features instead of waiting forever.
    self_improvement: bool = False
    # How many times the healer has reset this feature from blocked→pending.
    # Capped so a permanently-broken feature doesn't ping-pong forever.
    heal_attempts: int = 0
    # Set by the healer when it resets a blocked feature. Carries a targeted
    # hint into the next attempt's prompt so the agent doesn't repeat the same
    # failure mode (e.g. opening a worktree and committing nothing). Cleared
    # by the loop on successful completion.
    heal_hint: str = ""
    # If the splitter broke an over-broad feature into per-repo children,
    # each child's parent_id points at the original. Lets us reconstruct
    # the high-level intent and avoid re-splitting children recursively.
    parent_id: str = ""
    # Planner's pre-execution estimate of total diff lines. Recorded via the
    # ``record_planner_estimate`` tool after the planner subagent returns its
    # JSON. verify_changes compares this against the actual diff and flags
    # undercount (>1.5x) as a blocker — catches planner-under-estimation
    # before it burns verifier iterations (research job #3673).
    planner_estimate_lines: int = 0


# Initial backlog. The idea engine appends to this over time.
INITIAL_FEATURES: list[Feature] = [
    Feature(
        id="matrix-federation",
        name="Matrix Federation for E2EE",
        description=(
            "Implement Matrix protocol federation with Megolm/Vodozemac group encryption. "
            "Uniquely positions Hearth against Discord which has no federation."
        ),
        priority="critical",
        repos=["hearth"],
        research_topics=[
            "Matrix server-to-server API implementation in Go with gomatrixserverlib",
            "Matrix Megolm Vodozemac E2EE for federated chat",
        ],
        discord_parity="Server federation (competitive advantage — Discord has none)",
    ),
    Feature(
        id="voice-channels-always-on",
        name="Always-On Voice Channels",
        description=(
            "Discord-style persistent voice channels. Drop-in/drop-out, voice activity detection, "
            "push-to-talk, current-user sidebar display."
        ),
        priority="high",
        repos=["hearth", "hearth-desktop"],
        research_topics=[
            "LiveKit persistent voice channel architecture",
            "WebRTC voice activity detection VAD patterns",
        ],
        discord_parity="Core Discord feature",
    ),
    Feature(
        id="noise-suppression",
        name="Noise Suppression in Voice Channels",
        description=(
            "RNNoise-based ML noise cancellation for voice channels. Users immediately notice "
            "the quality difference vs Discord's Krisp-powered suppression."
        ),
        priority="high",
        repos=["hearth", "hearth-desktop", "hearth-mobile"],
        research_topics=[
            "RNNoise WebAssembly integration for browser-based noise suppression",
            "LiveKit audio track processor API for client-side ML",
        ],
        discord_parity="Discord uses Krisp — we match with open-source equivalent",
    ),
    # ── Dogfood features: hearth-agents improving itself ────────────────────
    Feature(
        id="self-prompt-tuning",
        name="Tune orchestrator/developer prompts from real run logs",
        description=(
            "Read the agent's own run log at ``/app/logs/hearth-agents.log`` (or "
            "``/tmp/hearth-agents.log`` in dev). Grep for tool-call histograms and "
            "for cases where Kimi responded with prose instead of tool calls. "
            "Use ``wikidelve_search`` to find prior research on prompt anti-patterns "
            "(jobs #472, #481). Edit ``python/hearth_agents/prompts.py`` to tighten "
            "whichever prompt produced the anti-pattern. Commit on the main branch "
            "of hearth-agents with message starting ``feat(prompts):``."
        ),
        priority="high",
        repos=["hearth-agents"],
        research_topics=[],
        discord_parity="(self-improvement)",
        self_improvement=True,
    ),
    Feature(
        id="self-add-cost-tracking",
        name="Per-feature cost tracking for MiniMax + Kimi calls",
        description=(
            "Add a CostTracker that hooks into LangChain callbacks to record input/output "
            "token counts per feature, persists to /data/costs.json, and exposes a "
            "``/costs`` FastAPI endpoint. Enforces ``per_feature_budget_usd``."
        ),
        priority="high",
        repos=["hearth-agents"],
        research_topics=[
            "LangChain callback handlers for token usage tracking per run",
        ],
        discord_parity="(self-improvement)",
        self_improvement=True,
    ),
]


class Backlog:
    """In-memory backlog with optional JSON persistence."""

    def __init__(self, persist_path: str | None = None):
        self.features: list[Feature] = list(INITIAL_FEATURES)
        self._path = Path(persist_path) if persist_path else None
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        data = json.loads(self._path.read_text())
        self.features = [Feature(**f) for f in data]
        # Features stuck in transient states from a prior crash/kill should be
        # retried, not abandoned. ``done`` and ``blocked`` are terminal and stay.
        for f in self.features:
            if f.status in ("implementing", "reviewing", "researching"):
                f.status = "pending"

    def save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([asdict(f) for f in self.features], indent=2))

    def next_pending(self) -> Feature | None:
        """Self-improvement features always jump the queue ahead of product
        features at the same priority band — the agent cannot help the user
        effectively if it keeps making the same prompt mistakes. Within each
        (self_improvement, priority) band, oldest first."""
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        candidates = [f for f in self.features if f.status == "pending"]
        candidates.sort(
            key=lambda f: (
                0 if f.self_improvement else 1,
                priority_order.get(f.priority, 99),
                f.created_at,
            )
        )
        return candidates[0] if candidates else None

    def set_status(self, feature_id: str, status: Status) -> None:
        for f in self.features:
            if f.id == feature_id:
                f.status = status
                self.save()
                return

    def add(self, feature: Feature) -> bool:
        """Append a feature. Returns False if the ID already exists OR the
        name is near-duplicate of an existing non-done feature.

        The fuzzy check is intentionally cheap: normalize to lowercase alnum
        and compare equality of the full normalized name. This catches the
        common idea-engine pattern of re-proposing the same feature with
        slight punctuation/casing drift ("Auto Retention Policies" vs
        "auto-retention-policies") while being too coarse to block genuinely
        different features.
        """
        if any(f.id == feature.id for f in self.features):
            return False
        norm = _norm_name(feature.name)
        if norm and any(
            f.status != "done" and _norm_name(f.name) == norm for f in self.features
        ):
            return False
        self.features.append(feature)
        self.save()
        return True

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.features:
            counts[f.status] = counts.get(f.status, 0) + 1
        counts["total"] = len(self.features)
        return counts

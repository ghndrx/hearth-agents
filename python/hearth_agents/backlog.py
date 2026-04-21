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
Kind = Literal["feature", "bug", "refactor", "schema", "security", "incident", "perf-revert"]
RiskTier = Literal["low", "medium", "high"]


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
    # Work type: "feature" (new capability, default) or "bug" (reproduce a
    # broken behavior + ship a failing test + fix it). Bug flow uses a
    # different prompt phase set — reproduce BEFORE planning — because
    # shipping a fix without a failing regression test is how bugs come
    # back (research #3803 — bug reproduction and fix loops).
    kind: Kind = "feature"
    # For bugs only: the command that reproduces the broken behavior. Agent
    # MUST see this fail before writing any fix code; the fix is "this
    # command now exits 0" rather than "I wrote some code".
    repro_command: str = ""
    # Human-readable done condition. For features: "login returns JWT on
    # valid creds". For bugs: "POST /messages no longer 500s when body is
    # empty; added regression test". Separate from description so the
    # agent's ACCEPTANCE statement has a concrete target.
    acceptance_criteria: str = ""
    # For incident + perf-revert kinds: gates auto-merge / auto-PR.
    # high = human Telegram approval required before anything goes to origin
    # medium = PR opens but is draft
    # low = normal auto-PR flow (default)
    risk_tier: RiskTier = "low"
    # List of feature IDs that must be in status=done before this feature
    # is schedulable. Lets multi-step projects queue coherently without a
    # human sequencer. next_pending() respects this.
    depends_on: list[str] = field(default_factory=list)
    # Optional per-feature budget override in USD. When > 0, takes precedence
    # over settings.per_feature_budget_usd in the loop's per-feature cost
    # check. Use for features known to be expensive (Matrix federation,
    # cross-repo refactors) that shouldn't trip the default budget cap.
    budget_usd: float = 0.0
    # Free-form operator labels for grouping across kinds (e.g. "q2-launch",
    # "deprecation-sweep", "tech-debt"). No constraints — operator discipline
    # only. Kanban filter exposes them so labeled features can be pulled up
    # together across repos + kinds.
    labels: list[str] = field(default_factory=list)

    def to_dict(self, updated_at: str | None = None) -> dict:
        """Curated JSON representation for the kanban UI. Includes a derived
        ``branch`` hint and truncated ``heal_hint`` so cards stay compact.

        ``updated_at`` — when provided, overrides ``created_at`` as the
        card's age reference. The server endpoint computes a
        feature_id → latest_ts map once per request (see server.py) so
        we don't re-read the transitions log N times.
        """
        branch = f"feat/{self.id}"
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description[:400],
            "priority": self.priority,
            "repos": list(self.repos),
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": updated_at or self.created_at,
            "heal_attempts": self.heal_attempts,
            "heal_hint": self.heal_hint[:500],
            "self_improvement": self.self_improvement,
            "parent_id": self.parent_id,
            "planner_estimate_lines": self.planner_estimate_lines,
            "kind": self.kind,
            "risk_tier": self.risk_tier,
            "depends_on": list(self.depends_on),
            "labels": list(self.labels),
            "repro_command": self.repro_command[:200],
            "acceptance_criteria": self.acceptance_criteria[:400],
            "branch": branch,
        }


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
        # Register as the default so tools running in the same process can
        # mutate this Backlog in-memory instead of racing with the JSON file.
        # Last-instance-wins on purpose — tests that build throwaway backlogs
        # will take over the slot while they run. Restoring isn't necessary
        # because production only ever constructs one Backlog.
        global _default_backlog
        _default_backlog = self

    def update_planner_estimate(self, feature_id: str, estimate_lines: int) -> bool:
        """Update a feature's planner estimate in-memory and persist. Returns
        True on success, False if the feature wasn't found. Thread-safety
        relies on asyncio's single-threaded execution — all callers run in
        the same event loop."""
        for f in self.features:
            if f.id == feature_id:
                f.planner_estimate_lines = max(0, int(estimate_lines))
                self.save()
                return True
        return False

    def _load(self) -> None:
        """Load backlog from disk. Falls back to the latest snapshot if the
        primary file is empty or malformed (e.g. truncated to 0 bytes by a
        SIGKILL mid-write — observed in prod and caused a 6-hour restart loop).
        Falls back to ``INITIAL_FEATURES`` if no snapshot is recoverable.
        """
        assert self._path is not None
        raw = self._path.read_text().strip()
        data: list | None = None
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                from .logger import log as _log
                _log.warning("backlog_load_corrupt", path=str(self._path), error=str(e)[:200])
        if data is None:
            data = self._load_from_snapshot()
        if data is None:
            # Last resort: keep the seeded INITIAL_FEATURES set by __init__.
            # The container boots cleanly instead of crash-looping.
            from .logger import log as _log
            _log.error("backlog_load_no_recovery", path=str(self._path))
            return
        self.features = [Feature(**f) for f in data]
        # Features stuck in transient states from a prior crash/kill should be
        # retried, not abandoned. ``done`` and ``blocked`` are terminal and stay.
        for f in self.features:
            if f.status in ("implementing", "reviewing", "researching"):
                f.status = "pending"

    def _load_from_snapshot(self) -> list | None:
        """Return the parsed contents of the most recent snapshot JSON in
        ``<backlog-dir>/backlog-snapshots/``, or None if none usable.
        """
        assert self._path is not None
        snap_dir = self._path.parent / "backlog-snapshots"
        if not snap_dir.is_dir():
            return None
        snaps = sorted(snap_dir.glob("*.json"))
        for snap in reversed(snaps):  # newest first
            try:
                parsed = json.loads(snap.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(parsed, list):
                from .logger import log as _log
                _log.warning("backlog_restored_from_snapshot", snapshot=snap.name, features=len(parsed))
                return parsed
        return None

    def save(self) -> None:
        """Atomic write via tmp + os.replace so a SIGKILL mid-write can't
        leave a 0-byte backlog.json. On POSIX, os.replace is atomic within
        the same filesystem; the tmp file lives alongside the target.
        """
        if not self._path:
            return
        import os as _os
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps([asdict(f) for f in self.features], indent=2))
        _os.replace(str(tmp), str(self._path))

    def next_pending(self) -> Feature | None:
        """Self-improvement features always jump the queue ahead of product
        features at the same priority band — the agent cannot help the user
        effectively if it keeps making the same prompt mistakes. Within each
        (self_improvement, priority) band, oldest first.

        Respects ``Feature.depends_on``: a feature is skipped if any
        dependency is not yet ``done`` (or is absent from the backlog).
        This prevents scheduling downstream work before its prerequisite
        lands without requiring a manual sequencer.
        """
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        done_or_missing = {f.id for f in self.features if f.status == "done"}
        def _blocked_by_dep(f: Feature) -> bool:
            return bool(f.depends_on) and not all(d in done_or_missing for d in f.depends_on)
        candidates = [
            f for f in self.features
            if f.status == "pending" and not _blocked_by_dep(f)
        ]
        candidates.sort(
            key=lambda f: (
                0 if f.self_improvement else 1,
                priority_order.get(f.priority, 99),
                f.created_at,
            )
        )
        return candidates[0] if candidates else None

    def set_status(self, feature_id: str, status: Status, reason: str = "", actor: str = "loop") -> None:
        from .transitions import record_transition
        for f in self.features:
            if f.id == feature_id:
                if f.status == status:
                    return  # no-op, don't pollute the transition log
                record_transition(feature_id, f.status, status, reason=reason, actor=actor)
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

    def archive_old_done(self, max_age_days: int = 7) -> int:
        """Move done features older than ``max_age_days`` into a sibling
        archive file alongside backlog.json. Keeps the live backlog +
        every /features payload responsive as the project ages.

        Self-improvement features and features with parent_id (split
        children) are kept regardless — they're cross-referenced by
        other parts of the system.
        """
        if not self._path:
            return 0
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        keep: list[Feature] = []
        archive_now: list[Feature] = []
        for f in self.features:
            if f.status != "done":
                keep.append(f)
                continue
            if f.self_improvement or f.parent_id:
                keep.append(f)
                continue
            try:
                created = datetime.fromisoformat(f.created_at.replace("Z", "+00:00"))
            except ValueError:
                keep.append(f)
                continue
            if created < cutoff:
                archive_now.append(f)
            else:
                keep.append(f)
        if not archive_now:
            return 0
        archive_path = self._path.with_name("archive.json")
        existing: list[dict] = []
        if archive_path.exists():
            try:
                existing = json.loads(archive_path.read_text())
            except (OSError, json.JSONDecodeError):
                existing = []
        existing.extend(asdict(f) for f in archive_now)
        archive_path.write_text(json.dumps(existing, indent=2))
        self.features = keep
        self.save()
        return len(archive_now)

    def action(self, feature_id: str, action: str) -> tuple[bool, str]:
        """Apply a kanban action to a feature. Returns (success, message).

        Actions:
        - ``approve``: mark a blocked feature as human-verified done. Clears
          heal state so re-queues don't carry stale hints.
        - ``retry``:   reset heal_attempts and flip blocked → pending so the
          loop takes another crack. This is the "maybe another attempt fixes
          it" path — distinct from approve which asserts human verification.
        - ``nuke``:    drop the feature from the backlog. Irreversible. Used
          for debris features the agent will never productively resolve.
        """
        from .transitions import record_transition
        for i, f in enumerate(self.features):
            if f.id != feature_id:
                continue
            if action == "approve":
                record_transition(feature_id, f.status, "done", reason="kanban approve", actor="kanban")
                f.status = "done"
                f.heal_hint = ""
                f.heal_attempts = 0
                self.save()
                return True, f"{feature_id} -> done"
            if action == "retry":
                record_transition(feature_id, f.status, "pending", reason="kanban retry", actor="kanban")
                f.status = "pending"
                f.heal_attempts = 0
                f.heal_hint = ""
                self.save()
                return True, f"{feature_id} -> pending"
            if action == "nuke":
                record_transition(feature_id, f.status, "nuked", reason="kanban nuke", actor="kanban")
                self.features.pop(i)
                self.save()
                return True, f"{feature_id} removed"
            if action == "cleanup_branch":
                # Delete the feature's feat/<id> branch on origin (and
                # any remaining worktree). Useful after the PR merged
                # and you don't want stale branches piling up. Doesn't
                # touch the feature row itself; just hygiene.
                from .gc_worktrees import delete_feature_branch_everywhere
                summary = delete_feature_branch_everywhere(f)
                record_transition(feature_id, f.status, f.status, reason=f"branch_cleanup: {summary}", actor="kanban")
                return True, summary
            return False, f"unknown action: {action}"
        return False, f"feature not found: {feature_id}"


# Module-level registry of the "default" (main-process) Backlog instance. Set
# by __init__ so tools (like ``record_planner_estimate``) can mutate the live
# in-memory state directly instead of writing to disk and racing with the
# in-memory instance's save(). Single source of truth.
_default_backlog: "Backlog | None" = None


def get_default_backlog() -> "Backlog | None":
    """Return the Backlog instance registered by the first __init__ call, or
    None if no Backlog has been instantiated yet. Tools should use this to
    mutate live state rather than writing the JSON file directly."""
    return _default_backlog
